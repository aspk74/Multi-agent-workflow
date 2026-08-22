# ADR-0006 — Two-layer idempotency: HTTP replay cache + a unique constraint on the side effect

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 5 (implementation)
- **Relates to:** [ADR-0003](0003-durable-execution.md), [ADR-0004](0004-ingestion-topology.md)

## Context

`app/main.py:194` mints a fresh `uuid4()` per request. There is no idempotency key, no dedupe,
and no unique constraint anywhere. Combined with the at-least-once SQS consumer
([ADR-0004](0004-ingestion-topology.md)), **a redelivered webhook pauses the same vendor twice**
and bills two LLM calls to do it.

Duplicates arrive from at least four independent sources, and any single-layer defence misses some
of them:

| Source | Same HTTP body? | Same idempotency key? |
|---|---|---|
| SQS visibility-timeout redelivery | yes | yes |
| Worker crash after send, before delete | yes | yes |
| S3 re-upload of an identical log under a new key | yes | yes (content-derived) |
| Two *different* source systems reporting one real-world event | no (different wrapper) | no |
| Operator curl replay during an incident | maybe | maybe not |

## Decision

**Defend at two layers with different keys and different guarantees. Neither layer trusts the
other.**

### Layer 1 — HTTP replay cache (fast, courteous, best-effort)

- `Idempotency-Key` header is **required** on `POST /webhook/vendor-log`. Absent → `400`.
- Table `idempotency_key(key PK, endpoint, request_sha256, response_status, response_body jsonb,
  state, created_at, expires_at)`.
- Flow:
  1. `INSERT … ON CONFLICT DO NOTHING` with `state='in_progress'`.
  2. Insert won → process the request; on completion write status + body and set `state='done'`.
  3. Insert lost, existing row `state='done'` → return the **stored response byte-identically**,
     with `Idempotency-Replayed: true`.
  4. Insert lost, existing row `state='in_progress'` → `409 Conflict`, `Retry-After: 5`. The
     worker retries; SQS redelivery handles it.
  5. Insert lost, existing `request_sha256` **differs** → `422 Unprocessable Entity`. Same key,
     different body is a **client bug**, and silently serving the old answer would hide it.
- **TTL 7 days**, purged nightly. Beyond that a replay is treated as a new request — and Layer 2
  still catches it.

### Layer 2 — unique constraint on the side effect (slow, absolute)

```sql
CREATE UNIQUE INDEX action_execution_dedupe
    ON action_execution (action_key)
    WHERE status IN ('pending', 'succeeded');
```

with

```
action_key = sha256(vendor_id ‖ "\x00" ‖ normalized_log_text ‖ "\x00" ‖ action_type)
```

using the **same normaliser** as `thread_id` ([ADR-0003](0003-durable-execution.md)).

The action node's contract:

1. `INSERT` the `action_execution` row `pending` **before** the outbound HTTP call.
   `IntegrityError` → a pause for this `(vendor, log, action)` already exists → return the
   existing `external_txn_id`, make **no** network call.
2. Call the procurement API, passing `action_key` as *its* idempotency header too, so the
   downstream system can dedupe independently.
3. Update the row to `succeeded` with the returned `external_txn_id`, or `failed` with the error.
   `failed` rows fall outside the partial index, so a genuine retry is permitted.

The partial index is the load-bearing detail: `pending`/`succeeded` block duplicates, `failed`
does not. Retrying a failure is correct; retrying a success is a second suspension.

**No pause is possible without a row.** The database, not application logic, is what makes this
exactly-once. Every other layer is an optimisation on top of it.

### Layer 0 — free, from ADR-0003

Deterministic `thread_id` means a duplicate that reaches the graph at all **resumes** rather than
re-executes, so retrieval and classification are not repeated either. This saves money; it does
not provide safety. Layer 2 provides safety.

## Consequences

### Positive
- Double delivery costs one LLM call and produces one pause. The ROADMAP Phase 5 exit criterion
  (`grep -c 'execute_vendor_pause.*PAUSED'` equals `1`, not `2`) is satisfied by construction.
- Byte-identical replay responses make the worker's retry logic trivial.
- Layer 2 holds even if a caller sends a fresh key, skips the header path, or an operator replays
  by hand.
- `action_execution` doubles as the complete, queryable audit trail of every side effect.

### Negative
- Two more tables and a nightly purge job.
- A `409` while `in_progress` is a real state clients must handle. Documented in
  [LLD §5](../LLD.md#5-http-api-contract).
- Content-hash dedupe **cannot distinguish two genuinely distinct real-world events that produced
  byte-identical text.** Two separate late shipments described in identical words collapse to one
  case. Judged correct-by-default for an irreversible action; the escape hatch is the audited
  `X-Force-New-Run` header.
- A crash between step 1 and step 3 leaves a `pending` row that blocks retries. A reaper marks
  `pending` rows older than 15 minutes as `failed` (unblocking retry) **only after** querying the
  procurement API for that `action_key` — it must never guess.

## Alternatives considered

| Option | Why rejected |
|---|---|
| HTTP layer only | Loses to key rotation, TTL expiry, direct calls, and operator replay. |
| Action layer only | Works, but wastes an LLM call per duplicate and returns a non-identical response. |
| SQS FIFO exactly-once | 5-minute dedupe window; our redelivery window is longer. Ties correctness to one transport. |
| `MessageId` / S3 key as the key | Both change on redelivery or re-upload. Content is the only stable identity. |
| Advisory locks | Not durable across restarts; solves concurrency, not replay. |

## Compliance / verification

- Same payload + same key, twice → **byte-identical** bodies, second carries
  `Idempotency-Replayed: true`, and exactly one `PAUSED` log line.
- Same key + different body → `422`.
- Different key + same body → one `action_execution` row (Layer 2 catches it), and the second
  response references the same `external_txn_id`.
- `tests/unit/test_idempotency.py` drives all five Layer-1 branches.
- Concurrency test: 10 parallel POSTs, identical key → 1× `200`, 9× `409`, one action row.

## Revisit when

- A legitimate need arises to process byte-identical logs as distinct events at volume, making
  the `X-Force-New-Run` escape hatch routine rather than exceptional.
- The procurement API gains native idempotency guarantees strong enough to demote Layer 2.
