"""Visit counter for my portfolio (www.anthonyjaimez.com).

The portfolio page sends small beacons here: one when it opens, one when the
visitor leaves (time on page, how far they scrolled) and one per click on a
link that matters (live demo, resume, GitHub, LinkedIn, email).

What is stored per event: time, city, region and country, the name of the
network the visit came from (from the IP's autonomous system), the referrer
domain, an optional ?r= tag, device type, browser and OS. The IP address
itself is never stored: it is looked up, then kept only as a salted hash so
repeat visits can be counted.

Routes (behind Caddy on the lab droplet):
  POST /t             beacon from the portfolio page
  GET  /visits        the dashboard page (data needs the dashboard key)
  GET  /api/visits    dashboard data, header X-Dashboard-Key
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("VISITS_DATA", "/var/lib/agent-ops-lab/visits")
GEO_DIR = os.environ.get("GEO_DIR", "/var/lib/agent-ops-lab/geoip")
KEY_HASH = open(os.path.join(HERE, "dashboard.sha256")).read().strip()
os.makedirs(DATA, exist_ok=True)

# Origins allowed to send beacons: the portfolio and its Vercel previews.
ORIGIN_OK = re.compile(r"^https://((www\.)?anthonyjaimez\.com|portfolio-eosin-delta-24\.vercel\.app|portfolio-[a-z0-9-]+-gkitad\.vercel\.app)$")
PRODUCTION_HOSTS = {"www.anthonyjaimez.com", "anthonyjaimez.com", "portfolio-eosin-delta-24.vercel.app"}
BOT_UA = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|pingdom|uptime|monitor|curl|wget|python|httpclient", re.I)

# --- storage ---------------------------------------------------------------

db = sqlite3.connect(os.path.join(DATA, "visits.db"), check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.execute(
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,          -- view | leave | click
        session TEXT NOT NULL,
        visitor TEXT NOT NULL,       -- salted hash of the IP
        path TEXT, referrer TEXT, tag TEXT, target TEXT,
        seconds INTEGER, scroll INTEGER,
        city TEXT, region TEXT, country TEXT, network TEXT, network_kind TEXT,
        device TEXT, browser TEXT, os TEXT, preview INTEGER DEFAULT 0
    )"""
)
db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
db.execute("CREATE INDEX IF NOT EXISTS events_session ON events(session)")


def salt() -> bytes:
    path = os.path.join(DATA, "salt")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(os.urandom(32))
        os.chmod(path, 0o600)
    return open(path, "rb").read()


SALT = salt()

# --- IP lookups (DB-IP Lite databases, downloaded by deploy.sh) ----------------

try:
    import maxminddb
except ImportError:  # the service still counts visits without geo data
    maxminddb = None


def open_geo(name: str):
    path = os.path.join(GEO_DIR, name)
    if maxminddb and os.path.exists(path):
        try:
            return maxminddb.open_database(path)
        except Exception:
            return None
    return None


CITY = open_geo("city.mmdb")
ASN = open_geo("asn.mmdb")

HOME_ISP = re.compile(
    r"comcast|charter|spectrum|at&t|att-|verizon|t-mobile|tmobile|cox |cox-|frontier|centurylink|lumen|windstream|"
    r"mediacom|altice|optimum|suddenlink|rcn|wow|astound|cable|broadband|telecom|telefonica|movistar|claro|vodafone|"
    r"orange|telus|rogers|bell canada|shaw|starlink|mobile|wireless|dsl|fiber|fibre|google fiber|cellular",
    re.I,
)
CLOUD = re.compile(
    r"amazon|aws|google cloud|google-cloud|googl|microsoft azure|azure|digitalocean|linode|akamai|ovh|hetzner|vultr|"
    r"oracle cloud|cloudflare|fastly|leaseweb|choopa|contabo|scaleway|alibaba|tencent|m247|datacamp|hosting|server|data center|datacenter",
    re.I,
)


def lookup(ip: str) -> dict:
    out = {"city": None, "region": None, "country": None, "network": None, "network_kind": None}
    try:
        if CITY:
            c = CITY.get(ip) or {}
            out["city"] = (c.get("city") or {}).get("names", {}).get("en")
            subs = c.get("subdivisions") or []
            out["region"] = subs[0].get("names", {}).get("en") if subs else None
            out["country"] = (c.get("country") or {}).get("names", {}).get("en")
        if ASN:
            a = ASN.get(ip) or {}
            out["network"] = a.get("autonomous_system_organization")
    except Exception:
        pass
    n = out["network"] or ""
    if n:
        out["network_kind"] = "cloud" if CLOUD.search(n) else "home" if HOME_ISP.search(n) else "company"
    return out


def device(ua: str) -> tuple[str, str, str]:
    kind = "Tablet" if re.search(r"ipad|tablet", ua, re.I) else "Phone" if re.search(r"mobi|iphone|android", ua, re.I) else "Desktop"
    browser = next((b for p, b in [(r"edg/", "Edge"), (r"opr/|opera", "Opera"), (r"chrome/|crios/", "Chrome"), (r"firefox/|fxios/", "Firefox"), (r"safari/", "Safari")] if re.search(p, ua, re.I)), "Other")
    os_ = next((o for p, o in [(r"iphone|ipad|ios", "iOS"), (r"android", "Android"), (r"windows", "Windows"), (r"mac os|macintosh", "macOS"), (r"linux", "Linux"), (r"cros", "ChromeOS")] if re.search(p, ua, re.I)), "Other")
    return kind, browser, os_


def client_ip(request: Request) -> str:
    # Caddy runs on the same host and appends the real client to X-Forwarded-For.
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[-1].strip()
    return request.client.host if request.client else ""


def clip(v, n=200):
    return str(v)[:n] if v not in (None, "") else None


def ref_domain(ref: str | None) -> str | None:
    if not ref:
        return None
    host = urlparse(ref).hostname or ""
    host = host.removeprefix("www.")
    if (host.endswith("vercel.app") and "portfolio" in host) or host == "anthonyjaimez.com":
        return None  # moving around the site itself
    return {"lnkd.in": "linkedin.com", "com.linkedin.android": "linkedin.com", "t.co": "x.com"}.get(host, host) or None


recent: dict[str, deque] = defaultdict(deque)


async def beacon(request: Request) -> Response:
    if not ORIGIN_OK.match(request.headers.get("origin", "")):
        return Response(status_code=204)
    ua = request.headers.get("user-agent", "")
    if BOT_UA.search(ua):
        return Response(status_code=204)
    ip = client_ip(request)
    now = time.time()
    q = recent[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 30:
        return Response(status_code=204)
    q.append(now)
    try:
        body = json.loads((await request.body())[:4000] or b"{}")
    except Exception:
        return Response(status_code=204)
    kind = body.get("k")
    session = clip(body.get("s"), 40)
    if kind not in ("view", "leave", "click") or not session:
        return Response(status_code=204)
    geo = lookup(ip) if kind == "view" else {}
    dev, browser, os_ = device(ua)
    host = urlparse(request.headers.get("origin", "")).hostname or ""
    db.execute(
        "INSERT INTO events (ts, kind, session, visitor, path, referrer, tag, target, seconds, scroll, city, region, country,"
        " network, network_kind, device, browser, os, preview) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            now, kind, session, hmac.new(SALT, ip.encode(), hashlib.sha256).hexdigest()[:16],
            clip(body.get("p")), ref_domain(clip(body.get("r"), 500)), clip(body.get("t"), 60), clip(body.get("x"), 80),
            int(body["sec"]) if str(body.get("sec", "")).isdigit() else None,
            int(body["sc"]) if str(body.get("sc", "")).isdigit() else None,
            geo.get("city"), geo.get("region"), geo.get("country"), geo.get("network"), geo.get("network_kind"),
            dev, browser, os_, 0 if host in PRODUCTION_HOSTS else 1,
        ),
    )
    return Response(status_code=204)


# --- dashboard ---------------------------------------------------------------


def key_ok(request: Request) -> bool:
    k = request.headers.get("x-dashboard-key", "")
    return bool(k) and hmac.compare_digest(hashlib.sha256(k.encode()).hexdigest(), KEY_HASH)


def sessions_since(since: float, previews: bool) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM events WHERE ts >= ? AND session IN (SELECT session FROM events WHERE kind='view' AND ts >= ?) ORDER BY ts",
        (since, since),
    ).fetchall()
    cols = [d[0] for d in db.execute("SELECT * FROM events LIMIT 0").description]
    by: dict[str, dict] = {}
    for r in rows:
        e = dict(zip(cols, r))
        s = by.get(e["session"])
        if e["kind"] == "view":
            if s is None:
                s = by[e["session"]] = {
                    "start": e["ts"], "visitor": e["visitor"], "city": e["city"], "region": e["region"], "country": e["country"],
                    "network": e["network"], "networkKind": e["network_kind"], "source": e["tag"] and f"tag: {e['tag']}" or e["referrer"] or "Direct",
                    "tag": e["tag"], "referrer": e["referrer"], "device": e["device"], "browser": e["browser"], "os": e["os"],
                    "preview": bool(e["preview"]), "seconds": 0, "scroll": 0, "clicks": [],
                }
        elif s is not None:
            if e["kind"] == "leave":
                s["seconds"] = max(s["seconds"], e["seconds"] or 0)
                s["scroll"] = max(s["scroll"], e["scroll"] or 0)
            elif e["kind"] == "click" and e["target"] and e["target"] not in s["clicks"]:
                s["clicks"].append(e["target"])
    out = [s for s in by.values() if previews or not s["preview"]]
    # A repeat visitor is someone whose hashed IP shows up in more than one session, ever.
    counts = dict(db.execute("SELECT visitor, COUNT(DISTINCT session) FROM events WHERE kind='view' GROUP BY visitor").fetchall())
    for s in out:
        s["visitsByThisVisitor"] = counts.get(s["visitor"], 1)
    return out


async def data(request: Request) -> JSONResponse:
    if not key_ok(request):
        return JSONResponse({"error": "wrong or missing key"}, status_code=401)
    days = {"1": 1, "7": 7, "30": 30, "90": 90, "365": 365}.get(request.query_params.get("days", "30"), 30)
    previews = request.query_params.get("previews") == "1"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    sessions = sessions_since(since, previews)
    return JSONResponse({
        "days": days,
        "geo": bool(CITY), "network": bool(ASN),
        "sessions": [{k: v for k, v in s.items() if k != "visitor"} | {"visitor": s["visitor"][:6]} for s in reversed(sessions)],
    })


async def page(request: Request) -> FileResponse:
    return FileResponse(os.path.join(HERE, "dashboard.html"), headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


app = Starlette(routes=[
    Route("/t", beacon, methods=["POST"]),
    Route("/visits", page),
    Route("/api/visits", data),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8095")), log_level="warning", proxy_headers=False)
