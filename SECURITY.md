# Security

Chaos Monkey deliberately breaks production systems, so the biggest risks are that it breaks the wrong thing, breaks too much, or is used by the wrong person. This page lists the threats and the control for each. It describes the state after milestones M1 and M2.

To report a vulnerability, contact the maintainers privately; do not open a public issue.

## What we protect

- **Customer-facing services** of the company running Chaos Monkey (payments, ledgers, onboarding).
- **The power to terminate**: Chaos Monkey's cloud credentials and the ability to switch apps to live.
- **The audit trail**: the evidence auditors and regulators rely on.
- **Secrets**: database credentials, the audit key, identity-provider configuration.

## Threats and controls

| Threat | Control | Checked by |
|---|---|---|
| Kills something it should never touch (a ledger primary, a non-opted-in team) | Opt-in per app; global account allowlist; a protected label that makes a target invisible; per-app exceptions; only targets carrying the opt-in label are listed by drivers | tests/test_scheduler.py, tests/test_executor.py |
| Kills too much at once | One kill per group per day; minimum gap between kills; global daily cap; cap on kills still recovering; capacity floor (`min_healthy`); refuses a group that is already degraded | tests/test_guards.py, tests/test_executor.py |
| Kills during an incident, freeze or outside hours | Incident guard (declared, or from incident tools, failing closed when they cannot be reached); freeze windows; holidays; working hours | tests/test_guards.py, tests/test_executor.py |
| A kill causes an outage that spreads | Recovery watch after every live kill; failure to recover by the deadline switches the kill switch on for everyone | tests/test_executor.py |
| Someone needs to stop everything now | Kill switch (API or CLI, even in production), re-read immediately before each platform call | tests/test_executor.py, tests/test_service.py |
| One person turns on live chaos alone | Maker-checker: live mode needs an approver who is neither the requester nor the last editor; admins cannot approve; any change voids the approval; approvals expire | tests/test_service.py, tests/test_api.py |
| Unauthenticated or forged access to the API | OpenID Connect tokens verified against the provider's keys; asymmetric algorithms only; issuer, audience, expiry and subject required; `alg: none` and HS256/RS256 confusion rejected; development tokens refused in production | tests/test_api.py |
| Over-privileged users | Five roles; app owners limited to their own teams; resuming chaos limited to operators and admins | tests/test_service.py, tests/test_api.py |
| Audit evidence altered | Hash chain with HMAC key held outside the database; verification detects edits, gaps and removed newest entries; head can be anchored externally | tests/test_audit.py |
| A change slips through without a record | Every change and its audit entry commit in one transaction; if the audit log cannot be written, the change is refused (503) | tests/test_service.py, src/chaosmonkey/api/app.py |
| Secrets leak through config or git | Secrets only from environment variables; loader refuses secrets in files; secrets redacted when printed; pre-commit hook blocks .env, local settings, keys, databases and credential-looking text | tests/test_settings.py, tools/precommit_guard.py |
| Misconfigured production | Startup refuses until PostgreSQL, audit key, single sign-on, TLS, exact host names and a real driver are in place | tests/test_settings.py |
| Browser-based attacks on the API | JSON-only API with `default-src 'none'` CSP, no framing, no-sniff, no-referrer, no-store, HSTS in production; CORS off unless listed exactly; host header allowlist; interactive docs only in development | tests/test_api.py |
| Abuse and resource exhaustion | Per-client rate limit; request body limit; token length limit; bounded page sizes | tests/test_api.py |
| Error messages leak internals | Generic 500 responses with a request ID; details only in server logs | tests/test_api.py |
| Two replicas double up | Database lease for the executor; unique constraint on one kill per group per day; conditional claim of each kill | tests/test_executor.py |
| Remote command execution through the tool | Drivers can only list and terminate. No shell, SSH or command runner exists | src/chaosmonkey/drivers/base.py |
| Vulnerable or tampered dependencies | uv.lock pins exact versions with hashes; pip-audit on every check run; ruff security rules | tools/check.py |

## Least privilege in the cloud (milestone M3)

The drivers will ship with permission files that allow only what Chaos Monkey needs:

- **Kubernetes**: a Role per opted-in namespace allowing `get`/`list` on pods and `create` on `pods/eviction`. Evictions respect PodDisruptionBudgets, so the cluster's own safety limits always win.
- **AWS**: an IAM policy allowing `ec2:DescribeInstances`, `autoscaling:Describe*` and `ec2:TerminateInstances` only on instances tagged with Chaos Monkey's opt-in tag, using short-lived credentials (IRSA or an assumed role), never long-lived keys.

## Known gaps, planned

- Database grants forbidding UPDATE and DELETE on the audit table, and export to write-once storage (M4).
- Alembic schema migrations (M4).
- Container hardening: minimal non-root image, read-only filesystem, signed images, software bill of materials (M4).
- Rate limiting is per replica; add a gateway limit in production (M4).

## Routine

- Before committing, run `uv run python tools/check.py` (lint, security rules, formatting, types, tests, dependency audit).
- Once a month, update dependencies with `uv lock --upgrade` and rerun the checks.
- Record the audit head after each export (`chaosmonkey audit verify`) somewhere outside the database.
- Never bypass the pre-commit hook.
