# Chaos Monkey

Chaos engineering for regulated financial services.

Chaos Monkey does what Netflix's original Chaos Monkey does: on working days, during working hours, it randomly terminates instances of services that have opted in, so teams build systems that shrug off a lost server. It adds the controls a bank, payments company or broker needs before such a tool may touch production: single sign-on, roles, maker-checker approval for live runs, a tamper-evident audit log, capacity and recovery checks, freeze windows, incident awareness and a kill switch.

This is an independent implementation, written from scratch from Netflix's published descriptions. It shares the name of Netflix's open-source tool but contains none of its code, and it is not affiliated with or endorsed by Netflix. See [docs/RESEARCH.md](docs/RESEARCH.md) for what Netflix's Chaos Monkey is and how it works, with sources.

**Status:** milestones M1 (engine and safety) and M2 (secure API) are built and tested against a simulated fleet. Real Kubernetes and AWS drivers arrive in M3; until then production mode refuses to start. Plan: [SPEC.md](SPEC.md). **New here? Start with the step-by-step guide: [docs/GUIDE.md](docs/GUIDE.md).**

## How it decides

Each working day, for every group of every opted-in app, Chaos Monkey flips Netflix's weighted coin: the chance of a kill is 1 / (mean working days between kills). On heads it plans one kill at a random time inside working hours. When the time comes, **all 12 safety guards must agree**:

| Guard | Refuses when |
|---|---|
| Kill switch | Someone (or an automatic halt) has switched it on |
| Master switch | `enabled = false` in settings |
| Opted in | The app is not registered or not enabled |
| Account allowlist | The account is not in `allowed_accounts` |
| Working hours | Outside hours, at the weekend or on a holiday |
| Freeze window | A freeze window is in effect (month-end, peak days) |
| No open incident | An incident is declared, or an incident tool says so, or cannot be reached |
| Minimum gap | The group lost an instance too recently |
| Daily cap | The day's live-kill limit is reached |
| Concurrency cap | Earlier live kills are still recovering |
| Group healthy | Any instance in the group is already unhealthy |
| Capacity | One more kill would leave fewer than `min_healthy` instances |

If all agree, a random healthy instance is picked. It is only terminated for real if the global leash is off, the app is set to live, and **a second person has approved this exact configuration**. Otherwise it is a dry run: logged, not carried out. After a live kill Chaos Monkey watches for recovery; if the group has not recovered in time, it switches the kill switch on and nothing else happens until a person resumes.

Every decision, including every skipped kill and its reason, goes into a hash-chained audit log.

## Try it on this PC

The short version is below; [docs/GUIDE.md](docs/GUIDE.md) explains each step, the everyday controls and what to do when nothing seems to happen. You need [uv](https://docs.astral.sh/uv/). Nothing here touches a real system: the simulator stands in for a fleet.

```bash
uv sync
```

```bash
copy chaosmonkey.example.toml chaosmonkey.toml
```

```bash
copy fleet.example.toml fleet.toml
```

Then, in `chaosmonkey.toml`, set `enabled = true`, `leashed = false` and `schedule_enabled = true` to let the simulator run live.

```bash
uv run chaosmonkey init
```

```bash
uv run chaosmonkey fleet
```

```bash
uv run chaosmonkey apps set payments-api --team payments --enable --mode live --mean-days 1
```

```bash
uv run chaosmonkey --as olivia approvals request payments-api --reason "Quarterly resilience test"
```

```bash
uv run chaosmonkey --as arjun approvals approve 1 --note "Runbook reviewed, on-call ready"
```

```bash
uv run chaosmonkey run --once
```

```bash
uv run chaosmonkey schedule
```

```bash
uv run chaosmonkey audit verify
```

`--as` lets you act as different people locally, to see the four-eyes rule at work (olivia cannot approve her own request). Kills happen only inside the working hours set in `chaosmonkey.toml`.

### The API

```bash
uv run chaosmonkey serve
```

Interactive docs at http://127.0.0.1:8080/docs (development only). Get a local sign-in token with:

```bash
uv run chaosmonkey dev-token --subject priya --role approver
```

Main endpoints, all under `/api/v1` and all requiring a bearer token:

| Method and path | Who |
|---|---|
| `GET /me` | anyone signed in |
| `GET /apps`, `GET /apps/{name}` | any role |
| `PUT /apps/{name}` | app owner (own team) or admin |
| `POST /apps/{name}/approval-requests` | app owner (own team) or admin |
| `POST /approvals/{id}/approve` or `/reject` | approver (not the requester or last editor) |
| `POST /kill-switch/engage` | app owner, approver, operator, admin |
| `POST /kill-switch/release` | operator, admin |
| `POST /incident/declare`, `/incident/close` | as for the kill switch |
| `GET/POST/DELETE /freeze-windows` | read: any role; change: operator, admin |
| `GET /schedule`, `GET /terminations` | any role |
| `GET /audit`, `GET /audit/verify` | any role |

`/healthz` and `/readyz` need no token.

## Production

Production mode (`environment = "production"`) refuses to start until every check passes: PostgreSQL, an audit key of at least 32 characters, single sign-on (OpenID Connect), no development tokens, TLS, exact host names, and a real driver (M3). `uv run chaosmonkey status` lists what is missing. Secrets come only from environment variables:

| Variable | Holds |
|---|---|
| `CHAOSMONKEY_DATABASE_URL` | PostgreSQL URL including credentials (install with `uv sync --extra postgres`) |
| `CHAOSMONKEY_AUDIT_KEY` | Random secret that signs the audit log |
| `CHAOSMONKEY_CONFIG` | Optional path to the settings file |

In production the CLI can only read, switch the kill switch on and declare incidents; every other change goes through the API so it is tied to a signed-in person. See [SECURITY.md](SECURITY.md).

## Checks

```bash
uv run python tools/check.py
```

Runs lint and security rules (ruff), formatting, types (mypy, strict), the tests (pytest) and a dependency vulnerability audit (pip-audit). Install the pre-commit hook once with `uv run pre-commit install`.

## Layout

```
src/chaosmonkey/
  domain.py        apps, targets, groups, roles, people
  settings.py      settings file + secrets from the environment, production checks
  working_time.py  working days, hours, holidays
  scheduler.py     Netflix's daily scheduling algorithm
  guards.py        the 12 safety guards
  executor.py      plans the day, carries out kills, watches recovery
  service.py       what people can do, with roles and the four-eyes rule
  audit.py         hash-chained audit log
  state.py         kill switch, incidents, approvals, leases
  db.py            database tables
  drivers/         simulator now; Kubernetes and AWS in M3
  api/             FastAPI app, sign-in, hardening middleware, endpoints
  cli.py           operator command line
docs/              GUIDE.md (how to use it), RESEARCH.md (Netflix sources), DECISIONS.md
tests/             122 tests, all against synthetic fleets
tools/             check.py, precommit_guard.py
```

## Licence

Apache-2.0.
