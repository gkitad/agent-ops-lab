"""Policy tests: python3 opsmcp/test_policy.py"""

import sys

from policy import Denied, plan, redact

ALLOW = [
    ("office-pc", "dig +short crm.lab.internal"),
    ("office-pc", "dig @172.30.0.53 crm.lab.internal A"),
    ("office-pc", "curl -skv https://crm.lab.internal/health"),
    ("office-pc", "curl -sI --max-time 5 https://crm.lab.internal/customers"),
    ("office-pc", "openssl s_client -connect crm.lab.internal:443 -servername crm.lab.internal"),
    ("office-pc", "openssl x509 -in /certs/live/crm.crt -noout -dates"),
    ("office-pc", "ping -c 3 172.30.0.40"),
    ("office-pc", "getent hosts crm.lab.internal"),
    ("office-pc", "cat /etc/resolv.conf"),
    ("crm-app-02", "df -h /var/lib/crm"),
    ("crm-app-02", "du -sh /var/lib/crm/audit"),
    ("crm-app-02", "du -ah --max-depth=1 /var/lib/crm"),
    ("crm-app-02", "ls -lah /var/lib/crm"),
    ("crm-app-02", "find /var/lib/crm -maxdepth 1 -type f -size +1M"),
    ("crm-app-02", "tail -n 50 /var/lib/crm/debug-export-trace.log"),
    ("crm-app-02", "head -20 /etc/hosts"),
    ("crm-app-01", "grep -rn error /var/log"),
    ("crm-app-01", "grep -E 'error|warn' /var/log/app.log"),
    ("crm-app-01", "ps aux"),
    ("crm-app-01", "free -m"),
    ("db-01", 'psql -c "SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1"'),
    ("db-01", "psql -U crm -d crm -c 'SHOW max_connections;'"),
    ("db-01", "psql -x -c \"SELECT pid, state, left(query, 60) FROM pg_stat_activity WHERE query LIKE '%export%'\""),
    ("lb-01", "nginx -T"),
    ("lb-01", "cat /etc/nginx/nginx.conf"),
    ("dns-01", "cat /zones/lab.internal.db"),
]

DENY = [
    ("crm-app-01", "cat /proc/1/environ"),
    ("crm-app-01", "cat /proc/self/environ"),
    ("crm-app-01", "cat proc/1/../1/environ"),
    ("crm-app-01", "env"),
    ("crm-app-01", "/usr/bin/printenv"),
    ("lb-01", "cat /certs/live/crm.key"),
    ("lb-01", "cat /certs/ca.key"),
    ("crm-app-01", "rm -rf /var/lib/crm"),
    ("crm-app-01", "ls /var/lib/crm | head"),
    ("crm-app-01", "ls /var/lib/crm; rm -rf /"),
    ("crm-app-01", "ls /var/lib/crm && rm x"),
    ("crm-app-01", "cat $(echo /etc/passwd)"),
    ("crm-app-01", "cat `id`"),
    ("crm-app-01", "echo hi > /tmp/x"),
    ("crm-app-01", "find /var/lib/crm -name '*.log' -delete"),
    ("crm-app-01", "find / -exec sh -c id ;"),
    ("crm-app-01", "tail -f /var/log/app.log"),
    ("crm-app-01", "ps auxe"),
    ("crm-app-01", "grep -f /certs/ca.key /etc/hosts"),
    ("crm-app-01", "python3 -c 'print(1)'"),
    ("crm-app-01", "sh -c id"),
    ("crm-app-01", "psql -c 'SELECT 1'"),
    ("office-pc", "curl -d x=1 https://crm.lab.internal/customers"),
    ("office-pc", "curl -X POST https://crm.lab.internal/customers"),
    ("office-pc", "curl -o /tmp/x https://crm.lab.internal/"),
    ("office-pc", "curl file:///etc/passwd"),
    ("office-pc", "ping crm.lab.internal"),
    ("office-pc", "ping -c 100 crm.lab.internal"),
    ("office-pc", "dig -f /etc/passwd"),
    ("db-01", 'psql -c "SELECT pg_terminate_backend(123)"'),
    ("db-01", 'psql -c "DELETE FROM customers"'),
    ("db-01", 'psql -c "SELECT 1; DROP TABLE customers"'),
    ("db-01", "psql -c '\\! sh'"),
    ("db-01", "psql -f /tmp/x.sql"),
    ("db-01", 'psql -c "EXPLAIN ANALYZE DELETE FROM customers"'),
    ("db-01", 'psql -c "COPY customers TO PROGRAM \'id\'"'),
    ("db-01", 'psql -c "SELECT pg_read_file(\'/etc/passwd\')"'),
    ("lb-01", "nginx -s stop"),
    ("dns-01", "ls /zones"),
    ("dns-01", "cat /etc/passwd"),
    ("crm-app-01", "cat /root/.ssh/id_rsa"),
    ("crm-app-01", ""),
]

failures = 0
for host, cmd in ALLOW:
    try:
        p = plan(host, cmd)
    except Denied as e:
        failures += 1
        print(f"FAIL should allow  [{host}] {cmd}\n      -> {e}")
for host, cmd in DENY:
    try:
        p = plan(host, cmd)
        failures += 1
        print(f"FAIL should deny   [{host}] {cmd}\n      -> {p.argv}")
    except Denied:
        pass

assert plan("db-01", 'psql -c "SELECT 1"').argv[:4] == ["psql", "-U", "ops_readonly", "-d"]
assert "--max-time" in plan("office-pc", "curl -s https://crm.lab.internal/").argv
red = redact("DATABASE_URL=postgresql://crm_app:s3cret@172.30.0.21/crm\n-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nPOSTGRES_PASSWORD=hunter2")
assert "s3cret" not in red and "abc" not in red and "hunter2" not in red, red

print(f"{len(ALLOW)} allowed, {len(DENY)} denied, {failures} failures")
sys.exit(1 if failures else 0)
