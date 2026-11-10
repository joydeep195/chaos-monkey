# Chaos Monkey

Chaos engineering for regulated financial services: Netflix Chaos Monkey's random, opt-in terminations during working hours, plus single sign-on, roles, maker-checker approvals, a tamper-evident audit log, capacity and recovery checks, freeze windows, incident awareness and a kill switch. Built to be used by real fintech companies, not as a hobby project.

Read SPEC.md at the start of each milestone. Record every significant choice in docs/DECISIONS.md. Background on Chaos Monkey, with sources: docs/RESEARCH.md.

## Working with me

- I am a product manager who builds with AI coding tools. Explain the options and their costs in plain English before making an architectural choice, and keep the code simple enough for me to follow and change.
- Propose a plan and wait for my approval before writing code for a new milestone.
- Work one milestone at a time, in small commits with clear messages.
- My machine runs Windows. Use pathlib for paths and commands that work in PowerShell.
- Use British English in docs, messages and UI text.

## Product rules (these always apply)

1. Safe by default. A fresh install terminates nothing: off, leashed (dry run) and not scheduling, as Netflix's original ships.
2. Opt-in only. Nothing is touched unless its app is enabled, its account is allowlisted, and it is neither protected nor excepted.
3. Two people to go live. Live mode needs an approver who is neither the requester nor the last editor. Any config change voids the approval.
4. Every guard, at kill time. All 12 guards in guards.py must agree immediately before each kill; the kill switch is re-read right before the platform call.
5. Fail closed. When in doubt (an incident tool unreachable, a group not recovering), stop rather than continue.
6. Termination only. Drivers list and terminate; no shell, SSH or command execution, ever.
7. Everything is audited, in the same transaction as the change. If the audit log cannot be written, the change does not happen.
8. Secrets only from environment variables. Never in settings files, the database, logs or git.
9. The product is called Chaos Monkey (D19). It is an independent implementation, not affiliated with Netflix: never use Netflix's name as a brand, its logos or its code, and keep the "not affiliated" line in the README.
10. Synthetic data only in tests and the simulator, and labelled as such.

## Stack

- Python 3.14, managed with uv
- FastAPI and uvicorn for the API, pydantic for settings and requests, Typer for the CLI
- SQLAlchemy 2.1: PostgreSQL in production (`uv sync --extra postgres`), SQLite locally; times stored in UTC
- PyJWT with cryptography for OpenID Connect sign-in
- pytest (with httpx2 for the API test client), ruff (lint, format, security rules), mypy strict, pip-audit, pre-commit

## Commands

- Install: `uv sync`
- All checks (lint, formatting, types, tests, dependency audit): `uv run python tools/check.py`
- Tests: `uv run pytest`
- Create tables: `uv run chaosmonkey init`
- Overview: `uv run chaosmonkey status`
- One executor tick: `uv run chaosmonkey run --once`; keep running: `uv run chaosmonkey run`
- API (and executor) at http://127.0.0.1:8080, docs at /docs in development: `uv run chaosmonkey serve`
- Local sign-in token (development only): `uv run chaosmonkey dev-token --subject NAME --role ROLE [--team TEAM]`
- Verify the audit log: `uv run chaosmonkey audit verify`; export it: `uv run chaosmonkey audit export FILE`
- Simulated fleet: `uv run chaosmonkey fleet`

Update this section whenever a command changes.

## Layout

```
src/chaosmonkey/ domain, settings, working_time, scheduler, guards, executor, service,
                 audit, state, db, views, runtime, cli
  drivers/       base (the interface), simulator; kubernetes and aws in M3
  api/           app (factory, error handling), auth (sign-in), middleware (hardening), routes
docs/            GUIDE.md, RESEARCH.md, DECISIONS.md, governance/ (assessments)
tools/           check.py, precommit_guard.py
tests/           one file per area, all against synthetic fleets
```

## Secrets and git hygiene

- Secrets: CHAOSMONKEY_DATABASE_URL, CHAOSMONKEY_AUDIT_KEY, CHAOSMONKEY_DEV_SIGNING_KEY (development). The settings loader refuses them in files.
- chaosmonkey.toml and fleet.toml are local and gitignored; commit only the .example files.
- Before every commit, run `git status`. Stop and tell me if anything under data/, a .toml without .example, a key file or a database is staged.
- The pre-commit hook blocks secrets, local settings, keys and databases. Never bypass it.
- Remote: https://github.com/joydeep195/chaos-monkey (public). Push only when I ask.

## Definition of done

`uv run python tools/check.py` passes, new behaviour has tests, SECURITY.md is updated when a threat or control changes, DECISIONS.md records any new choice, and README is updated wherever commands or behaviour changed. Show me the actual command output, not a summary of it.
