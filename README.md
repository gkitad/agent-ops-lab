# Agent Ops Lab

An AI agent that fixes **real** broken servers. A small CRM stack runs in Docker on a live Ubuntu server — load balancer, two app servers, PostgreSQL, DNS and an office client — and real faults are injected into it: a full disk, a crashed process, a bad DNS record, an expired certificate, exhausted database connections. The agent connects through an MCP server with an allowlist, approvals and an audit log, finds the cause from real logs and commands, and fixes it.

Built by [Anthony Jaimez](https://portfolio-eosin-delta-24.vercel.app/).

> Work in progress. Stage 1 (the real lab, fault injection and self-test) is in place; the MCP server, agent and benchmark come next.

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
| `app-crash` | crm-app-01's process is killed and stays down; half the requests return 502 |
| `dns-stale` | `crm.lab.internal` is repointed to a retired address |
| `cert-expired` | lb-01 starts serving an expired certificate |
| `db-connections` | A leaked reporting job holds every database connection |

Each fault also writes a realistic trigger and a red herring to the change log.

## Operating the server

- `deploy/bootstrap.sh` sets up a fresh Ubuntu 24.04 droplet once.
- `lab-update.timer` checks `main` every minute and redeploys new commits with every fault restored.
- `labctl` on port 80 shows status (`/api/status`) and runs a self-test (`/api/selftest`) that injects each fault, confirms the CRM breaks, restores it and confirms it recovers.
