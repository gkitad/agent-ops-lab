"""Command policy for run_command.

The agent may run read-only diagnostics, one program at a time, from an
allowlist. Every command is parsed into an argv and checked flag by flag; it is
then executed with `docker exec` directly, never through a shell, so quoting
tricks have nothing to escape into. A denial explains why, so the agent can
pick a different command instead of retrying the same one.

Defense in depth sits behind this file: database queries run as a read-only
Postgres role, changes go through separate approval-gated tools, and output is
redacted before the model sees it.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field


class Denied(Exception):
    """The command is not allowed. The message is shown to the agent."""


@dataclass
class Plan:
    argv: list[str]
    user: str = "0"
    env: dict[str, str] = field(default_factory=dict)
    virtual_file: str | None = None  # dns-01 has no shell tools; files are read from the host


CHAINING = {"|", "||", "&", "&&", ";", ";;", ">", ">>", "<", "<<", "<<<", "2>", "2>>", "2>&1", "&>", "|&"}
NO_ENV = {"env", "printenv", "set", "export", "declare"}
WRITERS = {"rm", "mv", "cp", "dd", "tee", "truncate", "chmod", "chown", "kill", "pkill", "killall", "reboot",
           "shutdown", "sh", "bash", "ash", "python", "python3", "perl", "vi", "vim", "nano", "sed", "awk",
           "touch", "mkdir", "rmdir", "ln", "docker", "systemctl", "service", "apt", "apt-get", "apk", "pip"}

SECRET_PATHS = [re.compile(p) for p in (
    r"\.key$", r"\.pem$", r"(^|/)\.env$", r"(^|/)\.pgpass$",
    r"^/proc/[^/]+/(environ|mem|maps|fd|fdinfo|auxv)(/|$)",
    r"^/etc/(shadow|gshadow)", r"^/root(/|$)", r"^/run/secrets(/|$)",
    r"^/var/lib/postgresql/data(/|$)",
)]

DANGEROUS_SQL = re.compile(
    r"\b(pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_read_file|pg_read_binary_file|pg_ls_\w+|"
    r"pg_stat_file|pg_sleep\w*|pg_rotate_logfile|pg_promote|pg_switch_wal|pg_advisory\w*|set_config|lo_\w+|"
    r"dblink\w*|query_to_xml\w*|copy|into|analyze)\b",
    re.I,
)

HOST_ONLY = {"psql": {"db-01"}, "nginx": {"lb-01"}}


def normpath(p: str) -> str:
    return posixpath.normpath(p if p.startswith("/") else "/" + p)


def check_path(p: str) -> str:
    n = normpath(p)
    for rx in SECRET_PATHS:
        if rx.search(n) or rx.search(p):
            raise Denied(f"{p} holds credentials or private keys, so it can't be read. Inspect metadata instead (ls -l, stat, openssl x509 for certificates).")
    return p


def as_int(value: str, flag: str, lo: int, hi: int) -> str:
    if not re.fullmatch(r"\d+", value or "") or not lo <= int(value) <= hi:
        raise Denied(f"{flag} needs a number between {lo} and {hi}.")
    return value


@dataclass
class Spec:
    short: str = ""                       # letters allowed alone or combined, e.g. -lah
    values: dict = field(default_factory=dict)  # flag -> validator(value, flag) for flags that take a value
    long: dict = field(default_factory=dict)    # --flag -> validator or None (no value)
    operands: str = "paths"               # "paths", "any", "none"
    min_operands: int = 0
    max_operands: int = 20
    digit_flag: bool = False              # head -20 / tail -50


def num(lo, hi):
    return lambda v, f: as_int(v, f, lo, hi)


def only(allowed: set, message: str):
    def validate(v, f):
        if v.upper() not in allowed:
            raise Denied(message)
        return v
    return validate


def text(v, f):
    if len(v) > 200:
        raise Denied(f"{f} value is too long.")
    return v


SPECS: dict[str, Spec] = {
    "cat": Spec(short="n", min_operands=1),
    "head": Spec(values={"-n": num(1, 2000), "-c": num(1, 65536)}, digit_flag=True, min_operands=1),
    "tail": Spec(values={"-n": num(1, 2000), "-c": num(1, 65536)}, digit_flag=True, min_operands=1),
    "ls": Spec(short="lahtrSRd1iF"),
    "stat": Spec(short="L", min_operands=1),
    "wc": Spec(short="lcwm", min_operands=1),
    "df": Spec(short="hiTPk"),
    "du": Spec(short="shacxk", values={"-d": num(0, 5)}, long={"--max-depth": num(0, 5)}),
    "free": Spec(short="mhgk", operands="none"),
    "uptime": Spec(operands="none"),
    "date": Spec(short="uR", operands="none"),
    "hostname": Spec(operands="none"),
    "whoami": Spec(operands="none"),
    "id": Spec(operands="none"),
    "ss": Spec(short="tulnpa", operands="none"),
    "netstat": Spec(short="tulnpa", operands="none"),
    "nslookup": Spec(operands="any", min_operands=1, max_operands=2, long={"-type": text, "-query": text}),
    "dig": Spec(operands="any", min_operands=1, max_operands=6, values={"-t": text, "-x": text, "-p": num(1, 65535)}),
}


def parse_flags(prog: str, tokens: list[str], spec: Spec) -> list[str]:
    operands: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            operands.extend(tokens[i + 1:])
            break
        if t.startswith("--") or (t.startswith("-") and "=" in t and t.split("=")[0] in spec.long):
            name, eq, value = t.partition("=")
            if name not in spec.long:
                raise Denied(f"{prog} {name} is not allowed here.")
            validator = spec.long[name]
            if validator is not None:
                if not eq:
                    i += 1
                    if i >= len(tokens):
                        raise Denied(f"{name} needs a value.")
                    value = tokens[i]
                validator(value, name)
        elif t.startswith("-") and len(t) > 1:
            if spec.digit_flag and re.fullmatch(r"-\d+", t):
                as_int(t[1:], "-N", 1, 2000)
            elif t in spec.values:
                i += 1
                if i >= len(tokens):
                    raise Denied(f"{t} needs a value.")
                spec.values[t](tokens[i], t)
            elif t[:2] in spec.values:
                spec.values[t[:2]](t[2:], t[:2])
            else:
                bad = [c for c in t[1:] if c not in spec.short]
                if bad:
                    raise Denied(f"{prog} -{bad[0]} is not allowed. Allowed flags: {', '.join('-' + c for c in spec.short) or 'none'}.")
        else:
            operands.append(t)
        i += 1
    if spec.operands == "none" and operands:
        raise Denied(f"{prog} takes no arguments here.")
    if len(operands) < spec.min_operands:
        raise Denied(f"{prog} needs at least {spec.min_operands} argument(s).")
    if len(operands) > spec.max_operands:
        raise Denied(f"{prog} accepts at most {spec.max_operands} argument(s) here.")
    if spec.operands == "paths":
        for p in operands:
            check_path(p)
    return operands


def check_grep(tokens):
    spec = Spec(short="inrcvEilFwHhosRxqL", values={"-m": num(1, 500), "-A": num(0, 50), "-B": num(0, 50), "-C": num(0, 50), "-e": text},
                long={"--include": text, "--exclude": text, "--color": text}, operands="any", max_operands=21)
    if any(t == "-f" or (t.startswith("-") and not t.startswith("--") and "f" in t[1:] and t not in spec.values) for t in tokens):
        raise Denied("grep -f (patterns from a file) is not allowed.")
    operands = parse_flags("grep", tokens, spec)
    has_e = any(t == "-e" or t.startswith("-e") and len(t) > 2 for t in tokens)
    paths = operands if has_e else operands[1:]
    if not has_e and not operands:
        raise Denied("grep needs a pattern and a file or directory.")
    for p in paths:
        check_path(p)


FIND_PREDICATES = {"-name": 1, "-iname": 1, "-type": 1, "-size": 1, "-mtime": 1, "-mmin": 1, "-newer": 1, "-maxdepth": 1,
                   "-mindepth": 1, "-path": 1, "-user": 1, "-empty": 0, "-print": 0, "-ls": 0, "-not": 0, "!": 0,
                   "-o": 0, "-a": 0, "(": 0, ")": 0, "-xdev": 0}


def check_find(tokens):
    i = 0
    while i < len(tokens) and not tokens[i].startswith("-") and tokens[i] not in ("!", "(", ")"):
        check_path(tokens[i])
        i += 1
    while i < len(tokens):
        t = tokens[i]
        if t not in FIND_PREDICATES:
            raise Denied(f"find {t} is not allowed. Use -name, -type, -size, -mtime, -mmin, -maxdepth and similar tests; actions like -exec and -delete are not available.")
        i += 1 + FIND_PREDICATES[t]
        if i > len(tokens):
            raise Denied(f"find {t} needs a value.")


def check_ps(tokens):
    if tokens not in ([], ["aux"], ["auxw"], ["auxww"], ["-ef"], ["-eF"], ["-e"], ["-A"], ["-eo", "pid,user,etime,args"]):
        raise Denied("ps accepts: ps, ps aux, ps -ef (process environment listings are not available).")


def check_ping(tokens):
    spec = Spec(values={"-c": num(1, 5), "-W": num(1, 5), "-w": num(1, 10)}, operands="any", min_operands=1, max_operands=1)
    parse_flags("ping", tokens, spec)
    if not any(t == "-c" or t.startswith("-c") for t in tokens):
        raise Denied("ping needs -c with a count of 1 to 5.")


def check_getent(tokens):
    if len(tokens) != 2 or tokens[0] not in ("hosts", "ahosts"):
        raise Denied("getent accepts: getent hosts <name>.")


def check_curl(tokens) -> list[str]:
    for t in tokens:
        if t.startswith("-") and not t.startswith("--") and len(t) > 2 and any(c in t[1:] for c in "dFTKuoXmHwe"):
            raise Denied(f"Combine curl flags that take values separately (got {t}).")
    spec = Spec(short="sSIikvLf", operands="any", min_operands=1, max_operands=3,
                values={"-m": num(1, 15), "-H": text, "-w": text, "-o": only({"/DEV/NULL"}, "curl -o only accepts /dev/null."),
                        "-X": only({"GET", "HEAD"}, "curl is read-only here: only GET and HEAD requests.")},
                long={"--max-time": num(1, 15), "--connect-timeout": num(1, 10), "--head": None, "--insecure": None, "--silent": None,
                      "--show-error": None, "--verbose": None, "--fail": None, "--resolve": text, "--cacert": lambda v, f: check_path(v),
                      "--header": text, "--write-out": text, "--location": None, "--include": None})
    for t in tokens:
        if t in ("-d", "-F", "-T", "-K", "-u") or t.startswith(("--data", "--form", "--upload", "--config", "--user", "--output", "--request")):
            raise Denied("curl is read-only here: no request bodies, uploads, output files or config files.")
    operands = parse_flags("curl", tokens, spec)
    for url in operands:
        if not re.match(r"https?://", url):
            raise Denied(f"curl URLs must start with http:// or https:// (got {url}).")
    if not any(t in ("-m", "--max-time") or t.startswith(("-m", "--max-time=")) for t in tokens):
        return ["--max-time", "10"]
    return []


def check_openssl(tokens):
    if not tokens:
        raise Denied("openssl needs a subcommand: s_client, x509, verify or version.")
    sub, rest = tokens[0], tokens[1:]
    if sub == "version":
        return
    allowed = {
        "s_client": ({"-showcerts", "-brief", "-verify_return_error"}, {"-connect", "-servername", "-CAfile"}),
        "x509": ({"-noout", "-dates", "-subject", "-issuer", "-text", "-enddate", "-startdate", "-serial", "-fingerprint"}, {"-in", "-ext"}),
        "verify": (set(), {"-CAfile"}),
    }
    if sub not in allowed:
        raise Denied("openssl accepts: s_client, x509, verify, version.")
    flags, valued = allowed[sub]
    i = 0
    operands = []
    while i < len(rest):
        t = rest[i]
        if t in valued:
            i += 1
            if i >= len(rest):
                raise Denied(f"openssl {t} needs a value.")
            if t in ("-in", "-CAfile"):
                check_path(rest[i])
        elif t in flags:
            pass
        elif t.startswith("-"):
            raise Denied(f"openssl {sub} {t} is not allowed.")
        else:
            operands.append(t)
        i += 1
    if sub == "x509" and "-in" not in rest:
        raise Denied("openssl x509 needs -in <certificate file>.")
    if sub == "s_client" and "-connect" not in rest:
        raise Denied("openssl s_client needs -connect host:port.")
    for p in operands:
        if sub != "verify":
            raise Denied(f"unexpected argument {p}.")
        check_path(p)


def check_nginx(tokens):
    if tokens not in (["-t"], ["-T"], ["-v"], ["-V"]):
        raise Denied("nginx accepts: nginx -t (test config) or nginx -T (print config).")


def check_ip(tokens):
    if not tokens or tokens[0] not in ("addr", "a", "route", "r", "link", "l", "neigh", "n") or len(tokens) > 4:
        raise Denied("ip accepts: ip addr, ip route, ip link, ip neigh.")
    if len(tokens) > 1 and tokens[1] not in ("show", "list") and tokens[1] != "dev":
        raise Denied("ip is read-only here: only show/list.")


def plan_psql(tokens) -> Plan:
    sql = None
    extra = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-c", "--command"):
            i += 1
            sql = tokens[i] if i < len(tokens) else None
        elif t.startswith("--command="):
            sql = t.split("=", 1)[1]
        elif t in ("-U", "-d", "-h", "-p", "--username", "--dbname", "--host", "--port"):
            i += 1  # connection settings are fixed by the server
        elif t.startswith(("-U", "-d")) and len(t) > 2:
            pass
        elif t in ("-x", "-t", "-A", "-X", "-q"):
            extra.append(t)
        else:
            raise Denied(f"psql {t} is not allowed. Use: psql -c \"SELECT ...\"")
        i += 1
    if not sql:
        raise Denied("psql needs a query: psql -c \"SELECT ...\"")
    q = sql.strip().rstrip(";").strip()
    if "\\" in q:
        raise Denied("psql meta-commands (backslash commands) are not available; query the catalog views instead, e.g. SELECT * FROM pg_stat_activity.")
    if ";" in q:
        raise Denied("One SQL statement at a time.")
    if not re.match(r"(?is)^(select|show|explain|with|table)\b", q):
        raise Denied("Only read-only queries (SELECT, SHOW, EXPLAIN) are allowed. Changes go through approval tools such as terminate_db_sessions.")
    m = DANGEROUS_SQL.search(q)
    if m:
        raise Denied(f"{m.group(1)} is not allowed in diagnostics queries. To end database sessions, use the terminate_db_sessions tool, which needs approval.")
    return Plan(argv=["psql", "-U", "ops_readonly", "-d", "crm", "-X", "-P", "pager=off", *extra, "-c", q], user="postgres")


def plan_dns(tokens) -> Plan:
    if len(tokens) == 2 and tokens[0] == "cat":
        p = normpath(tokens[1])
        if p.startswith("/zones/") or p == "/etc/coredns/Corefile":
            return Plan(argv=tokens, virtual_file=p)
    raise Denied("dns-01 runs a minimal CoreDNS image with no shell tools. Available: cat /zones/lab.internal.db, cat /etc/coredns/Corefile. "
                 "To test resolution, run dig from office-pc, e.g. dig @172.30.0.53 crm.lab.internal.")


def plan(host: str, command: str) -> Plan:
    """Turn the agent's command into an argv to exec, or raise Denied."""
    command = (command or "").strip()
    if not command:
        raise Denied("Empty command.")
    if len(command) > 500:
        raise Denied("Command is too long.")
    if "\n" in command or "\r" in command:
        raise Denied("One command at a time, on one line.")
    if "$(" in command or "`" in command:
        raise Denied("Command substitution is not supported; run one command at a time.")
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        raise Denied(f"Could not parse the command ({e}).")
    if not tokens:
        raise Denied("Empty command.")
    for t in tokens:
        if t in CHAINING or re.fullmatch(r"\d?>{1,2}.*|<.*", t):
            raise Denied("Pipes, redirects and chaining aren't supported; run one command at a time (grep, head and tail all take a file argument).")
    prog, args = posixpath.basename(tokens[0]), tokens[1:]
    if host == "dns-01":
        return plan_dns(tokens)
    if prog in NO_ENV:
        raise Denied("Environment variables hold credentials on these servers, so env/printenv are not available.")
    if prog in WRITERS:
        raise Denied(f"{prog} is not available: run_command is read-only. To change something, use the change tools "
                     "(restart_service, delete_file, update_dns_record, renew_certificate, reload_load_balancer, terminate_db_sessions); each needs approval.")
    if prog in HOST_ONLY and host not in HOST_ONLY[prog]:
        raise Denied(f"{prog} is only available on {', '.join(sorted(HOST_ONLY[prog]))}.")
    if prog == "psql":
        return plan_psql(args)
    extra: list[str] = []
    if prog in SPECS:
        parse_flags(prog, args, SPECS[prog])
    elif prog == "grep":
        check_grep(args)
    elif prog == "find":
        check_find(args)
    elif prog == "ps":
        check_ps(args)
    elif prog == "ping":
        check_ping(args)
    elif prog == "getent":
        check_getent(args)
    elif prog == "curl":
        extra = check_curl(args)
    elif prog == "openssl":
        check_openssl(args)
    elif prog == "nginx":
        check_nginx(args)
    elif prog == "ip":
        check_ip(args)
    else:
        raise Denied(f"{prog} is not on the allowlist. Available: {', '.join(sorted(ALLOWED))}.")
    return Plan(argv=[prog, *extra, *args])


ALLOWED = sorted(set(SPECS) | {"grep", "find", "ps", "ping", "getent", "curl", "openssl", "nginx", "ip", "psql"})

REDACTIONS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[private key redacted]"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API_KEY)[A-Z0-9_]*\s*[=:]\s*)\S+"), r"\1***"),
]


def redact(text: str) -> str:
    for rx, repl in REDACTIONS:
        text = rx.sub(repl, text)
    return text
