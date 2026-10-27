"""End-to-end test of the MCP server over real MCP (streamable HTTP).

  selftest.py            full test against the live lab, including all five faults
  selftest.py --no-faults  only auth, policy, approvals and budgets

Prints one JSON object: {"passed": n, "failed": n, "results": [...]}.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

BASE = os.environ.get("OPSMCP_URL", "http://127.0.0.1:8765")
TOKENS = os.environ.get("OPSMCP_TOKENS", "/etc/agent-ops-lab")
ROOT = os.environ.get("LAB_ROOT", "/opt/agent-ops-lab")
FAULT = os.path.join(ROOT, "lab", "faults", "fault.sh")
REAL = "--no-faults" not in sys.argv

TOOLS = {"list_hosts", "check_service", "run_command", "read_logs", "recent_changes", "restart_service", "delete_file",
         "update_dns_record", "renew_certificate", "reload_load_balancer", "terminate_db_sessions"}

results: list[dict] = []


def token(name):
    with open(os.path.join(TOKENS, f"{name}.token")) as f:
        return f.read().strip()


AGENT, OPERATOR = token("agent"), token("operator")


def rec(name, ok, detail=""):
    results.append({"test": name, "ok": bool(ok), "detail": str(detail)[-500:]})


def text(result) -> str:
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def op(method, path, body=None, auth=OPERATOR):
    async with httpx2.AsyncClient(timeout=20) as http:
        r = await http.request(method, BASE + path, json=body, headers={"Authorization": f"Bearer {auth}"} if auth else {})
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text)


async def new_run(approval="auto", **kw):
    run_id = "test-" + uuid.uuid4().hex[:10]
    status, run = await op("POST", "/operator/runs", {"id": run_id, "approval": approval, "label": "selftest", **kw})
    assert status == 201, run
    return run_id


@asynccontextmanager
async def agent(run_id):
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {AGENT}", "X-Run-Id": run_id}, timeout=httpx2.Timeout(30, read=400))
    try:
        async with Client(streamable_http_client(f"{BASE}/mcp", http_client=http), read_timeout_seconds=400) as client:
            yield client
    finally:
        await http.aclose()


async def call(client, tool, **args) -> str:
    return text(await client.call_tool(tool, args))


def fault(name, action):
    p = subprocess.run(["bash", FAULT, name, action], capture_output=True, text=True, timeout=240)
    return p.stdout + p.stderr


ALLOWED = [
    ("office-pc", "dig +short crm.lab.internal", "172.30.0.40"),
    ("office-pc", "openssl x509 -in /certs/live/crm.crt -noout -enddate", "notAfter"),
    ("crm-app-02", "df -h /var/lib/crm", "/var/lib/crm"),
    ("db-01", 'psql -c "SELECT usename FROM pg_stat_activity WHERE usename IS NOT NULL"', "ops_readonly"),
    ("db-01", "psql -c 'SHOW max_connections'", "40"),
    ("dns-01", "cat /zones/lab.internal.db", "SOA"),
    ("lb-01", "nginx -t", "successful"),
]

DENIED = [
    ("crm-app-01", "cat /proc/1/environ"),
    ("crm-app-01", "env"),
    ("lb-01", "cat /certs/live/crm.key"),
    ("crm-app-01", "rm -rf /var/lib/crm"),
    ("crm-app-01", "ls /var/lib/crm | head"),
    ("office-pc", "curl -d x=1 https://crm.lab.internal/customers"),
    ("db-01", 'psql -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"'),
    ("db-01", 'psql -c "DELETE FROM customers"'),
    ("office-pc", "ping crm.lab.internal"),
]

FIXES = [
    ("dns-stale", "update_dns_record", {"name": "crm.lab.internal", "ip": "172.30.0.40"}),
    ("cert-expired", "renew_certificate", {}),
    ("app-crash", "restart_service", {"host": "crm-app-01"}),
    ("disk-full", "delete_file", {"host": "crm-app-02", "path": "/var/lib/crm/debug-export-trace.log"}),
    ("db-connections", "terminate_db_sessions", {"query_contains": "sales-export"}),
]


async def main():
    # Auth: each endpoint needs its own token.
    rec("operator API rejects no token", (await op("GET", "/operator/audit", auth=None))[0] == 401)
    rec("operator API rejects the agent token", (await op("GET", "/operator/audit", auth=AGENT))[0] == 401)
    async with httpx2.AsyncClient(timeout=10) as http:
        r = await http.post(BASE + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        rec("MCP endpoint rejects no token", r.status_code == 401, r.status_code)

    # Unregistered run.
    async with agent("unregistered-run") as c:
        r = await c.call_tool("list_hosts", {})
        rec("tool call without a registered run is refused", r.is_error and "registered run" in text(r), text(r))

    run_a = await new_run("auto", maxChanges=10, maxCalls=120)
    async with agent(run_a) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        rec("server lists the 11 tools", tools == TOOLS, sorted(tools))
        out = await call(c, "list_hosts")
        rec("list_hosts shows the inventory", "lb-01" in out and "172.30.0.21" in out, out[:300])

        for host, cmd, expect in ALLOWED:
            out = await call(c, "run_command", host=host, command=cmd)
            ok = not out.startswith("BLOCKED") and (expect in out if REAL else True)
            rec(f"allowed [{host}] {cmd}", ok, out[:300])
        for host, cmd in DENIED:
            out = await call(c, "run_command", host=host, command=cmd)
            rec(f"blocked [{host}] {cmd}", out.startswith("BLOCKED"), out[:200])

        out = await call(c, "delete_file", host="crm-app-02", path="/var/lib/crm/audit/2026-10-27/x.json", reason="free space on the shared volume")
        rec("policy blocks deleting audit records", out.startswith("BLOCKED") and "compliance" in out, out[:200])
        out = await call(c, "terminate_db_sessions", reason="too many connections on the database")
        rec("policy blocks terminating every session", out.startswith("BLOCKED"), out[:200])
        out = await call(c, "update_dns_record", name="crm.lab.internal", ip="8.8.8.8", reason="point crm at a public resolver")
        rec("policy blocks DNS outside the lab network", out.startswith("BLOCKED"), out[:200])

        if REAL:
            for name, tool, args in FIXES:
                try:
                    fault(name, "inject")
                    time.sleep(3)
                    broken = await call(c, "check_service")
                    fixed = await call(c, tool, reason=f"selftest: fixing {name} with {tool}", **args)
                    time.sleep(3)
                    after = await call(c, "check_service")
                    rec(f"{name}: breaks the service", "Result: PASSED" not in broken, broken[-200:])
                    rec(f"{name}: {tool} is approved by policy and runs", fixed.startswith("APPROVED by policy:auto"), fixed[:300])
                    rec(f"{name}: service passes after the fix", "Result: PASSED" in after, after[-300:])
                finally:
                    fault(name, "restore")
                    time.sleep(2)

    # Human approval: deny, then approve.
    run_b = await new_run("human")
    async with agent(run_b) as c:
        async def decide(decision, note):
            for _ in range(40):
                _, pending = await op("GET", f"/operator/approvals?run={run_b}&status=pending")
                if pending:
                    await op("POST", f"/operator/approvals/{pending[0]['id']}", {"decision": decision, "by": "anthony", "note": note})
                    return pending[0]
                await asyncio.sleep(0.25)

        task = asyncio.create_task(call(c, "restart_service", host="office-pc", reason="selftest: operator should deny this"))
        pending = await decide("deny", "office-pc is fine; look at the server side")
        out = await task
        rec("human mode waits for the operator", pending is not None and pending["summary"].startswith("Restart office-pc"), pending)
        rec("denied change is not made and the note reaches the agent", out.startswith("DENIED by anthony") and "server side" in out, out[:300])

        task = asyncio.create_task(call(c, "reload_load_balancer", reason="selftest: operator should approve this"))
        await decide("approve", "")
        out = await task
        rec("approved change runs", out.startswith("APPROVED by anthony"), out[:300])

    run_c = await new_run("deny")
    async with agent(run_c) as c:
        out = await call(c, "reload_load_balancer", reason="selftest: dry run should refuse this")
        rec("dry-run mode refuses every change", out.startswith("DENIED by policy:deny"), out[:200])

    run_d = await new_run("auto", maxCalls=2, maxChanges=0)
    async with agent(run_d) as c:
        await call(c, "recent_changes")
        out = await call(c, "reload_load_balancer", reason="selftest: change budget is zero")
        rec("change budget is enforced", "Change budget" in out, out[:200])
        out = await call(c, "recent_changes")
        rec("tool-call budget is enforced", "Tool budget" in out, out[:200])

    _, entries = await op("GET", f"/operator/audit?run={run_a}")
    decisions = {e["decision"] for e in entries}
    rec("audit log records every call for the run", len(entries) >= 1 + len(ALLOWED) + len(DENIED) + 3 and {"allowed", "blocked"} <= decisions,
        f"{len(entries)} entries, decisions {sorted(decisions)}")
    if REAL:
        rec("audit log records executed changes", "executed" in decisions, sorted(decisions))
    for r in (run_a, run_b, run_c, run_d):
        await op("DELETE", f"/operator/runs/{r}")


if __name__ == "__main__":
    started = time.time()
    try:
        asyncio.run(main())
    except Exception as e:
        rec("selftest crashed", False, f"{type(e).__name__}: {e}")
    if REAL:
        subprocess.run(["bash", FAULT, "restore-all"], capture_output=True, timeout=240)
    failed = [r for r in results if not r["ok"]]
    print(json.dumps({"passed": len(results) - len(failed), "failed": len(failed), "seconds": int(time.time() - started), "results": results}))
