# ADR-0002 — Policy corpus storage and retrieval: Postgres + pgvector

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 2 (implementation)
- **Relates to:** [ADR-0001](0001-llm-provider.md) (embedding model fixes the vector width), [ADR-0003](0003-durable-execution.md) (same Postgres instance)

## Context

"Policy retrieval" today is a **six-entry Python dict** (`app/tools.py:34-69`) selected by
substring match over thirteen hardcoded keywords (`app/tools.py:71-85`). It has three defects
beyond being fake:

1. **Non-deterministic output order.** `matched_topics` is a `set` (`app/tools.py:126`) and is
   iterated to build the list (`app/tools.py:135`). Python randomises string hashing per process,
   so **the policy order inside the prompt changes between server restarts** — which changes the
   prompt, which can change the verdict. This directly contradicts the determinism claim at
   `app/agents.py:101`.
2. **No provenance.** `WorkflowResult` returns `retrieved_policies_count` (`app/main.py:151-153`)
   — a bare integer. Which policies drove a decision is unrecoverable after the response is sent.
   When a vendor disputes a suspension, there is nothing to show them.
3. **`langchain-pinecone` is a hard dependency** (`pyproject.toml:19`) that is **never imported**.
   It appears only in a commented-out block (`app/tools.py:108-121`) that references
   `settings.openai_api_key` — a field that does not exist in `Settings` (`app/config.py:26-64`).

Corpus size at target state is **tens to low hundreds of policy documents** (ROADMAP Phase 2
requires ≥ 20 files), chunked to perhaps 1–5k chunks. That is three orders of magnitude below
where a dedicated vector service earns its keep.

## Decision

**Store policy chunks and their embeddings in the same Postgres instance used for LangGraph
checkpoints, using the `pgvector` extension. Drop Pinecone entirely.**

1. `data/policies/*.md` in git is the **source of record**. Postgres is a derived index,
   rebuildable at any time by `scripts/build_index.py`. Losing the index is an inconvenience,
   never a data-loss event.
2. Each chunk row carries `policy_id`, `policy_version`, `chunk_index`, `text`,
   `embedding vector(1536)`, `source_path`, and `content_sha256`. Schema in
   [LLD §4.2](../LLD.md#42-policy_chunk).
3. Vector width is **1536**, fixed by `text-embedding-3-small` ([ADR-0001](0001-llm-provider.md)).
   Changing the embedding model is a schema migration plus a full reindex, and is an ADR-level
   decision, not a config tweak.
4. Index: **HNSW** with cosine distance (`vector_cosine_ops`). At this corpus size an IVFFlat
   index would need retraining after bulk loads for no measurable gain.
5. Retrieval is **ordered deterministically and totally**: `ORDER BY embedding <=> $1 ASC,
   policy_id ASC, chunk_index ASC`. The trailing tiebreakers make ties resolve identically across
   processes — this is the fix for defect 1, and it is a schema-level guarantee rather than a
   `sorted()` call someone can delete.
6. **Retrieved chunk ids are persisted** in a `retrieval` join table against the run, and
   returned in the API response as `retrieved_policy_ids`. This fixes defect 2 and is what makes
   a decision defensible to a vendor.
7. Indexing is **idempotent by content hash.** Re-running `build_index.py` on an unchanged corpus
   is a no-op; a changed file re-embeds only its own chunks.

Retrieval is a **hybrid** query, not pure vector search: `k=8` by cosine similarity, unioned with
any chunk whose `policy_id` is named literally in the log text, then truncated to the top 5 by
score. Vendor logs cite policy ids explicitly often enough ("this breaches POLICY-002") that
ignoring the literal signal wastes free precision.

## Consequences

### Positive
- **One datastore.** One backup, one failover, one connection pool, one thing to run locally.
- A decision and the policy ids that produced it are written in the **same transaction** — they
  cannot drift apart. A separate vector service makes that a two-phase problem with no rollback.
- No per-month vector-service bill.
- `docker compose up postgres` gives a developer the complete data tier. No cloud account needed
  to run the test suite.
- Deterministic ordering becomes a property of the query, not of Python's hash seed.

### Negative
- Postgres is now on the critical path for **both** retrieval and checkpointing. Its outage is a
  total outage. Mitigated by RDS Multi-AZ ([ADR-0007](0007-deployment-target.md)) and by the fact
  that a checkpoint-less system cannot serve traffic anyway.
- Embedding + vector search compete with OLTP for the same CPU. At target volume
  ([SLOS §3](../SLOS.md)) this is negligible; past ~100k chunks it would need a read replica.
- Migrating to a dedicated vector service later is roughly **one week** of work if the corpus
  outgrows this. That risk is accepted knowingly.

### Neutral
- `langchain-pinecone` is removed from `pyproject.toml`. Nothing imports it today, so removal is
  a no-op at runtime.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Pinecone** | Already a dependency, never used. Adds a second failure domain, a second bill, and cross-store consistency problems, to manage a corpus that fits in a single Postgres table. Its scaling advantage is unreachable at this size. |
| **In-process FAISS**, rebuilt at container start | Fastest and simplest, but the index is per-container: two Fargate tasks can serve different corpus versions during a rolling deploy, so two identical logs get different policies. Also blocks corpus updates without a redeploy. |
| **Keep keyword matching, skip embeddings** | Cannot generalise past the 13 hardcoded strings. A log saying "consignment arrived nine days late" matches none of them and would retrieve only the default policy. |
| **Elasticsearch / OpenSearch** | Strong hybrid search, but a whole JVM cluster to operate for < 5k chunks. |

## Compliance / verification

- **Cross-process ordering** (the ROADMAP Phase 2 gate):
  `PYTHONHASHSEED=1 python -m scripts.retrieve_once fixture.txt > a.txt;`
  `PYTHONHASHSEED=2 python -m scripts.retrieve_once fixture.txt > b.txt; diff a.txt b.txt` → exit 0.
- `grep -rn "pinecone" app/ pyproject.toml` returns nothing.
- Every `WorkflowResult` carries a non-empty `retrieved_policy_ids` array; each id resolves to a
  row in `policy_chunk`.
- `recall@5 ≥ 0.90` on the 20 hand-picked golden logs (ROADMAP Phase 2 exit criteria).
- `python scripts/build_index.py --dry-run` prints document count, chunk count, and embedding
  dimension without writing.

## Revisit when

- The corpus exceeds **100k chunks**, or p95 retrieval latency exceeds **150 ms**.
- Retrieval recall@5 falls below 0.90 and prompt/chunking tuning cannot recover it.
- A second consumer outside this system needs the same vector index.
