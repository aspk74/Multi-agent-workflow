# ADR-0001 — LLM provider, model pinning, and the client abstraction

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 3 (implementation)
- **Relates to:** [ADR-0002](0002-vector-store.md) (embeddings come from the same provider), [ADR-0009](0009-deterministic-guardrails.md)

## Context

The classifier is the only place in the system where a model is invoked. Today it is built
wrong in four independent ways:

- The client is constructed **inside the request path**, on every call — `app/agents.py:115-119`.
  Every request pays TLS handshake and client-init cost, and no connection pooling is possible.
- It sets only `model`, `google_api_key`, `temperature`. **No `timeout`, no `max_retries`,
  no fallback.** A single provider blip becomes an HTTP 500 to the caller (`app/main.py:216-225`).
- The model id is unvalidated. `app/config.py:44` defaults to `gemini-3.5-flash`; `README.md:50`
  says `gemini-2.0-flash`. Nothing reconciles them. A wrong id fails at the first live
  classification, in production, not at startup.
- Docs across the repo claim OpenAI (`README.md:3`, `app/main.py:82`, `app/models.py:21`) while
  the code imports `langchain_google_genai` (`app/agents.py:22`). Eleven such drifts are
  catalogued in ROADMAP §2.2.

The operator has a **paid OpenAI API key** and no comparable Gemini commitment. Provider choice
here is therefore settled by procurement reality, not by a benchmark.

## Decision

**Use OpenAI as the single LLM and embedding provider, pinned to exact model ids, behind one
client factory at `app/llm.py`.**

1. `app/llm.py` exposes `get_chat_model()` and `get_embeddings()`. Both are process
   singletons built **once during FastAPI lifespan startup** — never inside a node.
2. The classifier model id is config (`OPENAI_CLASSIFIER_MODEL`) with **no default that is a
   guess**. Startup calls the provider's model-list endpoint and **exits non-zero** if the
   configured id is absent. A wrong model id is a boot failure, not a 3am incident.
3. Structured output uses OpenAI **strict JSON-schema mode**
   (`with_structured_output(RiskClassification, method="json_schema", strict=True)`), not
   prompt-coaxed JSON. Schema violations become a typed `LLMSchemaViolation`, not a parse crash.
4. Every call carries an explicit timeout and a bounded retry budget (see
   [LLD §6.3](../LLD.md#63-timeout--retry-matrix)). Retries are attempted only on 429 /
   5xx / connect / read-timeout. A 400 or a schema violation is **never** retried.
5. Exhausting the budget raises a typed `LLMUnavailable`. The graph checkpoints and the API
   returns `503` with `Retry-After`, so the SQS consumer redelivers instead of dropping the log.
6. `model_id` and `prompt_version` are stamped onto **every** persisted decision. A model change
   is traceable to the rows it affected.
7. **No second provider.** Availability comes from retries plus queue redelivery, not from a
   fallback model that would need its own eval baseline.

`temperature=0` stays, but the docstring at `app/agents.py:101` claiming it "ensures
deterministic, reproducible classifications" is **false and must be corrected**. Temperature 0
reduces sampling variance; it does not make an LLM deterministic. Determinism is measured, not
asserted — see [SLOS §4](../SLOS.md).

### Model ids are pinned in config, verified at boot

The exact ids are **deliberately not hardcoded in this ADR.** Provider model catalogues change
faster than documents do, and a stale id copied out of an ADR is exactly the failure mode point 2
exists to prevent. The binding rules are:

| Role | Config var | Selection rule |
|---|---|---|
| Classifier | `OPENAI_CLASSIFIER_MODEL` | Cheapest OpenAI model that clears the Phase 4 gates (macro-F1 ≥ 0.80, false-pause ≤ 2%). Start on the small/mini tier; escalate only if evals fail. |
| Embeddings | `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small`, 1536 dimensions. Fixes the pgvector column width — see [ADR-0002](0002-vector-store.md). |

Before Phase 3 begins, run `python scripts/verify_models.py` (a Phase 3 deliverable) to list the
live catalogue and write the chosen ids into `.env.example` with a dated comment.

## Consequences

### Positive
- One provider, one key, one bill, one rate-limit budget to reason about.
- Model changes become deliberate PRs with an eval diff — which is the point of pinning.
- Strict JSON-schema mode removes a whole class of parse failures.
- The docs that already say "OpenAI" become **true** rather than needing removal.

### Negative
- **Single point of failure.** An OpenAI outage stops classification entirely. Mitigated by
  at-least-once queue redelivery ([ADR-0004](0004-ingestion-topology.md)) and durable
  checkpoints ([ADR-0003](0003-durable-execution.md)): logs queue up, nothing is lost, the
  backlog drains on recovery. Accepted explicitly — a fallback provider costs more in eval
  surface than it buys in availability at this volume.
- Swapping providers later means re-running the full eval and re-baselining prompts. `app/llm.py`
  makes it a one-file code change, not a one-file *quality* change.
- Embedding-model choice is now load-bearing on the pgvector schema. Changing it is a reindex.

### Neutral
- `langchain-google-genai` is dropped; `langchain-openai` is added. Cost-neutral dependency swap.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Keep Gemini | No paid key. Would require a new commercial relationship to run production traffic. |
| Switch to Claude | Same objection — no key held. Reconsider if OpenAI structured output underperforms on the adversarial set. |
| Primary + cross-provider fallback | Doubles the eval surface: both paths must be graded, and a decision's quality becomes provider-dependent and non-reproducible. Queue redelivery already covers the outage case. |
| No abstraction, call the SDK inline | Reproduces today's bug (`app/agents.py:115`) — untestable, un-stubbable, and there is no seam at which to inject retries. |

## Compliance / verification

- `grep -rn "ChatOpenAI\|OpenAIEmbeddings" app/ | grep -v "app/llm.py"` returns **nothing**.
  The only module allowed to construct a provider client is `app/llm.py`.
- Booting with `OPENAI_CLASSIFIER_MODEL=does-not-exist` exits non-zero before serving traffic.
- `curl … | jq -r '.model_id, .prompt_version'` returns two non-empty values on every response.
- `tests/unit/test_llm_retry.py`: three injected transport errors still succeed; a fourth raises
  `LLMUnavailable`, not bare `Exception`.

## Revisit when

- The pinned classifier model is deprecated or announced end-of-life.
- Measured provider availability drops below 99.5% over a rolling 30 days — at which point the
  fallback-provider alternative is re-opened with real numbers behind it.
- The adversarial eval set (Phase 4) shows prompt-injection leakage attributable to the model
  rather than to the prompt.

## Amendment to ROADMAP Phase 1 exit criteria

ROADMAP Phase 1 states: `grep -ci openai README.md app/main.py app/models.py app/tools.py`
returns `0`. That criterion was written to purge **false** OpenAI claims from a Gemini codebase.
Under this ADR those claims become true, and the criterion inverts.

The real requirement was always **docs must match code**, never *"the string OpenAI must not
appear"*. It splits across two phases, because the provider migration is Phase 3 work and Phase 1
cannot pre-empt it:

**Phase 1 (now)** — the code still imports `langchain_google_genai` (`app/agents.py:22`), so the
docs must say Gemini:

> The provider named in `README.md` matches the package imported by `app/agents.py`, and no doc
> claims an integration the code does not have. Verify:
> `grep -ci "openai\|pinecone" README.md` returns `0` **as a claimed integration** — mentions that
> explicitly describe an unused dependency or a stale TODO are the correction, not a violation.

**Phase 3 (after the migration)** — once `app/llm.py` exists and Gemini is gone:

> `grep -rli "gemini" README.md app/ | wc -l` returns `0`, and the provider named in `README.md`
> matches the package imported by `app/llm.py`.

ROADMAP §4 has been updated to match.
