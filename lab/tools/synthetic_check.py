"""Synthetic check: walks a test user through the CRM end to end and reports
the first step that fails. Run inside the client container."""

import http.client
import json
import socket
import ssl
import sys
import time

HOST = "crm.lab.internal"
steps = []


def step(name, state, detail):
    steps.append({"step": name, "result": state, "detail": detail})
    return state != "FAIL"


def run():
    try:
        ip = socket.getaddrinfo(HOST, 443, socket.AF_INET)[0][4][0]
    except socket.gaierror as e:
        return step(f"Resolve {HOST}", "FAIL", f"DNS lookup failed: {e}")
    step(f"Resolve {HOST}", "PASS", ip)

    try:
        sock = socket.create_connection((ip, 443), timeout=3)
    except OSError as e:
        return step("Connect to port 443", "FAIL", f"TCP connect to {ip}:443 failed: {e}")
    step("Connect to port 443", "PASS", "connected")

    ctx = ssl.create_default_context(cafile="/certs/ca.crt")
    try:
        tls = ctx.wrap_socket(sock, server_hostname=HOST)
        tls.close()
    except ssl.SSLCertVerificationError as e:
        return step("TLS handshake", "FAIL", e.verify_message or str(e))
    except (ssl.SSLError, OSError) as e:
        return step("TLS handshake", "FAIL", str(e))
    step("TLS handshake", "PASS", "certificate valid")

    def request(method, path, body=None):
        conn = http.client.HTTPSConnection(HOST, 443, timeout=6, context=ctx)
        headers = {"Content-Type": "application/json"} if body else {}
        start = time.monotonic()
        conn.request(method, path, body=json.dumps(body) if body else None, headers=headers)
        resp = conn.getresponse()
        resp.read()
        ms = int((time.monotonic() - start) * 1000)
        return resp.status, resp.getheader("X-Served-By") or "-", ms

    results = []
    for _ in range(4):
        try:
            results.append(request("GET", "/customers"))
        except Exception:
            results.append((0, "-", 0))
    bad = [r for r in results if r[0] != 200]
    summary = ", ".join(f"{s} from {srv}" if s else "timeout" for s, srv, _ in results)
    if len(bad) == len(results):
        return step("Load customer list", "FAIL", summary)
    step("Load customer list", "DEGRADED" if bad else "PASS", summary)

    try:
        status, srv, ms = request("POST", "/customers", {"name": "Synthetic Check", "company": "Monitoring", "cleanup": True})
    except Exception as e:
        return step("Save a test record", "FAIL", str(e))
    if status == 201:
        step("Save a test record", "PASS", f"201 from {srv} in {ms} ms")
    else:
        step("Save a test record", "FAIL", f"{status} from {srv}")
    return True


run()
failed = next((s for s in steps if s["result"] == "FAIL"), None)
degraded = any(s["result"] == "DEGRADED" for s in steps)
verdict = "FAILED" if failed else "DEGRADED" if degraded else "PASSED"

if "--json" in sys.argv:
    print(json.dumps({"verdict": verdict, "steps": steps}))
else:
    print("Synthetic check from office-pc (172.30.0.100)")
    for s in steps:
        print(f"{s['result']:<9}{s['step']}: {s['detail']}")
    print(f"Result: {verdict}" + (f' at "{failed["step"]}"' if failed else ""))
