# AI fit assessment: Chaos Monkey

| Field | Value |
|---|---|
| Owner (role) | Product owner |
| Status | Draft |
| Gate | G0 Intake and fit |
| Policy | aipg-default v1.1.0 (bundled default; this workspace has no `.ai-governance/policy.json`) |
| Skill | ai-product-governance 1.0.0 (github.com/joydeep195/ai-product-governance) |
| Last updated | 2026-11-10 |

**Verdict:** Out of scope as built (Chaos Monkey contains no AI) · Candidate A: Deterministic first · Candidate B: Deterministic first · Candidate C: AI-assisted · Policy: aipg-default v1.1.0

## Scope of this assessment

Chaos Monkey has no AI or machine learning. Planning uses Netflix's weighted coin, target choice is uniformly random among eligible instances, and every decision after that comes from 12 deterministic guards, a two-person approval and a recovery timer. The source, dependencies and specification were searched for model libraries and model calls; there are none.

The skill governs AI features, so for the product as built there is nothing for gates G1 to G5 to review. Its Gate 0 answers a question that does apply: **should Chaos Monkey use AI anywhere?** This document runs that test on the three AI uses most likely to be proposed for a chaos-engineering product. None of them has been proposed; they are hypothetical and labelled as such.

| # | Candidate | Fit score | Verdict | Risk tier if built |
|---|---|---|---|---|
| A | AI chooses which server to terminate, and when | 1 / 12 | Deterministic first | High |
| B | AI decides whether a group has recovered, or whether to halt | 1 / 12 | Deterministic first | not run (Gate 0 stops it) |
| C | AI drafts the narrative of the periodic resilience evidence report (M5) | 8 / 12 | AI-assisted | High |

---

## Candidate A: AI chooses which server to terminate, and when

### 1. Problem and users
- **Job to be done:** pick which instance in an opted-in group to terminate, and at what time.
- **Who does it today, and how often:** the scheduler and executor, automatically; a few kills per working day per customer.
- **What goes wrong today:** nothing identified. Randomness is the design intent: teams cannot predict or game which instance goes, which is what forces resilience.

### 2. Current baseline
| Measure | Value | Source |
|---|---|---|
| Time per task | Milliseconds | Measured (tests and trial run) |
| Cost per task | Effectively zero | Measured |
| Quality | Uniform random choice inside 12 guards; statistical test over 20,000 simulated days confirms the target rate | Measured (tests/test_scheduler.py) |

### 3. Proposed AI role
- **The model produces:** an action (terminate this instance now).
- **Who acts on the output, and how they check it:** nobody; kills are unattended by design.
- **Interaction pattern:** agentic action, the hardest pattern to evaluate. No narrower pattern delivers the idea.

### 4. AI fit test
| # | Dimension | Score | Reason |
|---|---|---|---|
| F1 | Input variability | 0 | Inputs are structured fleet records: app, account, region, cluster, health. |
| F2 | Rule coverage | 0 | Netflix's algorithm and the 12 guards cover every case; there is no residual for a model. |
| F3 | Verification cost | 0 | There is no cheaper way to check a "smarter" choice than the rule itself. |
| F4 | Error tolerance | 0 | A wrong choice terminates a production server, possibly in payments; termination is irreversible. |
| F5 | Context availability | 1 | Fleet state is available, but "which kill teaches the most" depends on knowledge nobody can give the model. |
| F6 | Value at volume | 0 | A handful of kills a day, already done at no cost. |
| | **Total** | **1 / 12** | First matching rule: F2 = 0, so Deterministic first. F3 = 0 and F4 = 0 also hold. |

Anti-patterns: *solution looking for a problem*, and *LLM as validator*: deterministic business rules belong in code.

### 5. Cost of error
- **Worst realistic failure:** the model picks an instance the guards would have refused, or biases kills towards some groups, taking a payments service below capacity.
- **Who is harmed, and how badly:** the customer's end users (failed payments) and the customer (incident, possible regulatory reporting).
- **How quickly would we notice:** at the next failed recovery, after the harm.

### 6. Data
- **Inputs required:** fleet inventory, health, kill history.
- **Availability, permissions and quality:** available, but it describes production infrastructure; sending it to an external model provider exposes confidential topology.

### 7. Success metric
N/A — no problem to move a metric on. The current mechanism meets its quality bar.

### Risk tier preview (skill calculator)
`python scripts/risk_tier.py` with answers: regulated process, external provider, takes irreversible actions, autonomy L5, financial impact per action 100,000 (assumed).

- **Tier: High.** Triggered by H-03 (runs inside a regulated process), H-05 (single action can cost 1,000 or more), H-06 (acts without per-action approval, hard to reverse), plus L-04, L-05 and L-06.
- **Autonomy cap at launch: L3** (a person approves each action). Planned L5 exceeds it. A person approving every kill removes the unattended randomness that makes Chaos Monkey work, so the idea defeats its own purpose.
- 21 mandatory controls; approvers: product owner, engineering lead, security lead, privacy officer, legal counsel, AI governance board.

---

## Candidate B: AI decides whether a group has recovered, or whether to halt

### 1. Problem and users
- **Job to be done:** after a kill, decide whether the group is healthy again, or whether to switch the kill switch on.
- **Who does it today, and how often:** the executor's recovery watch, once per kill.
- **What goes wrong today:** nothing identified. M3 adds monitoring and incident-tool checks to the same deterministic rule.

### 2. Current baseline
| Measure | Value | Source |
|---|---|---|
| Time per task | One check per tick (30 seconds) | Measured |
| Cost per task | Effectively zero | Measured |
| Quality | Halts correctly when a group fails to recover; shown in the 10 November trial | Measured (trial run, tests/test_executor.py) |

### 3. Proposed AI role
- **The model produces:** a label (recovered / not recovered) that triggers an action (halt or continue).
- **Who acts on the output, and how they check it:** nobody; it runs unattended.
- **Interaction pattern:** classification feeding an automatic action.

### 4. AI fit test
| # | Dimension | Score | Reason |
|---|---|---|---|
| F1 | Input variability | 0 | Healthy-instance counts now; monitoring metrics from M3 are still structured. |
| F2 | Rule coverage | 0 | "Back to N healthy within the timeout", plus M3 threshold checks, covers the cases. |
| F3 | Verification cost | 0 | Checking the model's call means running the health check anyway. |
| F4 | Error tolerance | 0 | A wrong "recovered" lets chaos continue during real damage, silently; it breaks the product's fail-closed rule. |
| F5 | Context availability | 1 | Signals exist, but subtle degradation may not show in them. |
| F6 | Value at volume | 0 | One decision per kill. |
| | **Total** | **1 / 12** | First matching rule: F2 = 0, so Deterministic first. |

### 5. Cost of error
- **Worst realistic failure:** the model says a group recovered when it did not, and the next kill lands on a degraded service.
- **Who is harmed, and how badly:** the customer's end users and the customer, as for A.
- **How quickly would we notice:** only when customers complain.

### 6. Data
- **Inputs required:** health checks, monitoring metrics (M3), incident status.
- **Availability, permissions and quality:** available through the M3 integrations.

### 7. Success metric
N/A — the deterministic rule already meets the bar. Improve it by adding signals (M3), not a model.

---

## Candidate C: AI drafts the narrative of the periodic resilience evidence report

### 1. Problem and users
- **Job to be done:** produce the monthly or quarterly evidence that resilience testing happened, for internal risk teams and external auditors (DORA testing in the EU, RBI business-continuity expectations in India). The report is planned for M5.
- **Who does it today, and how often:** an SRE or compliance analyst, monthly or quarterly per customer (assumed).
- **What goes wrong today:** writing the explanation around the numbers takes senior time (assumed; no customer yet).

### 2. Current baseline
| Measure | Value | Source |
|---|---|---|
| Time per task | 1 to 2 analyst days per report | Assumed |
| Cost per task | Not estimated | Assumed |
| Quality | Unknown | Assumed |

### 3. Proposed AI role
- **The model produces:** a draft (narrative sections only).
- **Who acts on the output, and how they check it:** a named reviewer (head of SRE or compliance) checks each sentence against the cited audit entries, edits it, and signs the report before it leaves.
- **Interaction pattern:** draft for review, grounded only in the audit export. Code computes every number and table; the model writes the words around them and cites audit sequence numbers for each claim. This is narrower than open Q&A or chat.

### 4. AI fit test
| # | Dimension | Score | Reason |
|---|---|---|---|
| F1 | Input variability | 1 | Structured audit entries mixed with free-text reasons and notes; the output is prose. |
| F2 | Rule coverage | 1 | A template produces the tables and counts; the residual is the explanation across apps and periods. |
| F3 | Verification cost | 2 | Each claim can be checked against cited audit entries in seconds; writing it takes hours. |
| F4 | Error tolerance | 1 | Errors are visible before sending because a person signs off, but a wrong claim that reaches an auditor is a regulatory problem. |
| F5 | Context availability | 2 | Every fact is in the audit log and termination records. |
| F6 | Value at volume | 1 | Low volume, but each report saves hours of senior time. |
| | **Total** | **8 / 12** | No zeroes; total 6 to 8, so AI-assisted: viable with human verification and a narrow scope. |

Deterministic first still applies to the tables: ship the templated M5 report before considering a model for the narrative.

### 5. Cost of error
- **Worst realistic failure:** the draft says a group recovered when it did not, or miscounts the tests, and the reviewer misses it.
- **Who is harmed, and how badly:** the customer (misleading evidence to a regulator or auditor) and the product's credibility.
- **How quickly would we notice:** at sign-off if citations are checked; otherwise at audit.

### 6. Data
- **Inputs required:** the audit export (JSON lines), schedule and termination records.
- **Availability, permissions and quality:** complete and tamper-evident. The data holds no customer records, but it does describe infrastructure (account names, instance IDs) and names employees by user ID, so it is confidential. An external model provider needs vendor data terms, or the model should be self-hosted.

### 7. Success metric
- **Metric:** time to produce a signed report, from 1 to 2 analyst days (assumed) to under 2 hours including review, with zero factual errors found at audit; measured on the first two customers after M5.

### Risk tier preview (skill calculator)
`python scripts/risk_tier.py` with answers: seen outside the organisation (auditors), regulated process, external provider, retrieval, no actions, autonomy L2, no direct financial impact.

- **Tier: High.** Triggered by H-03 (runs inside a regulated process), plus L-01 (seen outside the organisation) and L-04 (confidential data to an external provider).
- **Autonomy:** planned L2 (a person reviews and sends), within the L3 cap.
- 22 mandatory controls, including offline and continuous evaluation, red-teaming, prompt-injection defences, privacy review, vendor data terms and human review of every output. Eval minimums: golden set of at least 300, pass rate at least 95%, zero critical failures. Re-certification every 3 months. Approvers as for A.
- Implication: a high governance load for a modest saving. Only worth it if customers ask for narrative reports after the templated version exists.

---

## Note: controls Chaos Monkey already has

Chaos Monkey is not an AI system, so the policy does not apply to it. For reference only, several of its existing controls match the policy's high-tier operational controls in intent: maker-checker approval (GOV-03), approvals that expire after 90 days (GOV-02), a kill switch with automatic halt (OPS-02), a hash-chained audit log (OPS-03), the recovery watch (OPS-01), configuration versions that void approvals (OPS-05), and least-privilege cloud permissions planned for M3 (SEC-02). This is an observation, not a compliance claim.

Regulatory exposure to flag (not a legal conclusion): if candidate C were built, the EU AI Act's transparency duties and DORA's expectations for test evidence would need review by legal counsel and each customer's compliance team.

## Assumptions Ledger
| Assumption | Why it matters | How to verify | Owner |
|---|---|---|---|
| Candidates A to C are hypothetical; nobody has proposed them | Defines what was assessed | Confirm which, if any, are on the roadmap | Product owner |
| Evidence reports run inside a regulated process (DORA, RBI) | Triggers H-03, making C high-tier | Each customer's compliance team confirms | Legal counsel |
| C would use an external model provider | Triggers L-04 and the vendor controls | Decide hosting at G1 | Engineering lead |
| Employee user IDs in the audit log are business identifiers, not personal data beyond business contact details | If wrong, privacy controls widen (L-03) | Privacy review | Privacy officer |
| Writing a report takes 1 to 2 analyst days | Drives the value of C | Time the first manual report in M5 | Product owner |
| A wrong kill can cost 1,000 or more | Triggers H-05 for A | Customer impact estimates | Customer SRE lead |

## Open decisions
| Decision | Owner (role) | Needed by |
|---|---|---|
| Adopt "no AI or machine learning in the kill path (planning, target choice, guards, recovery, halt)" as a product rule | Product owner | Before the M3 plan is approved |
| Put candidate C on the roadmap after the templated M5 report, or not | Product owner | M5 planning |
| If C proceeds: external provider or self-hosted model | Engineering lead with privacy officer | G1 |

**Next gate:** none for the product as built. For candidate C only, if pursued: G1 Value and economics, after the templated evidence report ships in M5.
