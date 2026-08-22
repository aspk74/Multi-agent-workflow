# ADR-0005 — Autonomy: the agent proposes; a human disposes, until the numbers earn otherwise

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 3 (interrupt) → 5 (gate) → 6 (review)
- **Relates to:** [ADR-0003](0003-durable-execution.md), [ADR-0009](0009-deterministic-guardrails.md), [SLOS §4](../SLOS.md)

## Context

Today the system **acts autonomously, immediately, and irreversibly, on an unauthenticated HTTP
request.** `route_by_risk` (`app/graph.py:45-72`) sends any `HIGH`/`CRITICAL` verdict straight to
`action_node` (`app/agents.py:165-228`), which calls `execute_vendor_pause`
(`app/tools.py:154-212`). There is no interrupt, no approval, no reversal path, and no auth in
front of it (`app/main.py:173-245`).

The asymmetry of errors is the whole decision:

| Error | Cost |
|---|---|
| **False pause** (LOW/MEDIUM judged HIGH/CRITICAL) | A legitimate supplier's purchase orders stop. Production lines can starve. Contract dispute, possible damages, certain loss of trust. Recovery takes days and involves lawyers. |
| **Missed escalation** (HIGH/CRITICAL judged LOW/MEDIUM) | The status quo. A human notices later, as they do today. Recovery is a normal escalation. |

These are not comparable, and no accuracy figure alone captures the difference. The system has
**never been graded** — there is no eval harness, no labeled data, and no measured false-pause
rate. Granting irreversible authority to an ungraded classifier is not a risk appetite, it is an
absence of information.

## Decision

**The agent proposes. A human disposes. Autonomy is earned per-level, by measurement, and is
revocable.**

### Three autonomy modes, config-selected per risk level

`AUTONOMY_MODE` ∈ `{propose_only, approve_required, autonomous}`, resolvable per level via
`AUTONOMY_OVERRIDES` (e.g. `CRITICAL=autonomous`).

| Effective level | Launch behaviour | Node path |
|---|---|---|
| `LOW`, `MEDIUM` | Record the decision. No action, ever. | `… → persist → END` |
| `HIGH` | `approve_required` | `… → approval_gate → interrupt() → action \| END` |
| `CRITICAL` | `approve_required` | same |

**`HUMAN_APPROVAL_REQUIRED=true` is the default in `.env.example`, and the deploy fails closed:**
an unset or unparseable `AUTONOMY_MODE` resolves to `approve_required`, never to `autonomous`.

### The approval surface is API-only, for now

Pending cases live in Postgres and are exposed as:

- `GET /approvals?status=pending` — the queue, newest first
- `GET /approvals/{approval_id}` — full case: log text, retrieved policy ids and text, LLM
  reasoning, rule floor, confidence, vendor history
- `POST /approvals/{approval_id}/decide` — `{"decision": "approve"|"reject", "note": "..."}`,
  requires an **operator** credential distinct from the ingest credential
  ([ADR-0008](0008-api-authentication.md))

No UI in this phase. A Slack app or a review page is a later, separate ADR — deferring it is what
keeps the interrupt implementable in Phase 3 instead of blocked on frontend scope.

### Approval timeout

A case unapproved after **72 hours** expires to `EXPIRED`, takes **no action**, and raises an
alert. Silence must never be read as consent for an irreversible act.

### The promotion gate — the only way autonomy increases

`CRITICAL` may be promoted to `autonomous` **only when all five hold**:

1. Phase 4 false-pause rate ≤ **2%** on the golden set, with ≥ 200 labeled logs.
2. ≥ **30 consecutive days** of real traffic under `approve_required`.
3. ≥ **50 human decisions** on `CRITICAL` cases in that window, with a human-agreement rate
   ≥ **95%** (humans approved ≥ 95% of what the agent proposed at CRITICAL).
4. A **tested reversal procedure** exists — see [ADR-0008](0008-api-authentication.md) §Reversal
   and ROADMAP Open Decision 8. **If the procurement system exposes no un-pause endpoint,
   autonomy is permanently off the table** and this ADR's promotion gate is unreachable by
   construction.
5. The promotion is a PR that edits this ADR, links the evidence, and is reviewed.

`HIGH` is **not** promotable under this ADR. Revisit separately after CRITICAL has run
autonomously for 90 days.

### Demotion is automatic and needs no meeting

Any of the following reverts the level to `approve_required` immediately, via alert-triggered
config change: false-pause rate over a trailing 7 days > 2%; a single confirmed wrongful pause;
macro-F1 falling > 5 points below the trailing 7-day median ([SLOS §4](../SLOS.md)); or a change
to the model id or prompt version. **A model or prompt change resets the 30-day clock.** Autonomy
is a property of a *measured configuration*, not of the project.

## Consequences

### Positive
- The expensive error becomes structurally impossible at launch. No pause happens without a named
  human and a timestamp.
- Every approval decision is labeled data. The approval queue **is** the real-log labeling
  pipeline for ROADMAP Phase 2's "50 real logs" requirement, at zero extra cost.
- Human agreement rate is a live, honest quality signal — better than any offline metric.
- Promotion criteria are numeric and falsifiable, so "should we let it act?" is answered by a
  query, not by opinion.

### Negative
- **Latency for HIGH/CRITICAL becomes human-scaled** — hours, not seconds. This is why
  [SLOS §5](../SLOS.md) tracks time-to-approval as a *separate* SLO; folding it into the request
  latency SLO would make that number meaningless.
- Someone must actually watch the queue. An unstaffed queue makes the system a very expensive
  way of doing nothing. Mitigated by the 72h expiry alert.
- API-only approval is friction. If the agreement rate is high but throughput is low, that is
  evidence for building the UI, and the queue data will say so.
- The interrupt requires the checkpointer from [ADR-0003](0003-durable-execution.md). These two
  ADRs stand or fall together.

### Neutral
- `route_by_risk` (`app/graph.py:45-72`) is rewritten to route on the **effective** level (LLM
  verdict ∨ deterministic rule floor, see [ADR-0009](0009-deterministic-guardrails.md)), not on
  the raw LLM verdict.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Act autonomously now** (status quo) | Irreversible action from an ungraded classifier reachable by an unauthenticated POST. |
| **Propose only, forever** | Gives up the automation benefit permanently and provides no path to earn it. Also removes the incentive to measure. |
| **Confidence-threshold autonomy** (act when `confidence_score` > 0.95) | `confidence_score` (`app/models.py:33-37`) is a **self-report from the model**, not a calibrated probability. It is the first thing a prompt-injection attack manipulates. Unusable as a safety gate until calibration is measured — and if it were calibrated, it would still be model-controlled. |
| **Auto-act with a delayed reversal window** (pause now, auto-unpause in 1h if a human objects) | Halves the blast radius but does not remove it — a supplier's PO system is disrupted the moment the pause lands. Also requires the same reversal endpoint whose existence is unconfirmed. |

## Compliance / verification

- `.env.example` ships `HUMAN_APPROVAL_REQUIRED=true` and `AUTONOMY_MODE=approve_required`.
- A `CRITICAL` classification with no approval produces **zero** `action_execution` rows, and one
  `approval` row in state `pending`.
- Booting with `AUTONOMY_MODE=""` or a typo resolves to `approve_required` and logs a warning.
- `tests/unit/test_routing.py` asserts every level × mode combination, including that `HIGH` can
  never reach `action` while mode is `approve_required`.
- An expired approval leaves `action_taken = "expired:no_action"` and fires an alert.

## Revisit when

- All five promotion criteria are met (a PR against this file).
- Any demotion trigger fires.
- ROADMAP Open Decision 8 is answered — the presence or absence of an un-pause endpoint decides
  whether this ADR is a phase or a permanent state.
