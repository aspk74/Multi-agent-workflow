# ADR-0003 — Durable execution: LangGraph + Postgres checkpointer, deterministic thread_id

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 3 (implementation)
- **Relates to:** [ADR-0005](0005-autonomy-level.md) (the interrupt needs a checkpointer), [ADR-0006](0006-idempotency.md)

## Context

`app/graph.py:119` calls `builder.compile()` **with no checkpointer**. There is no `thread_id`
anywhere in the repo. Consequences today:

- A crash between `researcher` and `classifier` loses the run entirely. The caller sees a 500;
  nothing records that retrieval already happened.
- A retry re-runs **every** node, including a second LLM call — double cost for one log.
- Human-in-the-loop is impossible. `interrupt()` requires a checkpointer to have somewhere to
  suspend to. Without one, [ADR-0005](0005-autonomy-level.md) cannot be implemented at all.
- The ingestion path is at-least-once by design (`../sqs_consumer.py` deletes the SQS message
  only on HTTP 200). Redelivery therefore means full re-execution.

## Decision

**Compile the graph with `PostgresSaver` against the same Postgres instance as the policy index,
and derive `thread_id` deterministically from the log content.**

```
thread_id = "vrm:" + sha256(vendor_id + "\x00" + normalized_log_text).hexdigest()[:32]
```

`normalized_log_text` is NFC-normalised, `\r\n`→`\n`, trailing whitespace stripped. The
normalisation rule is defined once in `app/hashing.py` and is **the same function** that produces
the action-layer idempotency key ([ADR-0006](0006-idempotency.md)); the two must never diverge.

Why deterministic rather than a fresh UUID: a redelivered SQS message for the same S3 object
produces the **same** `thread_id`, so LangGraph resumes the existing run from its last completed
node instead of starting a parallel one. Redelivery becomes resumption, for free. A random
`thread_id` — the obvious choice — would silently double every LLM call under at-least-once
delivery.

Supporting rules:

1. **The checkpointer is a lifespan-managed resource**, not a module-level global. `app/graph.py`
   exports `build_graph(checkpointer)`; `app/main.py` compiles it during startup and stores it on
   `app.state`. Graph *construction* stays synchronous and import-safe; the *connection* does not.
2. **Nodes must be re-entrant.** Any node may execute twice if a crash lands between its work and
   its checkpoint write. Therefore no node performs an un-guarded side effect. The single
   side-effecting node (`action`) is protected by a unique constraint, not by hope
   ([ADR-0006](0006-idempotency.md)).
3. **Checkpoint retention:** 30 days, enforced by a nightly job. Long enough for any approval to
   resolve ([SLOS §5](../SLOS.md)), short enough that the table does not grow unbounded.
4. **Graceful shutdown.** On SIGTERM the API stops accepting new requests, allows in-flight graph
   runs `min(remaining_budget, 30s)` to reach their next checkpoint, then exits. ECS
   `stopTimeout` is set to 60s so the platform does not SIGKILL mid-write.
5. Every run row records `thread_id`, `correlation_id`, `prompt_version`, `model_id`, and node
   timings. `thread_id` is returned in the API response and in the `X-Thread-Id` header, so an
   operator can resume or inspect any run by hand.

## Consequences

### Positive
- **Crash-safety with no manual repair.** `kill -9` mid-classifier, restart, re-invoke → the run
  finishes and `researcher` shows exactly one execution (the ROADMAP Phase 3 kill test).
- Redelivery is idempotent at the *execution* layer, before any business logic runs.
- Human approval becomes implementable: `interrupt()` suspends to a durable checkpoint, and the
  process can be redeployed while a case waits.
- Node-level timings land in the checkpoint tables, which is free observability groundwork.

### Negative
- Postgres availability now gates **all** request handling, not just retrieval. Accepted: see
  [ADR-0002](0002-vector-store.md).
- Checkpoint writes add roughly 5–15 ms per node. Against a multi-second LLM call this is noise.
- Deterministic `thread_id` means a genuinely-resent-but-genuinely-new log with byte-identical
  text collides with the original. **This is intended** — that is what deduplication means — but
  it must be documented for operators, who can force a new run with an explicit
  `X-Force-New-Run: true` header (audited, requires an operator key).
- The checkpoint tables hold **raw log text**, which may contain commercially sensitive vendor
  data. Retention and encryption are covered in [ADR-0010](0010-data-retention.md).

### Neutral
- `app/graph.py`'s module-level `compiled_graph` (`app/graph.py:119`) is deleted. `app/main.py:31`
  and `app/main.py:66,169` must move to `request.app.state.graph`.

## Alternatives considered

| Option | Why rejected |
|---|---|
| No checkpointer (today) | Cannot support approval, cannot survive a crash, doubles cost on every retry. |
| `MemorySaver` | Per-process. Useless across the two Fargate tasks and lost on every deploy. |
| Redis checkpointer | Another datastore, and durability guarantees weaker than the one we already run. |
| External orchestrator (Temporal, Step Functions) | Genuinely stronger durability, but replaces LangGraph's programming model wholesale and adds an infrastructure component larger than the app. Revisit only if the graph grows past ~10 nodes or needs multi-day timers. |
| Random UUID `thread_id` | Silently double-executes under at-least-once delivery — the exact hazard this system has. |

## Compliance / verification

- **Kill test:** start a run, `kill -9` during the classifier node, restart, re-invoke with the
  same `thread_id` → the run completes and logs show `researcher_node` executed **once in total**.
- `grep -n "compile()" app/graph.py` shows a `checkpointer=` argument on every call.
- `tests/unit/test_hashing.py` asserts `thread_id` and the action idempotency key derive from the
  same normaliser, and that both are stable across `PYTHONHASHSEED` values.
- Restart-under-load: rolling-deploy the API while 20 runs are in flight; zero runs are lost and
  zero duplicate pauses are recorded.

## Revisit when

- The graph needs timers longer than the 30-day checkpoint retention.
- Checkpoint write latency exceeds 50 ms p95.
- The workflow needs fan-out/fan-in parallelism, at which point state reducers
  (`app/state.py:11-12` deliberately avoids them today) must be revisited alongside this ADR.
