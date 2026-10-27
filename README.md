# Agent Ops Lab

An AI agent that fixes **real** broken servers. A small CRM stack runs in Docker on a live Ubuntu server — load balancer, two app servers, PostgreSQL, DNS and an office client — and real faults are injected into it: a full disk, a crashed process, a bad DNS record, an expired certificate, exhausted database connections. The agent connects through an MCP server with an allowlist, approvals and an audit log, finds the cause from real logs and commands, and fixes it.

Built by [Anthony Jaimez](https://portfolio-eosin-delta-24.vercel.app/).

> Work in progress. Stages 1 and 2 are live: the real lab with fault injection, and the MCP server the agent works through. The agent, web UI and benchmark come next.

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

## Operating the server

- `deploy/bootstrap.sh` sets up a fresh Ubuntu 24.04 droplet once.
- `lab-update.timer` checks `main` every minute and redeploys new commits with every fault restored.
- `labctl` on port 80 shows status (`/api/status`) and runs a self-test (`/api/selftest`) that injects each fault, confirms the CRM breaks, restores it and confirms it recovers.
- `opsmcp.service` runs the MCP server; its tokens are generated on the server in `/etc/agent-ops-lab` and never leave it.
