"""Public incident API (behind Caddy at /api/incidents and /api/lab).

POST /api/incidents                    {"fault": "random" | name, "approval": "human" | "auto"}
                                       injects a real fault, registers a run on the MCP server and starts the agent.
                                       Returns the incident id and an approver key (only the starter can approve).
GET  /api/incidents                    recent incidents
GET  /api/incidents/{id}               one incident's summary
GET  /api/incidents/{id}/events        Server-Sent Events: the full timeline, replayed then live
POST /api/incidents/{id}/approvals/{approval}   {"decision": "approve" | "deny", "note": "...", "key": "..."}
GET  /api/lab                          is the lab busy, and with which incident

One incident at a time: the lab is a single shared environment. After every
incident the service grades it (synthetic check), then restores every fault so
the next visitor starts from a healthy lab.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

import httpx2
import uvicorn
from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent import run_agent, token

ROOT = os.environ.get("LAB_ROOT", "/opt/agent-ops-lab")
FAULT = os.path.join(ROOT, "lab", "faults", "fault.sh")
COMPOSE = ["docker", "compose", "-f", os.path.join(ROOT, "lab", "compose.yaml"), "--env-file", os.path.join(ROOT, "lab", ".env")]
DATA = os.environ.get("INCIDENTS_DIR", "/var/lib/agent-ops-lab/incidents")
LOCK_FILE = "/var/lib/agent-ops-lab/lab.lock"
OPERATOR = os.environ.get("OPSMCP_URL", "http://127.0.0.1:8765") + "/operator"
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"}
FAULTS = ["disk-full", "app-crash", "dns-stale", "cert-expired", "db-connections"]
PER_IP_HOUR = int(os.environ.get("INCIDENTS_PER_IP_HOUR", "4"))
PER_DAY = int(os.environ.get("INCIDENTS_PER_DAY", "40"))
ADMIN_IPS = {"127.0.0.1"}

current: dict | None = None
history_ip: dict[str, deque] = defaultdict(deque)
history_day: deque = deque()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def path(incident_id: str, ext: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", incident_id):
        raise ValueError("bad id")
    return os.path.join(DATA, f"{incident_id}.{ext}")


def save(incident: dict) -> None:
    os.makedirs(DATA, exist_ok=True)
    public = {k: v for k, v in incident.items() if not k.startswith("_")}
    with open(path(incident["id"], "json.tmp"), "w") as f:
        json.dump(public, f, indent=1)
    os.replace(path(incident["id"], "json.tmp"), path(incident["id"], "json"))


def load(incident_id: str) -> dict | None:
    try:
        with open(path(incident_id, "json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def emitter(incident: dict):
    seq = {"n": 0}

    async def emit(kind: str, **data):
        seq["n"] += 1
        event = {"seq": seq["n"], "at": now(), "type": kind, **data}
        with open(path(incident["id"], "jsonl"), "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
        if kind == "usage":
            incident["usage"] = data
        elif kind == "report":
            incident["report"] = data
        elif kind == "tool_call":
            incident["steps"] = incident.get("steps", 0) + 1
        save(incident)
    return emit


async def sh(*argv, timeout=240) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode, out.decode("utf-8", "replace")


async def synthetic() -> tuple[str, str]:
    code, out = await sh(*COMPOSE, "exec", "-T", "client", "python", "/tools/synthetic_check.py", timeout=60)
    m = re.search(r"Result: (\w+)", out)
    return (m.group(1) if m else "ERROR"), out.strip()


async def operator(method: str, sub: str, body=None):
    async with httpx2.AsyncClient(timeout=15) as http:
        r = await http.request(method, OPERATOR + sub, json=body, headers={"Authorization": f"Bearer {token('operator')}"})
        return r.status_code, r.json()


async def watch_approvals(incident: dict, emit, stop: asyncio.Event):
    """Relay approval requests and decisions from the MCP server into the timeline."""
    seen: dict[str, str] = {}
    while not stop.is_set():
        try:
            _, items = await operator("GET", f"/approvals?run={incident['run']}")
            for a in items:
                if seen.get(a["id"]) != a["status"]:
                    seen[a["id"]] = a["status"]
                    await emit("approval", approvalId=a["id"], tool=a["tool"], args=a["args"], summary=a["summary"],
                               reason=a["reason"], status=a["status"], decidedBy=a.get("decidedBy"), note=a.get("note"))
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), 0.5)
        except asyncio.TimeoutError:
            pass


async def work(incident: dict, lock_fd):
    global current
    emit = emitter(incident)
    stop = asyncio.Event()
    watcher = None
    try:
        await emit("status", phase="injecting", text="Breaking something in the lab (the agent is not told what).")
        await sh("bash", FAULT, "restore-all")
        code, out = await sh("bash", FAULT, incident["_fault"], "inject")
        if code != 0:
            raise RuntimeError(f"fault injection failed: {out[-300:]}")
        await asyncio.sleep(4)
        verdict, _ = await synthetic()
        incident["before"] = verdict
        await emit("status", phase="investigating", text=f"Users now see: {verdict}. The agent is on it.", verdict=verdict)

        status, run = await operator("POST", "/runs", {"id": "inc-" + incident["id"], "approval": incident["approval"],
                                                        "label": f"incident {incident['id']}", "maxCalls": 60, "maxChanges": 5})
        if status != 201:
            raise RuntimeError(f"could not register the run: {run}")
        incident["run"] = run["id"]
        watcher = asyncio.create_task(watch_approvals(incident, emit, stop))
        result = await run_agent(run["id"], incident["model"], emit)
        incident["usage"] = result["usage"]
    except Exception as e:
        incident["error"] = f"{type(e).__name__}: {e}"
        await emit("error", text=incident["error"])
    finally:
        try:
            stop.set()
            if watcher:
                await asyncio.gather(watcher, return_exceptions=True)
            if incident.get("run"):
                await operator("DELETE", f"/runs/{incident['run']}")
            verdict, check = await synthetic()
            incident["after"] = verdict
            incident["resolved"] = verdict == "PASSED" and incident.get("before") not in (None, "PASSED")
            incident["fault"] = incident["_fault"]
            incident["finished"] = now()
            await emit("graded", verdict=verdict, check=check, fault=incident["fault"], resolved=incident["resolved"])
            await emit("status", phase="restoring", text="Resetting the lab for the next visitor.")
            await sh("bash", FAULT, "restore-all")
            if (await synthetic())[0] != "PASSED":
                await sh(*COMPOSE, "up", "-d")
                await sh("bash", FAULT, "restore-all")
            await emit("end", resolved=incident["resolved"], fault=incident["fault"])
        except Exception as e:
            incident["finished"] = incident.get("finished") or now()
            save(incident)
            with open(path(incident["id"], "jsonl"), "a") as f:
                f.write(json.dumps({"seq": 0, "at": now(), "type": "end", "resolved": False, "error": str(e)}) + "\n")
        finally:
            current = None
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()


def rate_limited(ip: str) -> str | None:
    t = time.time()
    for q, window in ((history_ip[ip], 3600), (history_day, 86400)):
        while q and q[0] < t - window:
            q.popleft()
    if ip in ADMIN_IPS:
        return None
    if len(history_ip[ip]) >= PER_IP_HOUR:
        return f"You can start {PER_IP_HOUR} incidents per hour. Watch a recent one in the meantime."
    if len(history_day) >= PER_DAY:
        return "The lab has reached today's incident limit. Recent incidents can still be replayed."
    return None


async def create(request: Request) -> JSONResponse:
    global current
    try:
        body = await request.json()
    except ValueError:
        body = {}
    fault = body.get("fault") or "random"
    approval = body.get("approval") or "human"
    model = body.get("model") or MODEL
    if fault != "random" and fault not in FAULTS:
        return JSONResponse({"error": f"fault must be random or one of {', '.join(FAULTS)}"}, status_code=400)
    if approval not in ("human", "auto"):
        return JSONResponse({"error": "approval must be human or auto"}, status_code=400)
    ip = client_ip(request)
    if model not in MODELS or (model != MODEL and ip not in ADMIN_IPS):
        return JSONResponse({"error": "model not available"}, status_code=400)
    if current:
        return JSONResponse({"error": "The lab is busy with another incident.", "current": current["id"]}, status_code=409)
    limited = rate_limited(ip)
    if limited:
        return JSONResponse({"error": limited}, status_code=429)
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return JSONResponse({"error": "The lab is busy (a self-test is running). Try again in a few minutes."}, status_code=409)
    key = secrets.token_urlsafe(18)
    incident = {"id": uuid.uuid4().hex[:12], "created": now(), "approval": approval, "model": model, "faultChoice": fault,
                "_fault": random.choice(FAULTS) if fault == "random" else fault, "_key": hashlib.sha256(key.encode()).hexdigest()}
    current = incident
    history_ip[ip].append(time.time())
    history_day.append(time.time())
    save(incident)
    open(path(incident["id"], "jsonl"), "w").close()
    asyncio.create_task(work(incident, fd))
    return JSONResponse({"id": incident["id"], "key": key}, status_code=201)


async def listing(request: Request) -> JSONResponse:
    try:
        names = sorted((n for n in os.listdir(DATA) if n.endswith(".json")), key=lambda n: os.path.getmtime(os.path.join(DATA, n)), reverse=True)
    except OSError:
        names = []
    items = [i for i in (load(n[:-5]) for n in names[:30]) if i]
    return JSONResponse([{k: i.get(k) for k in ("id", "created", "finished", "approval", "model", "resolved", "steps", "fault", "error")}
                         | {"fault": i.get("fault") if i.get("finished") else None} for i in items])


def get_incident(request: Request) -> dict | None:
    try:
        return load(request.path_params["id"])
    except ValueError:
        return None


async def one(request: Request) -> JSONResponse:
    incident = get_incident(request)
    if not incident:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not incident.get("finished"):
        incident.pop("fault", None)
    return JSONResponse(incident)


async def events(request: Request):
    incident = get_incident(request)
    if not incident:
        return JSONResponse({"error": "not found"}, status_code=404)
    file = path(incident["id"], "jsonl")

    async def stream():
        pos, idle = 0, 0
        while True:
            with open(file) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            ended = False
            for line in chunk.splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                yield {"event": "message", "data": line}
                ended = ended or event["type"] == "end"
            if ended or await request.is_disconnected():
                return
            idle = 0 if chunk else idle + 1
            if idle > 2400:  # 20 minutes with nothing new
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(stream(), ping=15)


async def decide(request: Request) -> JSONResponse:
    incident = current if current and current["id"] == request.path_params["id"] else None
    if not incident:
        return JSONResponse({"error": "This incident is not running."}, status_code=404)
    body = await request.json()
    if not secrets.compare_digest(hashlib.sha256(str(body.get("key", "")).encode()).hexdigest(), incident["_key"]):
        return JSONResponse({"error": "Only the person who started this incident can approve its changes."}, status_code=403)
    if body.get("decision") not in ("approve", "deny"):
        return JSONResponse({"error": "decision must be approve or deny"}, status_code=400)
    status, record = await operator("POST", f"/approvals/{request.path_params['approval']}",
                                    {"decision": body["decision"], "by": "you", "note": str(body.get("note") or "")[:300]})
    if status != 200 or record.get("run") != incident.get("run"):
        return JSONResponse({"error": "approval not found"}, status_code=404)
    return JSONResponse({"status": record["status"]})


async def lab(request: Request) -> JSONResponse:
    return JSONResponse({"busy": current is not None, "current": current["id"] if current else None, "model": MODEL,
                         "faults": FAULTS, "perIpPerHour": PER_IP_HOUR})


app = Starlette(
    routes=[
        Route("/api/lab", lab),
        Route("/api/incidents", create, methods=["POST"]),
        Route("/api/incidents", listing),
        Route("/api/incidents/{id}", one),
        Route("/api/incidents/{id}/events", events),
        Route("/api/incidents/{id}/approvals/{approval}", decide, methods=["POST"]),
    ],
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["content-type"])],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8090")), log_level="warning", proxy_headers=True, forwarded_allow_ips="127.0.0.1")
