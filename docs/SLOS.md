# Service Level Objectives

- **Version:** 1.0
- **Date:** 2026-08-22
- **Status:** Targets for the Phase 5 production deployment. **None are measured today** — there
  is no `/metrics`, no tracing, and no eval harness (ROADMAP §2.1).
- **Companions:** [HLD §5](HLD.md#5-non-functional-requirements--design-response) · [ADR-0005](decisions/0005-autonomy-level.md) · [RISK_TAXONOMY](RISK_TAXONOMY.md)

An SLO nobody measures is a wish. Each objective below names the **metric series** that proves it
and the **alert** that fires when it breaks. Where a target is a guess, it says so.

---

## 1. Service definition

| Term | Definition |
|---|---|
| **A log** | One vendor message accepted at `POST /webhook/vendor-log`. |
| **A decision** | One `decision` row with `effective_risk_level` set. |
| **Serving** | Accepting a log and returning `200`/`202` within the request budget. |
| **Good minute** | A minute with ≥ 1 request and error rate < 1%. |
| **Approval wait** | Time from `approval.requested_at` to `decided_at`. **Excluded from latency SLOs** and tracked separately (§5). |

Approval wait is excluded on purpose. Human latency is hours; machine latency is seconds. Averaging
them produces a number that describes neither and hides regressions in both.

---

## 2. Availability

| SLI | Target | Window | Error budget |
|---|---|---|---|
| Successful ingest ratio — non-5xx over total, `/webhook/vendor-log` | **99.5%** | 30d rolling | 3 h 39 m |
| Approval API availability | **99.0%** | 30d rolling | 7 h 18 m |
| Decision durability — accepted logs that reach a terminal state | **99.99%** | 30d rolling | 1 in 10 000 |

```
availability = 1 - (rate(http_requests_total{route="/webhook/vendor-log",status=~"5.."}[30d])
                  / rate(http_requests_total{route="/webhook/vendor-log"}[30d]))
```

**Why 99.5% and not higher:** the queue absorbs an outage
([ADR-0004](decisions/0004-ingestion-topology.md)). A 30-minute API outage delays decisions by 30
minutes; it does not lose logs. Buying a further nine would cost multi-region infrastructure to
reduce a delay nobody would notice. **Durability is set two orders of magnitude tighter than
availability, because a lost log is a decision that never happens and nobody ever knows.**

**Alerts:** error rate > 5% for 5 min → page. Error budget 50% consumed at mid-window → ticket.

---

## 3. Latency and throughput

Measured server-side, `X-Correlation-Id` mint to response flush. Excludes approval wait.

| SLI | Target | Notes |
|---|---|---|
| `LOW`/`MEDIUM` end-to-end p50 | **≤ 4 s** | Retrieval + one LLM call + persist |
| End-to-end p95 | **≤ 12 s** | ~3× p50; absorbs one retry |
| End-to-end p99 | **≤ 25 s** | Two retries |
| Hard request budget | **60 s** | Cancel → checkpoint → `503` |
| Retrieval p95 | **≤ 150 ms** | pgvector HNSW, ≤ 5k chunks |
| Rules node p95 | **≤ 20 ms** | Pure function + one indexed read |
| Persist p95 | **≤ 50 ms** | One transaction |
| Queue lag p95 (S3 `ObjectCreated` → decision committed) | **≤ 90 s** | End-to-end freshness |

| Throughput | Target |
|---|---|
| Sustained | **5 000 logs/day** (~0.06/s) |
| Peak burst | **1 log/s for 10 min** |
| Backlog drain | **≥ 10 logs/min** after an outage |

**These are design-point estimates, not measurements.** The dominant term is one LLM call, which
is the vendor's latency, not ours — §3 will be re-baselined from real p50/p95 within two weeks of
the Phase 5 deployment.

**Alerts:** p95 > 12 s for 10 min → ticket. p95 > 25 s for 5 min → page. Queue lag > 300 s → page.

---

## 4. Quality — the objectives that actually matter

Availability and latency are table stakes. **A fast, available system that suspends the wrong
supplier is worse than no system.**

| SLI | Target | Measured by | Gate |
|---|---|---|---|
| **False-pause rate** — predicted `HIGH`/`CRITICAL` where true label is `LOW`/`MEDIUM` | **≤ 2%** | `evals/run_eval.py` on the golden set | **Blocks autonomy** ([ADR-0005](decisions/0005-autonomy-level.md)); non-zero exit in CI |
| Macro-F1 across four levels | **≥ 0.80** | same | Non-zero exit in CI |
| Missed-escalation rate — true `HIGH`/`CRITICAL` predicted `LOW`/`MEDIUM` | **≤ 10%** | same | Ticket, not a block |
| Retrieval recall@5 | **≥ 0.90** | 20 hand-picked golden logs | Blocks Phase 2 |
| Adversarial pass rate | **100%** | `evals/cases/` | Any injection-induced pause fails the build |
| Determinism | **≥ 98%** label agreement across two consecutive eval runs | `evals/history/` | Ticket |
| LLM vs baseline | **≥ +15 macro-F1** over `baselines/keyword_classifier.py` | `evals/run_eval.py` | If unmet, the LLM is not paying for itself |
| Human agreement rate on `HIGH`+ | **≥ 95%** approved | `approval` rows | Autonomy promotion criterion |

**False-pause is capped five times tighter than missed-escalation**, and that asymmetry is the
whole safety posture. A missed escalation returns the world to today's status quo — a human
notices later. A false pause halts a legitimate supplier's purchase orders and produces a contract
dispute. They are not comparable errors and a single accuracy number would hide the difference.

**Metrics are reported split by `source` (synthetic vs real), never pooled.** Synthetic logs are
generated from the same corpus the system retrieves, so they inflate every score
([RISK_TAXONOMY §5](RISK_TAXONOMY.md)).

**Alerts:** nightly macro-F1 > 5 points below the trailing 7-day median → open an issue
(ROADMAP Phase 6). Any confirmed wrongful pause → page + automatic demotion to `approve_required`.

---

## 5. Human-in-the-loop

| SLI | Target | Notes |
|---|---|---|
| Time-to-approval p50 | **≤ 2 business hours** | |
| Time-to-approval p95 | **≤ 8 business hours** | Same business day |
| Approval expiry rate | **≤ 1%** | Expiries mean nobody is watching the queue |
| Pending queue depth | **≤ 10** sustained | |
| Case-review completeness | **100%** | `GET /approvals/{id}` returns log, policy text, reasoning, rule matches, history — no second lookup needed |

These are **organisational** objectives, not engineering ones. The system can meet every other SLO
and still be useless if the queue is unattended — an unwatched approval queue is an expensive way
to do nothing.

**Alerts:** queue depth > 25 for 1 h → notify. Any approval within 6 h of its 72 h expiry → page.

---

## 6. Cost

| SLI | Ceiling | Notes |
|---|---|---|
| LLM cost per 1 000 logs | **≤ $2.00** | Computable from `/metrics` alone |
| Cost per log, p99 | **≤ $0.01** | Catches a runaway single request |
| Retry overhead | **≤ 10%** of total LLM spend | A high ratio means an upstream problem |
| Infrastructure, monthly | tracked, not capped | Fixed cost, dominated by Fargate + Multi-AZ RDS + NAT |

```
cost_per_1000 = 1000 * increase(llm_cost_usd_total[24h]) / increase(vendor_logs_processed_total[24h])
```

**Where $2.00 comes from:** the design point is ~1 200 input + ~400 output tokens plus one
embedding ([HLD §6](HLD.md#6-capacity-and-cost)), which on a small-tier model lands well under
$0.50/1000. The ceiling is set at roughly 4× that, so the alarm fires on a genuine regression —
prompt bloat, a retry storm, an accidental model upgrade — rather than on normal variance.
**Re-baseline once the pinned model id is chosen** ([ADR-0001](decisions/0001-llm-provider.md)).

Today per-request spend is **unknown and unbounded**: nothing counts tokens, and an
unauthenticated, uncapped `log_text` (`app/main.py:132-138`) is a direct route to the operator's
API budget.

**Alerts:** cost/1000 > 80% of ceiling for 24 h → ticket. > 100% → page. Daily spend > 2× the
trailing 7-day median → page.

---

## 7. Security and compliance

| SLI | Target |
|---|---|
| Unauthenticated requests reaching the graph | **0** |
| Actions without a matching `action_execution` row | **0** |
| Duplicate pauses per `(vendor, log)` | **0** |
| Decisions missing prompt version or model id | **0** |
| Raw log text in application logs or metric labels | **0** |
| Archive write failures silently swallowed | **0** |

These are **zero-tolerance invariants**, not percentages. Each is asserted by a test
([LLD §7](LLD.md#7-testing-specification)) and monitored in production. A non-zero value is an
incident, not a budget spend.

---

## 8. Error budget policy

| Budget state | Consequence |
|---|---|
| > 50% remaining | Ship features normally. |
| 25–50% | New features need a reliability review. |
| < 25% | **Feature freeze.** Reliability work only until the window rolls. |
| Exhausted | Freeze + written incident review. Autonomy reverts to `approve_required` if enabled. |

The quality budget (§4) is separate and stricter: **a single confirmed wrongful pause exhausts it
immediately**, regardless of the rate. Some errors do not average.

---

## 9. Review cadence

| Item | Cadence | Trigger for change |
|---|---|---|
| §3 latency | Monthly, first 3 months | Re-baseline from real traffic after Phase 5 |
| §4 quality | Every eval run; formally monthly | Model or prompt change resets the baseline |
| §6 cost | Weekly, first month | Model change, prompt change, volume shift |
| §5 human | Monthly | Informs whether an approval UI is worth building |
| This document | Quarterly | Any SLO missed two months running is either wrong or under-resourced — decide which |

---

## 10. Honest gaps

| Gap | Impact | Closes in |
|---|---|---|
| **Nothing here is measured today** | Every target is aspirational | Phase 6 |
| Latency targets are estimates, not observations | May be wrong by 2× in either direction | Phase 5 + 2 weeks |
| Cost ceiling assumes an unchosen model | Could be materially off | [ADR-0001](decisions/0001-llm-provider.md) model pinning |
| Quality targets assume ≥ 200 labeled logs, ≥ 50 real | Without real logs, §4 is **indicative, not measured** | Phase 2 — and if no SME is available to label them, that must be stated now, because it changes §4 from a measurement to a guess |
| No SLO on the procurement API — it is external and unmeasured | A slow ERP eats the request budget | Measure in Phase 5 |
| Human SLOs (§5) assume staffing nobody has committed | The single largest risk to the whole design | Before Phase 5 go-live |
