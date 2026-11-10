# How to use Chaos Monkey

Chaos Monkey switches off servers on purpose, at random, during working hours, so your teams find out whether their services survive it while everyone is at their desks rather than at 3am. This guide shows you how to run it, step by step.

On this PC it works against a **simulated fleet**: pretend servers listed in `fleet.toml`. Nothing real is touched. Connections to real Kubernetes clusters and AWS accounts arrive in milestone M3.

You can do everything from the command line. There is also a web API with an interactive page for trying it (section 6). A proper web console comes in milestone M5.

## 1. The ideas in two minutes

- **App.** A service that a team opts in, such as `payments-api`. Nothing that hasn't opted in is ever touched.
- **Group.** An app's servers are split into groups, by default one per cluster in each account and region. Chaos Monkey kills at most one server per group per day.
- **The daily coin.** Each working day, for each group, Chaos Monkey flips a weighted coin. The weighting comes from the app's **mean days between kills**: 2 (the default) means about every other working day; 1 means every working day. On heads, it picks a random time inside working hours.
- **Dry run or live.** A dry run records which server *would* have been killed, and kills nothing. A live run really terminates it. Live needs three things at once:
  1. the global leash is off (`leashed = false` in settings);
  2. the app is set to `live`;
  3. a second person has approved that exact configuration. Any later change to the app cancels the approval.
- **Safety checks.** At the moment of each kill, 12 checks must all say yes: kill switch, working hours, holidays, freeze windows, open incidents, minimum gap since the last kill, daily and concurrent limits, the group is healthy, and enough servers would remain. If any says no, the kill is skipped and the reason is recorded.
- **Kill switch.** One command stops everything. Chaos Monkey also switches it on by itself if a group fails to recover after a kill.
- **Audit log.** Every action is recorded in a chain where each entry seals the one before it, so any change or deletion is detected.

## 2. Who does what

| Role | Typical person | Can |
|---|---|---|
| Viewer | Anyone curious, auditors | See apps, schedules, results and the audit log |
| App owner | A team lead | Opt their own team's apps in or out, change their settings, ask to go live, press the kill switch |
| Approver | Head of engineering, risk | Approve or reject a request to go live (never their own, and never for a configuration they last changed); press the kill switch |
| Operator | SRE on call | Press and release the kill switch, declare and close incidents, manage freeze windows |
| Admin | Platform team | Everything except approving (admins set things up; approvers sign off risk) |

On this PC you hold every role at once. Add `--as NAME` before a command to act as someone else, which is how you try the two-person rule on your own. In production there is no `--as`: people sign in through the company's single sign-on.

## 3. Set up on this PC (once)

You need [uv](https://docs.astral.sh/uv/). Open PowerShell in `D:\Projects\chaos-monkey` and run these one at a time.

```powershell
uv sync
copy chaosmonkey.example.toml chaosmonkey.toml
copy fleet.example.toml fleet.toml
```

Open `chaosmonkey.toml` in a text editor. A fresh install is off, leashed and not scheduling, exactly as Netflix ships the original. To let the simulator run for real, change these three lines:

```toml
enabled = true
leashed = false
schedule_enabled = true
```

While you're there, check `time_zone`, `start_hour` and `end_hour`. Chaos Monkey only acts inside those hours, Monday to Friday, outside the `holidays` listed.

Then create the database and look around:

```powershell
uv run chaosmonkey init
uv run chaosmonkey status
uv run chaosmonkey fleet
```

`status` shows the switches, working hours, kill switch and incident. `fleet` lists the pretend servers. In the example fleet, `ledger-primary` is marked protected and is never touched, `kyc-checks` has only two servers so the capacity check always refuses, and `statements` never recovers, which shows the automatic halt.

Every command starts with `uv run chaosmonkey`. Add `--help` to any command to see its options, for example `uv run chaosmonkey apps set --help`.

## 4. Walkthrough: your first live kill

The output below is real, from a run on 10 November 2026. To finish quickly, that run used a one-hour working window and a 3-minute recovery timeout. Times in the output are in UTC (`+00:00`).

### Step 1: opt an app in, as a dry run

```powershell
uv run chaosmonkey apps set payments-api --team payments --enable --mean-days 1
```
```text
Saved payments-api (version 1). Dry run: the app is set to dry run.
```

`--mean-days 1` means a kill every working day, which suits a trial. Leave it at the default of 2, or higher, for real use. You can stop here and run in dry run for a week or two to see what *would* happen.

### Step 2: ask to go live

```powershell
uv run chaosmonkey apps set payments-api --mode live
uv run chaosmonkey --as olivia approvals request payments-api --reason "Quarterly resilience test"
```
```text
Saved payments-api (version 2). Dry run: live mode is waiting for approval of this configuration.
Request 1 is waiting for an approver.
```

### Step 3: a second person approves

The person who asked can't approve, and nor can the last person who changed the app:

```powershell
uv run chaosmonkey --as olivia approvals approve 1 --note "Looks fine to me"
```
```text
Four-eyes rule: you cannot decide on a request you made or a configuration you last changed
```

Someone else can:

```powershell
uv run chaosmonkey --as arjun approvals approve 1 --note "Runbook reviewed, on-call ready"
uv run chaosmonkey apps list
```
```text
Approved request 1 until 2027-02-07T19:59+00:00.
NAME          TEAM        ENABLED  MODE     RUNS_AS  MEAN_DAYS  GROUPING  VERSION
kyc-checks    onboarding  True     dry_run  dry_run  1.0        cluster   1
payments-api  payments    True     live     live     1.0        cluster   2
statements    statements  True     live     live     1.0        cluster   1
```

`MODE` is what the app owner asked for; `RUNS_AS` is what will actually happen. Approvals last 90 days. Two more apps were opted in the same way for this run: `kyc-checks`, left in dry run, and `statements`, approved for live.

### Step 4: start Chaos Monkey and leave it running

```powershell
uv run chaosmonkey run
```

It checks every 30 seconds. On its first check inside working hours each day it plans the day, then carries out each kill when its time comes. Leave this window open and use a second PowerShell window for the next steps. Press Ctrl+C to stop it.

(`uv run chaosmonkey serve` does the same and also starts the web API; see section 6.)

### Step 5: see what was planned and what happened

```powershell
uv run chaosmonkey schedule
```
```text
ID  RUN_AT                  GROUP                                                STATUS   OUTCOME
4   2026-11-09T20:14+00:00  kyc-checks/staging/ap-south-1/-/kyc-checks           skipped  Kill switch is on (Automatic halt: statements/staging/ap-south-1/-/statements did not reco
3   2026-11-09T20:11+00:00  statements/staging/ap-south-1/-/statements           done     Terminated staging-ap-south-1-statements-003. Live: approved by cli:arjun until 2027-02-07
2   2026-11-09T20:05+00:00  payments-api/prod/ap-south-2/main/payments-api-main  done     Terminated prod-ap-south-2-payments-api-main-003. Live: approved by cli:arjun until 2027-0
1   2026-11-09T20:00+00:00  payments-api/prod/ap-south-1/main/payments-api-main  done     Terminated prod-ap-south-1-payments-api-main-004. Live: approved by cli:arjun until 2027-0
```

```powershell
uv run chaosmonkey terminations
```
```text
AT                      TARGET                                 MODE  RECOVERY
2026-11-09T20:11+00:00  staging-ap-south-1-statements-003      live  failed
2026-11-09T20:05+00:00  prod-ap-south-2-payments-api-main-003  live  recovered
2026-11-09T20:00+00:00  prod-ap-south-1-payments-api-main-004  live  recovered
```

What happened:

- Each `payments-api` group lost one server, and a replacement was up within about a minute and a half.
- `statements` lost a server that never came back, because the example fleet makes it fragile on purpose. When the recovery timeout passed, Chaos Monkey switched the kill switch on by itself.
- The `kyc-checks` kill due three minutes later was therefore skipped, and the reason was recorded.
- The `ledger` servers were never touched, because that app never opted in.

Nothing more will happen until a person looks into it and releases the kill switch (section 5).

### Step 6: check the audit log

```powershell
uv run chaosmonkey audit verify
```
```text
Audit log intact: 20 entries.
Head (record this somewhere safe): 20:eec265816f9e5a0a5527ddf82a1cd0ecd849053df37af4255185d899758f7c56
```

Copy the `Head` line somewhere outside Chaos Monkey (a ticket, a wiki page). Later, `audit verify --expect-head <that line>` proves nothing recorded up to that point was changed or removed, even by someone with full database access. `uv run chaosmonkey audit list` shows the entries, and `uv run chaosmonkey audit export audit.jsonl` writes the whole log to a file for an auditor.

## 5. Everyday controls

**Stop everything now** (anyone with a role except viewer):

```powershell
uv run chaosmonkey kill-switch engage --reason "Investigating slow payments"
```

**Start again** (operators and admins only):

```powershell
uv run chaosmonkey kill-switch release --reason "Payments back to normal"
```

**An incident is under way** (pauses all kills until it's closed; operators and admins close it):

```powershell
uv run chaosmonkey incident declare --summary "Card authorisations failing"
uv run chaosmonkey incident close --note "Fixed by rollback"
```

**Freeze windows** for month-end, festival sales or a big release. Times are in your configured time zone:

```powershell
uv run chaosmonkey freeze add "Month-end close" --start 2026-11-30 --end 2026-12-02T18:00
uv run chaosmonkey freeze list
uv run chaosmonkey freeze remove 1
```

**Change an app.** Any change creates a new version and cancels its approval, so it drops back to dry run until a second person approves again:

```powershell
uv run chaosmonkey apps set payments-api --mean-days 3 --min-healthy 3
```

**Keep part of an app out.** The format is `account/region/stack/detail`, and `*` means any:

```powershell
uv run chaosmonkey apps set payments-api --except "prod/ap-south-2/*/*"
```

**Opt out completely:**

```powershell
uv run chaosmonkey apps set payments-api --disable
```

Other useful options on `apps set`: `--grouping app|stack|cluster` (how servers are grouped), `--regions-linked` (treat all regions as one group), `--min-days` (minimum working days between kills in a group), `--min-healthy` (never leave fewer healthy servers than this).

## 6. The web API and its try-it page

```powershell
uv run chaosmonkey serve
```

Then open http://127.0.0.1:8080/docs in a browser. This page lists every endpoint and lets you call them. It only exists in development.

Every call needs a sign-in token. In production these come from the company's identity provider. On this PC, make one in a second PowerShell window:

```powershell
uv run chaosmonkey dev-token --subject priya --role approver
```

Copy the long token it prints, click **Authorize** on the docs page, paste it and confirm. Try `GET /api/v1/me` first: it shows who you are and your roles. For an app owner, give a team as well: `--role app_owner --team payments`. You can repeat `--role` to hold several roles. Tokens last 8 hours.

What each role may call is in the README's endpoint table. Anything you aren't allowed to do returns `403`.

## 7. Settings

All in `chaosmonkey.toml`. The ones you're most likely to change:

| Setting | What it does | Default |
|---|---|---|
| `enabled` | Master switch. `false` means nothing is ever terminated | `false` |
| `leashed` | `true` turns every kill into a dry run, whatever apps ask for | `true` |
| `schedule_enabled` | `false` stops new kills being planned | `false` |
| `time_zone`, `start_hour`, `end_hour` | Working hours. Kills only happen inside them | example: Asia/Kolkata, 10 to 16 |
| `working_days`, `holidays` | Days when kills may happen | Monday to Friday, none |
| `allowed_accounts` | Accounts Chaos Monkey may touch. Anything else is off limits | none |
| `max_kills_per_day` | Live kills across all apps in a day | 10 |
| `max_concurrent_kills` | Live kills still waiting to recover at once | 1 |
| `recovery_timeout_minutes` | If a group hasn't recovered by then, the kill switch goes on | 10 |
| `approval_valid_days` | How long an approval lasts | 90 |

Passwords and keys never go in this file. Chaos Monkey refuses to start if they do; they come from environment variables (see the README's Production section).

`fleet.toml` describes the pretend fleet: for each group, the app, account, region, number of servers and how long a replacement takes to appear. `protected = true` marks servers that are never touched, and `recovers = false` makes a group that never comes back.

## 8. Why didn't anything happen?

Run `uv run chaosmonkey status` and `uv run chaosmonkey schedule`. Every skipped kill shows its reason in the `OUTCOME` column. The usual causes:

| What you see | Why | What to do |
|---|---|---|
| `schedule` shows nothing | Outside working hours, a weekend or holiday; or `enabled` or `schedule_enabled` is `false` | Check `status`; wait for working hours |
| Nothing planned for an app you just added | Each day is planned once, at the first check in working hours. Apps opted in after that wait until the next working day | Wait until tomorrow (see the tip below) |
| Some days have no kill | The coin came up tails. With mean days 2, about half the days have none | Normal. Use `--mean-days 1` for a trial |
| Kills are `dry run` | The leash is on, the app is in dry run, or its approval is missing, expired or cancelled by a change | `apps list` shows `RUNS_AS`; `apps show NAME` gives the reason |
| `skipped` with a reason | One of the 12 safety checks said no | Read the reason; it names the check |
| Everything stopped | The kill switch is on, possibly switched on automatically after a failed recovery | `kill-switch status`, then `kill-switch release` once it's safe |

**Tip for trying it on this PC.** To see a kill within the hour, set `start_hour` to the current hour and `end_hour` to one hour later, and use `--mean-days 1`. If today has already been planned, the simplest way to start over is to delete `data\chaosmonkey.db` and `data\simulator-state.json`, then run `uv run chaosmonkey init` again. Only ever do this with trial data on your own PC; on a real installation the audit log must never be deleted.

## 9. Going to production

Not yet: production needs the real Kubernetes and AWS drivers from milestone M3, and until then Chaos Monkey refuses to start in production mode. When it's ready, setting `environment = "production"` turns on strict checks, and `uv run chaosmonkey status` lists anything still missing: a PostgreSQL database, an audit key, company single sign-on, TLS, and no development tokens. In production the command line can only read, press the kill switch and declare incidents. Every other change goes through the API, so it is tied to a signed-in person.

## Command cheat sheet

| To | Run |
|---|---|
| See the overall state | `uv run chaosmonkey status` |
| List the (simulated) servers | `uv run chaosmonkey fleet` |
| Opt an app in or change it | `uv run chaosmonkey apps set NAME --team TEAM --enable` |
| List apps | `uv run chaosmonkey apps list` |
| Ask to go live | `uv run chaosmonkey apps set NAME --mode live`, then `approvals request NAME --reason "..."` |
| Approve or reject | `uv run chaosmonkey approvals approve ID --note "..."` (or `reject`) |
| Run Chaos Monkey | `uv run chaosmonkey run` (or `serve` for the API as well) |
| Do one check and stop | `uv run chaosmonkey run --once` |
| See today's plan and results | `uv run chaosmonkey schedule` |
| See recent kills | `uv run chaosmonkey terminations` |
| Stop everything | `uv run chaosmonkey kill-switch engage --reason "..."` |
| Start again | `uv run chaosmonkey kill-switch release --reason "..."` |
| Pause for an incident | `uv run chaosmonkey incident declare --summary "..."` |
| Add a freeze window | `uv run chaosmonkey freeze add NAME --start ... --end ...` |
| Check the audit log | `uv run chaosmonkey audit verify` |
| Make a local API token | `uv run chaosmonkey dev-token --subject NAME --role ROLE` |
