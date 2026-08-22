# ADR-0010 — Audit trail, data retention, and what a decision must be able to prove

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 2 (schema) → 5 (enforcement)
- **Relates to:** [ADR-0002](0002-vector-store.md), [ADR-0005](0005-autonomy-level.md), [THREAT_MODEL](../THREAT_MODEL.md)

## Context

**Nothing is persisted.** The HTTP response is the only record that a decision was ever made
(ROADMAP §2.1). If the caller drops it, the decision is gone — including the fact that a vendor
was suspended. The response does not even contain the policies used, only
`retrieved_policies_count` (`app/main.py:151-153`).

The ROADMAP names the reason this matters: *"when a vendor argues, you can show them exactly
why."* Today there is nothing to show. A suspension is an act with commercial and contractual
consequences, taken by a system that keeps no records of it.

Separately, the data involved is sensitive: vendor logs contain pricing, defect rates, and
capacity information — commercially confidential, sometimes contractually protected.

## Decision

**Every decision is reproducible from durable storage. Persistence is part of the transaction that
produces a decision, not a side effect that may fail quietly.**

### The reproducibility set

A decision row is complete only if these can be recovered together:

| Element | Where |
|---|---|
| Raw log text + received timestamp + source | S3 `raw_logs/` + `vendor_log` row |
| Normalised text + its `log_hash` | `vendor_log` |
| Retrieved policy chunk ids, versions, ranks, similarity scores | `retrieval` |
| Prompt template **version** and the rendered prompt hash | `run.prompt_version`, `run.prompt_sha256` |
| Model id, temperature, token counts, cost | `llm_call` |
| Raw LLM output, before any post-processing | `decision.llm_raw` (jsonb) |
| `rule_floor`, which rule ids fired, matched spans | `decision.rule_matches` (jsonb) |
| `effective_risk_level` and how it was derived | `decision` |
| Approval: who, when, decision, note | `approval` |
| Action: `action_key`, external txn id, status, reversal | `action_execution` |

**Prompt text is stored by version and hash, not by copy.** `app/prompts/*.md` is in git and
immutable once referenced by a persisted run; the row stores the version string plus the sha256 of
the rendered prompt. Copying full prompt text per row would multiply storage for no gain — but the
hash means tampering is detectable.

### Write ordering — the invariant that makes the audit trail trustworthy

> The decision row is committed **before** the action node runs, in the same transaction as the
> retrieval rows. **No side effect may precede its own audit record.**

If the process dies after committing the decision but before acting, the state is recoverable and
the run resumes ([ADR-0003](0003-durable-execution.md)). If it dies after acting but before
recording, `action_execution` is already `pending` from [ADR-0006](0006-idempotency.md) step 1 —
so an unrecorded action is impossible by construction.

**Archival to S3 is explicitly *not* in this transaction.** It is a post-commit best-effort step
whose failure raises an alert and increments `archive_failures_total`. It must **never** be
silently swallowed — which is precisely what the uncommitted parent-directory diff to
`app/main.py` does today (ROADMAP §2.3, [ADR-0004](0004-ingestion-topology.md) §8).

### Retention schedule

| Data | Retention | Rationale |
|---|---|---|
| `decision`, `approval`, `action_execution` | **7 years** | Contract dispute and audit horizon. These are the legal record. |
| `vendor_log` metadata + `log_hash` | **7 years** | Without it, a decision cannot be tied to its input. |
| Raw log text in S3 | **2 years** hot, then Glacier to 7 years | Bulk of the volume; rarely read after a quarter. |
| LangGraph checkpoints | **30 days** | Operational, not evidentiary. |
| `idempotency_key` | **7 days** | Replay window ([ADR-0006](0006-idempotency.md)). |
| `llm_call` token/cost rows | **13 months** | Year-over-year cost analysis. |
| Application logs | **90 days** | Incident investigation. |
| Golden dataset + eval history | **indefinite**, in git | The quality baseline is a repo artifact. |

### Protection

- **Encryption at rest:** RDS with KMS; S3 with SSE-KMS. The existing pipeline already uses AES256
  SSE (ROADMAP §2.3) — upgrade to SSE-KMS for key-level access control.
- **In transit:** TLS everywhere, including inside the VPC.
- **Append-only in practice.** `decision` and `action_execution` have no `UPDATE` grant for the
  application role beyond the specific status transitions in [ADR-0006](0006-idempotency.md).
  Corrections are new rows referencing the old, never edits.
- **PII:** vendor logs are business-to-business and should not contain personal data, but they
  contain **names and email addresses of vendor staff**. Treated as personal data:
  access-controlled, never emitted in logs or metric labels, and covered by the deletion procedure
  below.
- **No raw log text in application logs, metric labels, trace attributes, or error responses.**
  `app/main.py:197-201` currently logs only length — correct, and the rule is now explicit.
  Structured logs carry `log_hash`, never the text.
- **LLM data handling:** the provider's zero-retention / no-training terms must be confirmed and
  recorded here before production data is introduced. This is a prerequisite for the operator's
  stated plan to move from mock to production data.

### Deletion on request

`scripts/purge_vendor.py --vendor-id V-1234 --reason <ticket>` deletes S3 objects and log text,
**redacts** `vendor_log.raw_text` to `NULL`, and **preserves** `decision`, `approval`, and
`action_execution` rows with `redacted_at` set. The evidentiary chain survives; the content does
not. Retaining the record of a decision while deleting its content is the standard compromise
between a deletion obligation and a legal-hold obligation — and the tension must be resolved with
counsel before production data lands, not after.

## Consequences

### Positive
- A suspension can be defended with the exact log, the exact policies, the exact prompt version,
  and the named approver.
- The audit trail is the drift-detection input: re-scoring a historical decision against a new
  model version is a query.
- Real approval decisions accumulate as labeled data ([ADR-0005](0005-autonomy-level.md)).
- Cost per decision is reconstructable per-vendor and per-model.

### Negative
- Storage grows monotonically; 7-year retention on the legal record is a standing cost. Small in
  absolute terms — the rows are metadata; the text is in cheaper S3 tiers.
- Persisting inside the request path adds latency (~10–20 ms) and makes Postgres a hard dependency
  of a successful response. Consistent with [ADR-0002](0002-vector-store.md).
- Deletion requests are a manual, ticketed procedure. Fine at expected volume; would need
  automating if it becomes routine.

### Neutral
- `WorkflowResult` grows: `retrieved_policy_ids`, `prompt_version`, `model_id`, `thread_id`,
  `correlation_id`, `effective_risk_level`, `rule_matches`. See
  [LLD §5.1](../LLD.md#51-post-webhookvendor-log).

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Log-only audit** (structured logs to CloudWatch) | Logs are lossy, hard to query relationally, and typically retained 90 days — three orders of magnitude short of a contract dispute horizon. |
| **S3-only, no Postgres rows** | Cheap, but "show every CRITICAL decision for vendor X in Q3" becomes a scan. And it cannot be transactional with the action. |
| **Event sourcing** (append-only event log, projections) | A genuinely good fit for an audit-heavy domain, and worth revisiting. Rejected now as materially more machinery than a 3-node graph needs. |
| **Store the full rendered prompt per run** | Complete, but duplicative — the template is in git and the hash proves the render. Revisit if prompt templating ever becomes dynamic per-request. |

## Compliance / verification

- For any `decision` row, `scripts/replay_decision.py --run-id <id>` reconstructs the exact prompt
  from stored version + inputs and its sha256 matches `run.prompt_sha256`.
- Every `action_execution` row joins to a `decision` row with an **earlier** `committed_at`.
  A CI-run assertion, not a convention.
- `grep -rn "raw_log_text\|log_text" app/telemetry.py app/middleware.py` finds no logging of
  content.
- Killing the process after the decision commit but before the action leaves a resumable run and
  zero orphaned pauses.
- `scripts/purge_vendor.py --dry-run` prints exactly which rows redact and which are preserved.

## Revisit when

- Legal or compliance sets a retention schedule different from the one above — **this table is an
  engineering default and needs review by counsel before production data is introduced.**
- Storage cost becomes material.
- Deletion requests become frequent enough to need self-service.
