"""CRM API: a deliberately small, real web service backed by PostgreSQL."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

NAME = os.environ.get("APP_NAME", "crm-app")
DSN = os.environ["DATABASE_URL"]
VERSION = "4.12.0"
AUDIT_DIR = "/var/lib/crm/audit"


def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} {level} crm-api[{NAME}]: {msg}", flush=True)


def audit(event):
    """Every change is recorded on shared storage before it's committed."""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} {NAME} {event}\n"
    with open(os.path.join(AUDIT_DIR, "audit.log"), "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def db():
    return psycopg.connect(DSN, connect_timeout=3)


class Handler(BaseHTTPRequestHandler):
    server_version = f"crm-api/{VERSION}"

    def log_message(self, fmt, *args):  # replaced by our own access log
        pass

    def send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Served-By", NAME)
        self.end_headers()
        self.wfile.write(data)

    def timed(self, fn):
        start = time.monotonic()
        status = 500
        try:
            status = fn()
        finally:
            ms = int((time.monotonic() - start) * 1000)
            level = "INFO" if status < 500 else "ERROR"
            log(level, f'{self.command} {self.path} {status} {ms}ms')

    def do_GET(self):
        self.timed(self.get)

    def do_POST(self):
        self.timed(self.post)

    def get(self):
        if self.path == "/health":
            try:
                with db() as conn:
                    conn.execute("SELECT 1")
                self.send(200, {"status": "ok", "server": NAME, "version": VERSION})
                return 200
            except Exception as e:
                log("ERROR", f"health check failed: {e}")
                self.send(503, {"status": "unhealthy", "server": NAME})
                return 503
        if self.path in ("/", "/customers"):
            try:
                with db() as conn:
                    rows = conn.execute("SELECT id, name, company FROM customers ORDER BY id DESC LIMIT 20").fetchall()
                self.send(200, {"server": NAME, "customers": [{"id": r[0], "name": r[1], "company": r[2]} for r in rows]})
                return 200
            except Exception as e:
                log("ERROR", f"could not read customers: {str(e).strip()}")
                self.send(500, {"error": "Internal Server Error", "server": NAME})
                return 500
        self.send(404, {"error": "Not Found"})
        return 404

    def post(self):
        if self.path != "/customers":
            self.send(404, {"error": "Not Found"})
            return 404
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            try:
                audit(f"create customer name={str(body.get('name', ''))[:40]!r}")
            except OSError as e:
                log("ERROR", f"could not write audit record to {AUDIT_DIR}: {e}")
                self.send(500, {"error": "Internal Server Error", "server": NAME})
                return 500
            with db() as conn:
                row = conn.execute(
                    "INSERT INTO customers (name, company) VALUES (%s, %s) RETURNING id",
                    (str(body.get("name", "Test User"))[:80], str(body.get("company", "Test Co"))[:80]),
                ).fetchone()
                if body.get("cleanup"):
                    conn.execute("DELETE FROM customers WHERE id = %s", (row[0],))
            self.send(201, {"id": row[0], "server": NAME})
            return 201
        except Exception as e:
            log("ERROR", f"could not write to database: {str(e).strip()}")
            self.send(500, {"error": "Internal Server Error", "server": NAME})
            return 500


if __name__ == "__main__":
    log("INFO", f"starting crm-api {VERSION} on :8080")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
