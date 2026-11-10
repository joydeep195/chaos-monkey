# Chaos Monkey: specification and plan

Chaos Monkey is a chaos engineering platform for regulated financial services. It does what Netflix's original Chaos Monkey does (randomly switch off servers in production during working hours so teams build services that survive it) and adds the controls a bank, payments company or broker needs before it can let such a tool near production: sign-in, roles, approvals, a tamper-evident audit trail, capacity and health checks, freeze windows and a kill switch.

Background research: [docs/RESEARCH.md](docs/RESEARCH.md).

## Who uses it

| Role | Can do |
|------|--------|
| Viewer | See schedules, results and the audit log |
| App owner | Opt their own apps in or out, set how often and how they are grouped, run dry runs |
| Approver | Approve or reject an app owner's request to go live (never their own) |
| Operator | Pull the kill switch, pause and resume |
| Admin | Manage global settings, freeze windows and integrations |

## Safety rules (these always apply)

1. **Off and leashed by default.** A fresh install terminates nothing. Every app starts in dry run, as in Chaos Monkey.
2. **Opt-in only.** Nothing is touched unless its app is enabled, its account or cluster is on the global allowlist, and it is not excluded.
3. **Two people to go live.** Switching an app from dry run to live needs approval from a second person with the Approver role.
4. **Never below capacity.** No kill if it would leave fewer healthy instances than the app's minimum, and on Kubernetes the cluster's own disruption budgets always win.
5. **Healthy before, recovered after.** Each kill is preceded by a health check and followed by a recovery watch. A failed recovery halts all chaos until a person resumes it.
6. **Stay out of the way.** No kills outside working hours, on holidays, in freeze windows, or while an incident is open.
7. **Hard limits.** One kill per group per minimum gap (Netflix's rule), a global cap on kills per day, and a global cap on kills running at once.
8. **Kill switch wins.** When the kill switch is on, nothing runs. It is checked immediately before every single kill, not just at scheduling time.
9. **Everything is recorded.** Every setting change, approval, scheduled kill, skipped kill (with the reason), termination and recovery goes into an append-only, hash-chained audit log.
10. **Termination only.** Like Netflix's Chaos Monkey 2.0, no shell access, no SSH, no arbitrary commands. Chaos Monkey's cloud permissions allow it to terminate opted-in targets and nothing else.

## How it works

```
                 ┌─────────────── Chaos Monkey service ───────────────┐
 People, CLI ───▶│ API (SSO sign-in, roles, approvals)                │
                 │   │                                                │
                 │   ▼                                                │
                 │ Scheduler ─▶ Executor ─▶ Safety guards ─▶ Driver ──┼──▶ Kubernetes / AWS
                 │ (daily,      (runs due    (all 12 must     (evict    │
                 │  Netflix's    kills)       say yes)         or       │
                 │  algorithm)                                 terminate)
                 │   │             │              │                    │
                 │   └─────────────┴──────────────┴──▶ Database + audit log
                 └──────────────────────────────────────────┬─────────┘
                                                            ▼
                                     Monitoring, Slack/Teams, incident tool
```

- **Scheduler**: each working day, for every enabled group, flips Netflix's weighted coin (chance = 1 / mean days between kills) and, on heads, picks a random time inside working hours.
- **Executor**: at each planned time, re-checks every safety guard, picks a random healthy target, and asks the driver to terminate it.
- **Safety guards** (each returns yes or no, with a reason that is logged): kill switch, master switch, app opted in, account allowlist, working hours and holidays, freeze window, open incident, minimum gap, daily cap, concurrency cap, group already degraded, capacity. Approval decides live or dry run; protected and excepted targets are never even listed as candidates.
- **Drivers**: Kubernetes (uses the eviction API, which respects disruption budgets), AWS EC2 and auto-scaling groups, and a simulator for local runs, demos and tests.
- **Store**: PostgreSQL in production, SQLite on a laptop. Database constraints enforce the minimum gap, so two copies of Chaos Monkey can never double up.

## Security model

- **Sign-in**: single sign-on through the company's identity provider (Okta, Microsoft Entra ID, Google) using OpenID Connect. Local development tokens work only in development mode and are refused in production.
- **Roles and approvals** as above; an approver can never approve their own request.
- **Least privilege in the cloud**: shipped Kubernetes and AWS permission policies allow only listing and terminating targets carrying Chaos Monkey's opt-in label or tag.
- **Secrets** come from environment variables or a secrets manager, never from config files or the database.
- **Audit log**: each record carries the hash of the one before it, so any edit or deletion is detectable. Verifiable from the CLI.
- **Hardening**: input validation on every request, rate limiting, strict security headers, TLS, no customer data stored.
- **Supply chain**: locked dependencies with hashes, dependency vulnerability scan, security lint, software bill of materials, and (from M4) a minimal non-root container image that is signed.

Compliance fit (for the customer's own assessment, not a certification): DORA resilience testing (EU), RBI IT governance and business-continuity expectations (India), PCI DSS logging and change control, SOC 2 change management.

## Milestones

| Milestone | Delivers | Can you try it on this PC? |
|-----------|----------|----------------------------|
| **M1 Engine and safety** (built 9 Nov 2026) | Configuration, Netflix's scheduling algorithm, all safety guards, simulator driver, database, hash-chained audit log, kill switch, CLI, tests (including a statistical test that the mean time between kills comes out right) | Yes, against a simulated fleet |
| **M2 Secure API** (built 9 Nov 2026) | HTTP API with single sign-on, roles, maker-checker approvals, rate limits, security headers | Yes |
| **M3 Real targets** | Kubernetes and AWS drivers, least-privilege permission files, incident-tool and monitoring checks, Slack/Teams and Prometheus reporting | Tested with mocks; needs a cluster or AWS account for a live run |
| **M4 Ship it** | Hardened container, Helm chart, high availability, CI pipeline with all checks, signed images and bill of materials | Needs Docker |
| **M5 Console and evidence** | Web console (schedules, approvals, kill switch, audit), periodic resilience evidence reports for auditors | Yes |
| Later | Beyond termination: latency and failure injection with control and experiment groups (ChAP-style), availability-zone loss (Chaos Gorilla-style) | — |

## Stack

- Python 3.14, managed with uv (same as your other projects)
- FastAPI for the API, pydantic for validating settings and requests
- SQLAlchemy with PostgreSQL in production, SQLite locally
- Typer for the CLI
- Official Kubernetes and AWS (boto3) client libraries, from M3
- pytest, ruff (including security rules), mypy, pip-audit, pre-commit

## Costs

- **Software**: all open source and free to use. No paid services needed.
- **Running it** (from M4, on the customer's own cloud): one small container plus a small PostgreSQL database. Roughly US$15–40 a month on AWS if they need a new database; close to nothing extra if they already run Kubernetes and Postgres.
- **On this PC**: nothing. M1 and M2 run fully locally against the simulator.
