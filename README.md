# Agent Ops Lab

An AI agent that fixes **real** broken servers. A small CRM stack runs in Docker on a live Ubuntu server — load balancer, two app servers, PostgreSQL, DNS and an office client — and real faults are injected into it: a full disk, a crashed process, a bad DNS record, an expired certificate, exhausted database connections. The agent connects through an MCP server with an allowlist, approvals and an audit log, finds the cause from real logs and commands, and fixes it.

Built by [Anthony Jaimez](https://portfolio-eosin-delta-24.vercel.app/).

**Live:** https://138-197-164-83.sslip.io (pick a fault, watch Claude work it, approve or deny its changes)

> Stages 1-3 are live: the real lab, the MCP server, and the agent with its web page. A benchmark across Claude models and prompt-injection traps come next.

## The lab

| Node | Container | What it runs |
| --- | --- | --- |
| lb-01 | `edge` | nginx, TLS for `crm.lab.internal`, balancing across both app servers |
| crm-app-01, crm-app-02 | `app1`, `app2` | The CRM API (Python), reads and writes customers |
| db-01 | `db` | PostgreSQL 16 |
| dns-01 | `dns` | CoreDNS, authoritative for `lab.internal` |
| office-pc | `client` | Where synthetic checks run, the way a user would hit the CRM |

## Faults

`lab/faults/fault.sh <fault> inject|restore`

| Fault | What actually happens |
| --- | --- |
| `disk-full` | A runaway debug log fills the storage both app servers write audit records to; saves fail |
| `app-crash` | crm-app-01's process is killed and stays down; half the requests time out (504) |
| `dns-stale` | `crm.lab.internal` is repointed to a retired address |
| `cert-expired` | lb-01 starts serving an expired certificate |
| `db-connections` | A new sales export job holds every non-reserved database connection; the app servers can't connect |

Each fault also writes a realistic trigger and a red herring to the change log.

## The MCP server

`opsmcp/` is the only way the agent can touch the lab. It speaks MCP over streamable HTTP ([MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.3) and listens on `127.0.0.1` only.

**Read-only tools** run immediately:

| Tool | What it does |
| --- | --- |
| `list_hosts` | Inventory with roles, IPs and live state (running, or stopped with exit code) |
| `check_service` | The synthetic user check from office-pc, first failing step highlighted |
| `run_command` | One allowlisted diagnostic command on one host |
| `read_logs` | A server's log, filtered by text or time |
| `recent_changes` | The change log, to correlate with when the problem started |

**Change tools** each create an approval request and run only once it is approved: `restart_service`, `delete_file`, `update_dns_record`, `renew_certificate`, `reload_load_balancer`, `terminate_db_sessions`. The agent has to say why in `reason`, which the approver sees.

### Guardrails

- **No shell.** Commands are parsed into an argv, checked flag by flag against an allowlist (`opsmcp/policy.py`) and run with `docker exec` directly. Pipes, redirects, chaining and command substitution are rejected with a message that tells the agent what to do instead.
- **No secrets.** Private keys, `/proc/*/environ`, `env` and similar are blocked; output is redacted for connection strings, passwords and PEM keys before the model sees it.
- **Least privilege underneath.** Database queries run as `ops_readonly`, a read-only Postgres role that can see every session and use reserved connections, so diagnostics still work when the pool is exhausted. It cannot end sessions; only the approved `terminate_db_sessions` tool can.
- **Policy on changes too.** Audit records can never be deleted, DNS can't point outside the lab network, and session termination needs a filter and never touches administrative sessions.
- **The agent can't approve itself.** The MCP endpoint and the operator API use different tokens. Each run is registered by the operator with an approval mode: `human` (each change waits for a person), `auto` (approved by policy, used by the benchmark) or `deny` (dry run), plus tool-call and change budgets.
- **Everything is audited.** Every call, allowed, blocked, approved, denied or executed, is appended to a JSON-lines audit log, readable per run through the operator API.

### Tests

- `opsmcp/test_policy.py`: 68 allow/deny cases for the command policy, run on every deploy; a failure stops the deploy.
- `opsmcp/selftest.py`: 48 end-to-end checks over real MCP against the live lab, covering auth, policy, all three approval modes, budgets, the audit log, and fixing each of the five faults through the change tools. Trigger it with `/api/mcptest`.

## The agent

`opsagent/agent.py` is Claude (Sonnet 5.5 by default) working the incident through the MCP tools. It starts with only the users' complaint and is never told which fault was injected. It narrates each step in one sentence, makes the smallest change the evidence supports, re-checks, and ends by calling `submit_report` (strict schema: root cause, evidence, fix, verification, follow-ups, and any instructions it found in data and ignored). It runs one tool call at a time, with prompt caching, a 40-step cap and a 15-minute cap.

`opsagent/service.py` is the public incident API behind Caddy:

- `POST /api/incidents` injects a real fault (random or chosen), registers a run on the MCP server and starts the agent. It returns an approver key, so only the person who started the incident can approve its changes.
- `GET /api/incidents/{id}/events` streams the whole timeline over Server-Sent Events, replayed and then live, so anyone can watch or replay an incident by link.
- One incident at a time (the lab is one shared environment), with per-IP and daily limits.
- **Graded by the lab, not the agent.** After the agent finishes, the service runs the user check itself. An incident counts as resolved only if users saw the outage before the agent started and the check passes after. Then every fault is restored so the next visitor starts clean.

### First live results (Claude Sonnet 5.5, policy approval)

| Fault | Result | Steps | Fix |
| --- | --- | --- | --- |
| Bad DNS record | Resolved | 6 | Repointed `crm.lab.internal` to lb-01 |
| Expired certificate | Resolved | 7 | Renewed the certificate from the internal CA, reloaded nginx |
| App server crash | Resolved | 9 | Started crm-app-01 |
| Disk full | Resolved | 11 | Deleted the runaway debug log; audit records untouched |
| Database out of connections | Found and fixed, then retested | 11 | Ended only the 33 export-job sessions |

Two lab bugs found along the way, both by watching the agent: a DNS reset and injection could land in the same second with the same SOA serial, so CoreDNS ignored the fault (the agent flagged that the zone file and live answers disagreed), and the database fault could leave two connections free. Both are fixed.

## The web page

`web/` is a static Next.js app, served by the lab's Caddy (and deployable to Vercel as is). It shows the live lab state, lets a visitor start an incident with human or policy approval, renders the agent's timeline as it happens (commands, blocked attempts, approval cards with an optional note for the agent), and ends with the lab's grade and the agent's report, which can be copied as Markdown.

## Operating the server

- `deploy/bootstrap.sh` sets up a fresh Ubuntu 24.04 droplet once.
- `lab-update.timer` checks `main` every minute and redeploys new commits with every fault restored.
- `labctl` (behind Caddy at `/status` and `/api/*`) shows status (`/api/status`) and runs a self-test (`/api/selftest`) that injects each fault, confirms the CRM breaks, restores it and confirms it recovers.
- `opsmcp.service` runs the MCP server and `opsagent.service` the incident API; tokens are generated on the server in `/etc/agent-ops-lab` and never leave it. The Anthropic API key is placed there by hand.
- Caddy (`deploy/public/Caddyfile.template`) gets a Let's Encrypt certificate for `<ip>.sslip.io`, so HTTPS works without buying a domain. If it fails to start, the deploy falls back to serving the status page on port 80.
