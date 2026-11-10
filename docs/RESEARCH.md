# What Netflix's Chaos Monkey is and does

Research notes behind this Chaos Monkey platform. In this file, "Chaos Monkey" on its own means Netflix's original. Everything here is paraphrased from Netflix's own publications, read on 9 November 2026. Where a source could not be read, it says so.

## Sources

| # | Source | Date | Read? |
|---|--------|------|-------|
| S1 | Netflix Tech Blog, "5 Lessons We've Learned Using AWS" | 16 Dec 2010 | Yes |
| S2 | Netflix Tech Blog, "The Netflix Simian Army" | 19 Jul 2011 | Yes |
| S3 | Netflix Tech Blog, "Netflix Chaos Monkey Upgraded" (Chaos Monkey 2.0) | 19 Oct 2016 | Yes |
| S4 | Netflix Tech Blog, "ChAP: Chaos Automation Platform" | 26 Jul 2017 | Yes |
| S5 | Chaos Monkey docs, netflix.github.io/chaosmonkey (home, configuration file format, Spinnaker configuration, termination behaviour, tracker, outage checker, how to deploy, running locally) and the GitHub README (Apache-2.0) | current | Yes (the "How Chaos Monkey works" page returns 404) |
| S6 | Principles of Chaos Engineering, principlesofchaos.org (written by Netflix's chaos team) | Mar 2019 | Yes |
| S7 | Basiri, Hochstein, Jones, Tucker, "Automating Chaos Experiments in Production", ICSE-SEIP 2019, arXiv:1905.04648 | May 2019 | Yes, sections I–VIII in full; IX–X were truncated |
| — | Netflix Tech Blog, "FIT: Failure Injection Testing" (2014) | 2014 | No (the blog refused automated fetches); FIT facts below come from S7 |

## In one paragraph

Chaos Monkey is a tool that randomly switches off servers (virtual machines and containers) in production, on purpose, during working hours, so that engineers are forced to build services that survive a server disappearing. Each application opts in, chooses how often it is willing to lose a server, and is guaranteed a minimum gap between losses. It ships "leashed" (dry run) by default. It skips days when an outage is already under way, and it reports every termination to monitoring systems. Since version 2.0 (2016) it does nothing except terminate instances, and it does so through Netflix's deployment platform, Spinnaker.

## Why Netflix built it

- **Cloud servers fail more often than data-centre hardware** (S1). When Netflix moved to AWS in 2010, they found that designs which assumed hardware rarely fails (for example, keeping session state in memory) broke down. Their lesson was that the best way to avoid failure is to fail constantly. Chaos Monkey was one of the first systems they built on AWS.
- **Practise the failure at a convenient time** (S2). Netflix compared it to checking your spare tyre by deliberately puncturing a tyre on a quiet Sunday afternoon, rather than discovering at 3 am in the rain that the spare is flat. Running it mid-business-day, with monitoring and engineers standing by, means the real failure at 3 am is a non-event.
- **Alignment without mandates** (S3). Netflix's culture does not let anyone force engineers to architect code a particular way. Making server loss frequent and certain, instead of rare, gave every team a reason to build redundancy and automation.

## How it works (version 2, the current open-source release)

### Opt-in and configuration (S3, S5)

- Applications opt in; the default is `enabled = false`. A global `accounts` list limits which cloud accounts it may touch at all.
- Owners configure it per application in Spinnaker:
  - **Mean time between terminations** (default 2 working days). Netflix moved to this from a "probability over a period" setting because owners found it easier to reason about (S3).
  - **Minimum time between terminations** (default 1 working day). It never kills more often than this per group, enforced through a shared MySQL database so that several copies of Chaos Monkey cannot double up.
  - **Grouping**: by app, stack or cluster. Each group gets at most one termination per day, so an app with three stacks grouped by stack can lose up to three instances a day.
  - **Regions are independent**: if switched off, it will not hit the same group in two regions (for cross-region replicated databases).
  - **Exceptions**: opt out specific account / region / stack / detail combinations, with `*` as a wildcard. Netflix teams use this to exclude small test clusters.
- It automatically skips canary clusters and server groups that Spinnaker marks as disabled (S3, S5).

### Scheduling (S5)

- It is not a running service. A cron job runs `chaosmonkey schedule` once every weekday before working hours.
- For each enabled group it flips a weighted coin with probability p = 1 / (mean time between kills, in working days). On heads, it picks a random time between `start_hour` (9) and `end_hour` (15) in the configured time zone (default `America/Los_Angeles`), then picks a random instance in the group.
- The gap between kills therefore follows a geometric distribution with mean 1/p. When the minimum gap is above one day the real mean drifts upwards and Netflix does not correct for this.
- The day's schedule is stored in MySQL and written out as one cron job per planned kill.

### Safety features (S5)

- `leashed = true` by default: terminations are only logged, not carried out.
- `schedule_enabled = false` and `enabled = false` by default.
- An **outage checker** hook disables it during an ongoing outage (no-op unless you write one).
- A `max_apps` cap on how many apps are scheduled per run.
- Accounts allowlist, enabled-group filter, exceptions, working-hours window.

### Termination and tracking (S3, S5)

- Terminations go through the Spinnaker API, so it works on any cloud Spinnaker supports. Netflix uses it on AWS virtual machines and on containers in Titus, their container platform; the docs say it was tested on AWS, GCE and Kubernetes.
- **Termination only.** Earlier versions could SSH into a machine to burn CPU or take disks offline; version 2.0 removed this (S3).
- **Trackers** send each termination to external systems. Internally Netflix reports to Atlas (metrics) and Chronos (event history). Chaos Monkey periodically terminates itself too (S3).
- Version 2.0 also added cross-account terminations and automatic disabling during outages (S3).

### Default configuration (S5)

| Setting | Default |
|---------|---------|
| `enabled` | `false` |
| `leashed` | `true` |
| `schedule_enabled` | `false` |
| `accounts` | `[]` |
| `start_hour` / `end_hour` | `9` / `15` |
| `time_zone` | `America/Los_Angeles` |
| `max_apps` | 2147483647 (no real limit) |
| `outage_checker`, `trackers`, `decryptor`, `error_counter` | no-op |

Dependencies: Spinnaker (required), MySQL 5.6+, a Go toolchain; optional etcd or Consul for changing `enabled`, `leashed`, `schedule_enabled` and `accounts` without a redeploy.

## The Simian Army (2011, S2)

Chaos Monkey's success led Netflix to describe a family of "monkeys". Version 2.0 of Chaos Monkey kept only the termination job (S3).

| Member | What it did |
|--------|-------------|
| Chaos Monkey | Randomly disables production instances |
| Latency Monkey | Adds artificial delay between services, up to simulating a whole service being down |
| Conformity Monkey | Finds instances that break best practice (for example, not in an auto-scaling group) and shuts them down |
| Doctor Monkey | Uses health checks and signals like CPU load to remove unhealthy instances |
| Janitor Monkey | Finds and removes unused resources |
| Security Monkey | Finds security problems such as misconfigured security groups, and expiring certificates |
| 10–18 Monkey | Finds localisation and internationalisation problems across regions |
| Chaos Gorilla | Simulates losing an entire AWS availability zone |
| Chaos Kong | Simulates losing an entire region (named in S4) |

## How Netflix went beyond it (S4, S6, S7)

Chaos Monkey tests one thing: losing a server. Netflix later built more careful tools, and these supply most of the safety ideas an enterprise platform needs.

- **Principles of Chaos Engineering** (S6): define a measurable steady state (throughput, error rate, latency), hypothesise it holds in a control group and an experiment group, introduce real-world events, and try to disprove the hypothesis. Run in production, automate continuously, and minimise the blast radius.
- **FIT, Failure Injection Testing** (via S7): injects failures or latency into specific requests by tagging them with metadata that travels through the system. Injection points are the shared client libraries (RPC, Hystrix, EVCache, Cassandra).
- **ChAP, Chaos Automation Platform** (S4, S7):
  - FIT's problem was that experiment metrics were mixed with the whole system's, so teams ran bigger and longer experiments to see an effect, putting more customers at risk.
  - ChAP routes a small slice of users evenly into a control cluster and an experiment cluster (for example 0.5% of users on a 200-instance service means one instance each), and tracks business KPIs (stream starts per second) for just those two groups.
  - A **circuit breaker** ends the experiment automatically if a predefined error budget is exceeded, so experiments can run unsupervised. Analysis reuses Netflix's canary-analysis system.
  - It runs inside the deployment pipeline continuously, because hundreds of deploys a day quickly make old results stale.
  - "Concentrated" experiments expose problems that only appear when a large share of requests fail: exhausted thread pools, CPU-heavy fallbacks, logging lock contention and retry storms.
- **Guard rails stated in S7**: weekdays 9 am–5 pm only; all concurrent experiments together may affect no more than 5% of traffic in any region; no experiments during a regional failover; automatic stop on excessive customer impact.
- **Monocle** (S7) ranks which experiments to run and marks some as unsafe (no fallback, known impact on key metrics, stale data, blocklisted dependency). Unsafe experiments do not run.

## What Chaos Monkey leaves out, for a regulated fintech

These gaps are the reason this platform is more than a copy:

| Gap in Netflix's Chaos Monkey | Why it matters in fintech | Our answer |
|---------------------|---------------------------|------------------|
| Needs Spinnaker; most banks and fintechs do not run it | Unusable as-is | Talks directly to Kubernetes and AWS |
| Passwords in the config file are plaintext (decryptor is a no-op) | Fails any security review | Secrets only from the environment or a secrets manager; never in config |
| No login, roles or approvals of its own | Change management (SOX, PCI DSS, SOC 2, DORA) needs who-did-what and a second pair of eyes | Single sign-on, roles, and maker-checker approval before any live run |
| No audit trail beyond trackers | Auditors need tamper-evident records | Hash-chained, append-only audit log, exportable |
| No check that enough healthy servers remain before a kill | A kill during a scale-down could take a payment service below capacity | Capacity guard, plus Kubernetes disruption budgets honoured |
| No steady-state check or automatic stop | ChAP added this; Chaos Monkey never did | Health check before and recovery watch after every kill; failed recovery halts all chaos |
| No freeze windows or holidays | Month-end close, settlement windows and peak days must be off-limits | Freeze windows and holiday calendars |
| No global kill switch | Incident commanders need one button | Kill switch that stops everything immediately |
| No data retention or purge | Regulators expect a retention policy | Configurable retention with audit-safe purging |
