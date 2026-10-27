"""labctl: the lab's control and status service, listening on port 80.

GET  /                 human-readable status page
GET  /api/status       containers, active faults, synthetic check, last deploy
GET  /api/selftest     inject every fault in turn, check it breaks the CRM,
                       restore it, check it recovers (one run at a time)
GET  /api/mcptest      run the MCP server's end-to-end test (opsmcp/selftest.py):
                       auth, command policy, approvals, budgets, and fixing all
                       five faults through MCP tools

Stage 1 exposes no way to inject a single fault from outside; the agent API
in stage 3 adds that behind rate limits.
"""

import html
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("LAB_ROOT", "/opt/agent-ops-lab")
LAB = os.path.join(ROOT, "lab")
FAULT = os.path.join(LAB, "faults", "fault.sh")
DEPLOY_LOG = "/var/log/agent-ops-lab/deploy.log"
SELFTEST_FILE = "/var/lib/agent-ops-lab/selftest.json"

MCPTEST_FILE = "/var/lib/agent-ops-lab/mcptest.json"
VENV_PY = os.path.join(ROOT, ".venv", "bin", "python")

selftest_lock = threading.Lock()
selftest_state = {"running": False, "started": None, "results": None}
mcptest_lock = threading.Lock()
mcptest_state = {"running": False, "started": None, "report": None}


def sh(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def compose(*args, timeout=60):
    return sh(["docker", "compose", "-f", os.path.join(LAB, "compose.yaml"), "--env-file", os.path.join(LAB, ".env"), *args], timeout)


def containers():
    code, out = compose("ps", "-a", "--format", "json")
    if code != 0:
        return [{"error": out[-400:]}]
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        c = json.loads(line)
        items.append({"service": c.get("Service"), "state": c.get("State"), "status": c.get("Status")})
    return items


def synthetic():
    code, out = compose("exec", "-T", "client", "python", "/tools/synthetic_check.py", "--json", timeout=60)
    try:
        return json.loads(out.splitlines()[-1])
    except Exception:
        return {"verdict": "ERROR", "error": out[-400:]}


def tail(path, n=25):
    try:
        with open(path) as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def status():
    _, commit = sh(["git", "-C", ROOT, "log", "-1", "--format=%h %s (%cr)"])
    _, faults = sh(["bash", FAULT, "status"])
    return {
        "commit": commit,
        "containers": containers(),
        "faults": [f for f in faults.splitlines() if f],
        "synthetic": synthetic(),
        "deployLog": tail(DEPLOY_LOG),
        "services": {name: sh(["systemctl", "is-active", name])[1] for name in ("opsmcp", "labctl", "lab-update.timer")},
        "selftest": selftest_state if selftest_state["results"] or selftest_state["running"] else read_json(SELFTEST_FILE),
        "mcptest": mcptest_state if mcptest_state["report"] or mcptest_state["running"] else read_json(MCPTEST_FILE),
    }


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_mcptest():
    try:
        stderr = ""
        try:
            p = subprocess.run([VENV_PY, os.path.join(ROOT, "opsmcp", "selftest.py")], capture_output=True, text=True, timeout=900)
            stderr = p.stderr
            report = json.loads(p.stdout.strip().splitlines()[-1])
        except Exception as e:
            report = {"error": f"{type(e).__name__}: {e}", "stderr": stderr[-2000:]}
        record = {"running": False, "started": mcptest_state["started"], "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "report": report}
        with open(MCPTEST_FILE, "w") as f:
            json.dump(record, f, indent=1)
        mcptest_state.update(record)
    finally:
        mcptest_state["running"] = False
        mcptest_lock.release()


def start_mcptest():
    if selftest_state["running"] or not mcptest_lock.acquire(blocking=False):
        return False
    mcptest_state.update({"running": True, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "report": None})
    threading.Thread(target=run_mcptest, daemon=True).start()
    return True


def run_selftest():
    try:
        _selftest()
    finally:
        selftest_state["running"] = False
        selftest_lock.release()


def _selftest():
    results = []
    sh(["bash", FAULT, "restore-all"])
    time.sleep(4)
    baseline = synthetic()
    results.append({"fault": "baseline", "broken": None, "recovered": baseline.get("verdict") == "PASSED", "before": baseline})
    _, names = sh(["bash", FAULT, "list"])
    for name in [n for n in names.splitlines() if n]:
        code, out = sh(["bash", FAULT, name, "inject"], timeout=180)
        time.sleep(5)
        broken = synthetic()
        sh(["bash", FAULT, name, "restore"], timeout=120)
        time.sleep(6)
        after = synthetic()
        results.append({
            "fault": name,
            "injectOutput": out[-300:],
            "broken": broken.get("verdict") != "PASSED",
            "brokenCheck": broken,
            "recovered": after.get("verdict") == "PASSED",
            "afterCheck": after,
        })
    sh(["bash", FAULT, "restore-all"])
    record = {"running": False, "started": selftest_state["started"], "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": results}
    os.makedirs(os.path.dirname(SELFTEST_FILE), exist_ok=True)
    with open(SELFTEST_FILE, "w") as f:
        json.dump(record, f, indent=1)
    selftest_state.update(record)


def start_selftest():
    if mcptest_state["running"] or not selftest_lock.acquire(blocking=False):
        return False
    selftest_state.update({"running": True, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": None})
    threading.Thread(target=run_selftest, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/status":
            return self.send(200, status())
        if self.path == "/api/selftest":
            started = start_selftest()
            return self.send(202 if started else 409, {"started": started, "note": "poll /api/status for results"})
        if self.path == "/api/mcptest":
            started = start_mcptest()
            return self.send(202 if started else 409, {"started": started, "note": "poll /api/status for results"})
        if self.path == "/":
            s = status()
            rows = "".join(
                f"<tr><td>{html.escape(str(c.get('service')))}</td><td>{html.escape(str(c.get('state')))}</td><td>{html.escape(str(c.get('status')))}</td></tr>"
                for c in s["containers"]
            )
            steps = "".join(
                f"<li>{html.escape(x['result'])} {html.escape(x['step'])}: {html.escape(x['detail'])}</li>"
                for x in s["synthetic"].get("steps", [])
            )
            page = f"""<!doctype html><meta charset=utf-8><title>Agent Ops Lab</title>
<body style="font:15px system-ui;background:#0f1012;color:#ececee;max-width:52rem;margin:2rem auto;padding:0 1rem">
<h1>Agent Ops Lab</h1><p>Deployed: {html.escape(s['commit'])}</p>
<h2>Synthetic check: {html.escape(s['synthetic'].get('verdict', '?'))}</h2><ul>{steps}</ul>
<h2>Containers</h2><table>{rows}</table>
<h2>Active faults</h2><p>{html.escape(', '.join(s['faults']) or 'none')}</p>
<h2>Last deploy</h2><pre>{html.escape(chr(10).join(s['deployLog']))}</pre></body>"""
            return self.send(200, page, "text/html; charset=utf-8")
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "80"))), Handler).serve_forever()
