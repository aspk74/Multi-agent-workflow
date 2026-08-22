# ADR-0009 — Deterministic guardrails: the model can only ever raise the risk level

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 3 (implementation)
- **Relates to:** [ADR-0005](0005-autonomy-level.md), [THREAT_MODEL §4](../THREAT_MODEL.md), [RISK_TAXONOMY](../RISK_TAXONOMY.md)

## Context

The final risk level is decided **entirely** by one LLM call (`app/agents.py:148`) whose output
routes directly to an irreversible action (`app/graph.py:65`). Two problems follow:

1. **Written policy rules are not enforced.** `POLICY-003` states that "two or more MNCs within 90
   days automatically escalates the vendor to CRITICAL risk tier" (`app/tools.py:49-51`). That is
   a **deterministic, countable, non-negotiable rule** — and no code enforces it. Whether it fires
   depends on whether the retriever happened to surface POLICY-003 and whether the model happened
   to apply it. A rule enforced by vibes is not a rule.
2. **The model is the sole authority, and its input is attacker-controlled.** Vendor log text
   arrives from outside and lands in the prompt (`app/agents.py:126-141`). Anything that persuades
   the model to output `LOW` suppresses the entire risk pipeline.

The system also has no memory. Every log is judged in isolation, so the pattern the ROADMAP names
as the whole point — "a supplier who is late by four days every single month" — is invisible by
construction.

## Decision

**Compute a deterministic `rule_floor` from code and vendor history, and take the final level as
`max(llm_verdict, rule_floor)`. The model can raise the level. It can never lower it.**

```python
effective_risk_level = max(llm_risk_level, rule_floor, key=RISK_ORDER.index)
```

`app/rules.py` is a **pure function of `(vendor_id, log_text, vendor_history, now)`** — no LLM, no
network, no randomness. It is unit-testable, auditable line-by-line, and reviewable by a
procurement lawyer who does not read Python well.

### The graph gets a node, not a helper

```
researcher → classifier → rules → route_by_effective_risk → …
```

`rules` is its own node so the floor is **computed after** the model and merged over it. Placing
it before, or inside the classifier, would let a later step overwrite it. The floor is applied
last on purpose.

### Launch rule set

| Rule | Trigger | Floor | Source |
|---|---|---|---|
| `R-001` | ≥ 2 Major Non-Conformances for this vendor in 90 days | `CRITICAL` | POLICY-003 |
| `R-002` | Unauthorised price increase > 10% asserted in the log | `CRITICAL` | POLICY-002 |
| `R-003` | Delay > 7 calendar days on a critical-path shipment | `HIGH` (an MNC) | POLICY-001 |
| `R-004` | Defect rate > 0.5% asserted in the log | `HIGH` | POLICY-003 |
| `R-005` | Unauthorised price increase 5–10% | `HIGH` | POLICY-002 |
| `R-006` | ≥ 3 `HIGH`+ decisions for this vendor in 180 days | `CRITICAL` | POLICY-005 (pattern) |
| `R-007` | Vendor already paused and a new `HIGH`+ arrives | `CRITICAL` + no-op action | operational |

Each rule records `rule_id`, the matched span of input text, and the extracted value onto the
decision row. "The floor fired" is never an unexplained state.

### Numeric extraction is deterministic too

`R-002` through `R-005` need numbers out of free text ("increased prices by 40%", "15 days late").
These are extracted by **anchored regex in `app/rules.py`**, not by the model — asking the model
for the number and then applying a rule to it hands the attacker the rule.

Extraction is **conservative on ambiguity**: no confident match → the rule does not fire → the
floor stays `LOW` → the LLM verdict stands unmodified. A guardrail that misfires on ambiguity is
a false-pause generator, which is the expensive error ([ADR-0005](0005-autonomy-level.md)).
Recall is the model's job; the floor's job is precision.

### Vendor history

`app/rules.py` reads a **materialised** `vendor_history` view (decisions, MNC counts, current
pause state, all windowed) rather than issuing ad-hoc queries. History is read once per run, in
one query, and passed in — so the function stays pure and the tests need no database.

### Hard invariant

> **No code path may lower a level below `rule_floor`.** Not the model, not the prompt, not a
> config flag, not an operator override. Lowering is only possible by a human **rejecting** an
> approval ([ADR-0005](0005-autonomy-level.md)), which is recorded, attributable, and takes no
> action — it does not rewrite the assessment.

This is enforced by a property test, not by convention.

## Consequences

### Positive
- Written policy becomes executable and provable. "Show me that POLICY-003 is enforced" is
  answered by a test id.
- The most dangerous prompt-injection outcome — talking the system *down* to `LOW` — is bounded:
  an injected log that trips `R-001` stays `CRITICAL` no matter what the model emits.
- The system gains cross-log memory, which is the "quiet pattern" the ROADMAP identifies as the
  real value.
- Rules run in microseconds and cost nothing; several cases never need to consult the model's
  judgement at all.

### Negative
- **Rules can only push levels up**, so a mis-specified rule directly manufactures false pauses —
  the error class we care most about. Mitigated by conservative extraction, and by every rule
  being individually measured on the golden set before it ships enabled.
- Regex over free text is brittle. "prices went up by nearly half" trips nothing. **Accepted:**
  the floor is a safety net, not the classifier. Coverage is the LLM's job.
- Rules and the policy corpus can drift. Every rule cites its policy id, and a CI check fails if a
  cited policy id is absent from `data/policies/`.
- Vendor history makes the run stateful: replaying an old log now yields a different (possibly
  higher) level. Evals therefore pin history to a fixture, never to live data.

### Neutral
- `route_by_risk` (`app/graph.py:45-72`) is renamed and rewritten to route on
  `effective_risk_level`. The old function's `None` guard behaviour is preserved.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Trust the LLM alone** (today) | Written rules unenforced; a single prompt injection suppresses the pipeline; no cross-log memory. |
| **Rules only, no LLM** | Cannot read free text. This is the `baselines/keyword_classifier.py` baseline (ROADMAP Phase 3) — kept as a benchmark, not as the system. If the LLM cannot beat it by ≥ 15 macro-F1 points, the LLM is not paying for itself. |
| **Let rules lower the level too** ("this vendor is strategic, cap at MEDIUM") | Creates a suppression mechanism, which becomes the highest-value target for both attackers and internal pressure. Exceptions belong in the human approval step, where they are attributed. |
| **Ask the LLM to apply the rules**, feeding them in the prompt | Already effectively the design, and already demonstrably unreliable — POLICY-003 is in the corpus today and still unenforced. Also fully attacker-reachable. |
| **A rules engine (Drools, JSON-logic)** | Configurable at runtime, but a runtime-editable path to raising risk levels is a control that needs its own auth model. Python + tests + code review is the stronger audit trail at seven rules. |

## Compliance / verification

- **The ROADMAP Phase 3 gate:** a synthetic log with 2 MNCs in 90 days yields `CRITICAL` **even
  when the LLM is stubbed to return `LOW`**.
- Property test: for 10,000 random `(llm_level, rule_floor)` pairs,
  `effective >= rule_floor` always holds.
- `tests/unit/test_rules.py` covers every rule id, both firing and not firing, plus the ambiguous
  inputs that must **not** fire.
- CI check: every `rule.policy_id` resolves to a document in `data/policies/`.
- Each rule's individual false-positive rate is reported by `evals/run_eval.py`; a rule above
  **1%** ships disabled until fixed.

## Revisit when

- A rule's measured false-positive rate exceeds 1% on the golden set.
- The policy corpus lands in Phase 2 and reveals countable rules not in the launch set.
- Extraction misses become the dominant error class in eval, which would argue for a small
  dedicated extraction model — feeding a rule, still never bypassing it.
