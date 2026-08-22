# ADR-0004 — Ingestion topology: S3 → SQS → worker → authenticated HTTP webhook

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 2 (commit the scripts) → 5 (harden and deploy)
- **Relates to:** [ADR-0006](0006-idempotency.md), [ADR-0008](0008-api-authentication.md)

## Context

There are two candidate entry points and both partially exist:

- `POST /webhook/vendor-log` (`app/main.py:173-245`) — real, working, **unauthenticated**.
- An S3 → SQS pipeline that exists **only as uncommitted files in the parent directory**
  (ROADMAP §2.3): `../production_pipeline.py` (~16 KB, generates synthetic logs to S3 under
  `raw_logs/year=/month=/day=/`, AES256 SSE, boto3 credential chain, SIGTERM-aware) and
  `../sqs_consumer.py` (~19 KB, long-polls SQS for `ObjectCreated`, fetches the object, POSTs it
  to the webhook, deletes the message only on HTTP 200).

That parent directory is the same git repo at the same commit, is untracked, and contains a
`.env` with live credentials. It is days of work that is one `rm -rf` from gone.

The consumer's delete-only-on-200 behaviour gives **at-least-once** delivery. That is the correct
choice — but it means duplicate delivery is not an edge case, it is the designed steady state.
Wired into today's API, a redelivered message pauses the same vendor twice.

## Decision

**Keep the HTTP webhook as the single entry point into the domain. The SQS consumer is one
client of that webhook, not a second code path.**

```
source systems → S3 (raw_logs/…)  →  S3 Event Notification  →  SQS  →  ingest-worker
                                                                            │
                                                        POST /webhook/vendor-log
                                                        + Authorization + Idempotency-Key
                                                                            ▼
                                                                        vrm-api
```

Binding rules:

1. **One domain entry point.** Every path into the graph — SQS worker, manual curl, a future
   direct ERP push — goes through the same authenticated, validated, idempotent HTTP handler.
   No transport gets a private door into `compiled_graph`.
2. **The worker computes the idempotency key**, as `sha256(vendor_id ‖ normalized_log_text)` using
   the shared normaliser from [ADR-0003](0003-durable-execution.md) — *not* from the SQS
   `MessageId` (which changes on redelivery) and *not* from the S3 key (two keys can hold the same
   log). Content-derived keys survive replays, re-uploads, and queue migrations.
3. **Retry classification is explicit.** The worker deletes the SQS message on `2xx` **and on
   `4xx`** — a malformed log will never become well-formed by being retried, so retrying it is an
   infinite poison loop. Only `5xx`, `429`, and transport failures leave the message for
   redelivery. A `409` (idempotency conflict) is a permanent condition: delete and alert.
4. **Dead-letter queue** after 5 receives, with a CloudWatch alarm on DLQ depth > 0. Today's
   consumer has no DLQ and would loop a poison message forever.
5. **Visibility timeout = 6 × the API request budget** (60s → 360s). Too short and SQS redelivers
   a message that is still being processed, which is a duplicate the idempotency layer must then
   absorb unnecessarily.
6. **Backpressure lives in the worker.** A bounded concurrency semaphore (default 4 in-flight
   requests) and exponential backoff on `429`/`503`. The queue provides the buffer; the worker
   provides the throttle.
7. `../production_pipeline.py` and `../sqs_consumer.py` are **committed verbatim to a branch
   first** (ROADMAP work-order step 3), then refactored to `scripts/generate_synthetic_logs.py`
   and `worker/sqs_consumer.py`. Rescue before refactor.
8. **The S3-write block in the parent's uncommitted `app/main.py` diff must not ship as written.**
   It catches every exception, logs, and continues — silent data loss on the audit archive. In the
   target design, archival is a durable post-commit step, and a failed archive write is an alert,
   not a swallowed log line.

## Consequences

### Positive
- The graph stays transport-agnostic and testable with `TestClient` and zero AWS.
- One place to enforce auth, size caps, rate limits, idempotency, and correlation ids.
- S3 is the durable raw-log archive by construction, which satisfies the audit requirement in
  [ADR-0010](0010-data-retention.md) at no extra cost.
- Swapping SQS for Kafka or a direct ERP push later touches only the worker.

### Negative
- **An extra network hop** per log — worker → ALB → API. Adds a few ms and one more thing that can
  fail. Accepted for the testability and single-choke-point benefits.
- Backpressure is the worker's job rather than the queue's. If throughput ever demands it, the
  escape hatch is to have the worker invoke the graph in-process, which sacrifices the single
  entry point. Not now.
- Two deployable units instead of one.

### Neutral
- The worker holds AWS credentials; the API does not need SQS access at all. That is a cleaner
  IAM split than a combined service would have.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Direct ERP → webhook**, no queue | No buffer. An LLM outage becomes lost logs, since the ERP will not retry for hours. The queue is what makes [ADR-0001](0001-llm-provider.md)'s single-provider risk acceptable. |
| **Worker invokes the graph in-process** | Fastest, but duplicates auth/validation/idempotency logic or skips it, and makes the graph untestable without AWS. Reconsider only under real throughput pressure. |
| **Lambda triggered by S3 directly** | No queue-level backpressure, awkward retry semantics for 30s+ LLM calls, and a poor fit for durable checkpoints. See [ADR-0007](0007-deployment-target.md). |
| **Exactly-once via SQS FIFO** | FIFO dedupe windows are 5 minutes; our redelivery window is longer. Content-hash idempotency in Postgres is stronger and transport-independent. |

## Compliance / verification

- `git ls-files worker/ scripts/ | wc -l` ≥ 4 — the parent-directory work is committed.
- Same S3 object delivered twice → exactly one `action_execution` row, one `PAUSED` log line.
- Poison message (malformed JSON) → deleted after one attempt, DLQ alarm silent, error metric +1.
- API returns `503`; message reappears after the visibility timeout; the run **resumes** from its
  checkpoint rather than restarting.
- `tests/integration/test_webhook.py` passes with **zero sockets opened** — proven by a test that
  fails if any socket is created.

## Revisit when

- Sustained ingest exceeds ~50 logs/second, where the extra hop starts to matter.
- A second producer needs a different delivery contract (e.g. streaming, or ordered per-vendor).
