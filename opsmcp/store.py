"""Runs, approvals and the audit log, kept as files so they survive restarts.

A run is one agent session. The operator registers it before the agent
connects and chooses how changes are approved:
  human  each change waits for an operator decision
  auto   changes are approved by policy (used by the benchmark)
  deny   dry run: every change is refused
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone

STATE = os.environ.get("OPSMCP_STATE", "/var/lib/agent-ops-lab/opsmcp")
AUDIT = os.environ.get("OPSMCP_AUDIT", "/var/log/agent-ops-lab/mcp-audit.jsonl")
MODES = ("human", "auto", "deny")

_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _path(kind: str, ident: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", ident):
        raise ValueError("bad id")
    return os.path.join(STATE, kind, ident + ".json")


def _load(kind: str, ident: str) -> dict | None:
    try:
        with open(_path(kind, ident)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(kind: str, record: dict) -> dict:
    path = _path(kind, record["id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(record, f, indent=1)
    os.replace(path + ".tmp", path)
    return record


# ---------- runs ----------

def create_run(run_id: str | None, approval: str, max_calls: int, max_changes: int, label: str = "") -> dict:
    if approval not in MODES:
        raise ValueError(f"approval must be one of {', '.join(MODES)}")
    run = {"id": run_id or uuid.uuid4().hex[:16], "label": label[:80], "approval": approval, "created": now(),
           "maxCalls": max_calls, "maxChanges": max_changes, "calls": 0, "changes": 0, "closed": None}
    with _lock:
        if _load("runs", run["id"]):
            raise ValueError("run already exists")
        return _save("runs", run)


def get_run(run_id: str | None) -> dict | None:
    if not run_id:
        return None
    try:
        return _load("runs", run_id)
    except ValueError:
        return None


def close_run(run_id: str) -> dict | None:
    with _lock:
        run = _load("runs", run_id)
        if run and not run["closed"]:
            run["closed"] = now()
            _save("runs", run)
        return run


def count_call(run_id: str) -> tuple[bool, str]:
    """Charge one tool call to the run's budget."""
    with _lock:
        run = _load("runs", run_id)
        if not run:
            return False, "Unknown run."
        if run["closed"]:
            return False, "This run is closed."
        if run["calls"] >= run["maxCalls"]:
            return False, f"Tool budget for this run is used up ({run['maxCalls']} calls). Stop and report what you found."
        run["calls"] += 1
        _save("runs", run)
        return True, ""


def charge_change(run_id: str) -> tuple[bool, str]:
    """Charge one change request to the run's change budget."""
    with _lock:
        run = _load("runs", run_id)
        if run["changes"] >= run["maxChanges"]:
            return False, f"Change budget for this run is used up ({run['maxChanges']} changes). Stop and report what you found and what is still broken."
        run["changes"] += 1
        _save("runs", run)
        return True, ""


# ---------- approvals ----------

def create_approval(run: dict, tool: str, args: dict, summary: str, reason: str) -> dict:
    record = {"id": uuid.uuid4().hex[:16], "run": run["id"], "tool": tool, "args": args, "summary": summary,
              "reason": reason[:500], "created": now(), "status": "pending", "decidedBy": None, "note": None, "decided": None}
    if run["approval"] == "auto":
        record.update(status="approved", decidedBy="policy:auto", decided=now())
    elif run["approval"] == "deny":
        record.update(status="denied", decidedBy="policy:deny", note="Changes are disabled for this run (dry run).", decided=now())
    with _lock:
        return _save("approvals", record)


def get_approval(approval_id: str) -> dict | None:
    try:
        return _load("approvals", approval_id)
    except ValueError:
        return None


def decide(approval_id: str, approve: bool, by: str, note: str = "") -> dict | None:
    with _lock:
        record = get_approval(approval_id)
        if not record or record["status"] != "pending":
            return record
        record.update(status="approved" if approve else "denied", decidedBy=by[:60], note=note[:300] or None, decided=now())
        return _save("approvals", record)


def expire(approval_id: str) -> dict | None:
    with _lock:
        record = get_approval(approval_id)
        if record and record["status"] == "pending":
            record.update(status="expired", decided=now())
            _save("approvals", record)
        return record


def finish(approval_id: str, result: str) -> None:
    with _lock:
        record = get_approval(approval_id)
        if record:
            record.update(status="executed", result=result[:2000], executed=now())
            _save("approvals", record)


def list_approvals(run_id: str | None = None, status: str | None = None) -> list[dict]:
    folder = os.path.join(STATE, "approvals")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    items = []
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = get_approval(name[:-5])
        if rec and (not run_id or rec["run"] == run_id) and (not status or rec["status"] == status):
            items.append(rec)
    return sorted(items, key=lambda r: r["created"])


def wait_for_decision(approval_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        record = get_approval(approval_id)
        if record["status"] != "pending":
            return record
        if time.monotonic() > deadline:
            return expire(approval_id)
        time.sleep(0.5)


# ---------- audit ----------

def audit(entry: dict) -> None:
    entry = {"at": now(), **entry}
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    line = json.dumps(entry, default=str)
    with _lock, open(AUDIT, "a") as f:
        f.write(line + "\n")


def read_audit(run_id: str | None = None, limit: int = 500) -> list[dict]:
    try:
        with open(AUDIT) as f:
            rows = f.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(rows):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not run_id or e.get("run") == run_id:
            out.append(e)
            if len(out) >= limit:
                break
    return list(reversed(out))
