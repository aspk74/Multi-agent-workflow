# Autonomous Vendor Negotiation & Risk Matrix

A LangGraph pipeline that reads a vendor communication log, retrieves the compliance policies it
touches, classifies the risk with an LLM, and — for `HIGH`/`CRITICAL` verdicts — triggers a vendor
pause.

> ### ⚠️ Current state: prototype, not production
>
> This repository is an **early prototype**. Read this before drawing any conclusion from it.
>
> - **Both tools are mocked.** Policy retrieval is a 6-entry Python dict matched by substring
>   ([app/tools.py:34-146](app/tools.py)); the vendor pause is a locally-formatted string with a
>   random UUID and **makes no network call** ([app/tools.py:154-212](app/tools.py)).
> - **There is no authentication.** `POST /webhook/vendor-log` is an unauthenticated endpoint whose
>   side effect is suspending a vendor. See [docs/THREAT_MODEL.md §6](docs/THREAT_MODEL.md).
> - **Nothing is persisted.** The HTTP response is the only record that a decision was made.
> - **There are no tests, no CI, no container, and no evaluation.** Quality is currently unmeasured.
> - **Runs are not durable.** The graph compiles without a checkpointer
>   ([app/graph.py:119](app/graph.py)), so a crash mid-run loses the run.
>
> Where the system is going, and how it gets there, is in **[docs/ROADMAP.md](docs/ROADMAP.md)**.
> The target architecture is in **[docs/HLD.md](docs/HLD.md)** and **[docs/LLD.md](docs/LLD.md)**;
> the decisions behind it are in **[docs/decisions/](docs/decisions/README.md)**.

---

## Documentation

| Document | What it covers |
|---|---|
| [docs/ROADMAP.md](docs/ROADMAP.md) | Source of truth. Current state, six phases, work order, open decisions. |
| [docs/HLD.md](docs/HLD.md) | Target architecture: context, containers, components, request lifecycles, failure model, capacity. |
| [docs/LLD.md](docs/LLD.md) | Implementation spec: module contracts, DB schema, API contract, timeout matrix, tests. |
| [docs/decisions/](docs/decisions/README.md) | 10 ADRs — provider, vector store, durability, ingest, autonomy, idempotency, deploy, auth, guardrails, retention. |
| [docs/RISK_TAXONOMY.md](docs/RISK_TAXONOMY.md) | The labelling rubric. 12 worked examples, tiebreak rules, inter-rater protocol. |
| [docs/SLOS.md](docs/SLOS.md) | Latency, availability, cost, and quality targets — and which are still guesses. |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | STRIDE analysis plus prompt-injection defences. §6 lists what is exploitable today. |

---

## What runs today

```
POST /webhook/vendor-log          (unauthenticated)
        │
        ▼
┌───────────────────┐
│  researcher_node  │  substring match over a 6-entry dict  ── app/tools.py:88-146
└────────┬──────────┘  (no embeddings, no vector store)
         │
         ▼
┌───────────────────┐
│  classifier_node  │  ChatGoogleGenerativeAI + structured output ── app/agents.py:115-148
└────────┬──────────┘  (client rebuilt per request; no timeout, no retry)
         │
   ┌─────┴──────┐
   │            │
  HIGH/     LOW/MEDIUM
 CRITICAL       │
   │            ▼
   ▼           END
┌──────────────────┐
│   action_node    │  formats a string with a random UUID ── app/tools.py:154-212
└──────────────────┘  (no HTTP call; nothing is suspended)
```

Three nodes, one branch, **one LLM call**. Two of the three nodes contain no model call at all —
`researcher_node` is a dict lookup and `action_node` is string formatting. Calling this
"multi-agent" overstates it; it is a three-step pipeline with a conditional edge.

### Module breakdown

| File | Responsibility |
|---|---|
| [app/config.py](app/config.py) | Environment variables via `pydantic-settings`. Three fields (`app_host`, `app_port`, `pinecone_api_key`) are never read by any code. |
| [app/models.py](app/models.py) | Pydantic `RiskClassification` — the structured-output schema. |
| [app/state.py](app/state.py) | LangGraph `TypedDict` state shared across nodes. |
| [app/tools.py](app/tools.py) | Two `@tool` functions. **Both mocked.** |
| [app/agents.py](app/agents.py) | `researcher_node`, `classifier_node`, `action_node`. |
| [app/graph.py](app/graph.py) | `StateGraph`, conditional routing, eager module-level compile. |
| [app/main.py](app/main.py) | FastAPI server: `GET /health`, `POST /webhook/vendor-log`. |

### Stack

- **LLM:** Google Gemini via `langchain-google-genai` ([app/agents.py:22](app/agents.py)).
  Model id comes from `GEMINI_MODEL_NAME`, default `gemini-3.5-flash`
  ([app/config.py:44](app/config.py)). **Nothing validates this value** — a wrong id fails at the
  first live classification, not at startup.
- **Orchestration:** LangGraph + LangChain.
- **API:** FastAPI + Uvicorn.
- **Vector store:** none. `langchain-pinecone` is listed as a dependency
  ([pyproject.toml:19](pyproject.toml)) and is **never imported**.

The target stack differs: OpenAI behind a pinned client factory
([ADR-0001](docs/decisions/0001-llm-provider.md)) and Postgres + pgvector
([ADR-0002](docs/decisions/0002-vector-store.md)). Neither is implemented yet.

---

## Setup

Requires Python ≥ 3.11 and a Google AI Studio API key
(https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Set GEMINI_API_KEY. PINECONE_* are unused — the code never reads them.
```

`.[dev]` installs `pytest`, but **there is no `tests/` directory**, so `pytest` collects zero
tests. `ruff` and `mypy` are configured ([pyproject.toml:49-60](pyproject.toml)) and have never
been run in CI.

## Running

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

This is a **development server**. There is no production run path — no Dockerfile, no lockfile,
no deployment definition. See [ADR-0007](docs/decisions/0007-deployment-target.md) for the target.

Interactive docs at `http://localhost:8000/docs`.

## Usage

```bash
curl -X POST http://localhost:8000/webhook/vendor-log \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_id": "V-1234",
    "log_text": "Vendor missed delivery deadline by 15 days and unilaterally increased contract prices by 40% without notice. Multiple quality non-conformances detected in last shipment."
  }'
```

### Example response

`risk_classification` is produced by the model and will vary. Everything else is deterministic
for this input.

```json
{
  "vendor_id": "V-1234",
  "risk_classification": {
    "risk_level": "CRITICAL",
    "confidence_score": 0.94,
    "risk_factors": [
      "15-day delivery delay on a critical-path shipment",
      "40% unauthorised price increase without notice",
      "Multiple quality non-conformances in last shipment"
    ],
    "recommended_action": "Suspend purchasing authority and convene a Vendor Review Board.",
    "reasoning": "Breaches POLICY-001 (>7-day delay = MNC), POLICY-002 (>10% unauthorised increase = grounds for termination), and POLICY-003..."
  },
  "action_taken": "{\"status\": \"VENDOR_PAUSED\", \"vendor_id\": \"V-1234\", \"transaction_id\": \"TXN-4F2A9C1E7B03\", \"paused_at\": \"2026-08-22T09:14:22.104913+00:00\", \"reason\": \"[CRITICAL] Suspend purchasing authority...\", \"initiated_by\": \"risk-matrix-agent\", \"next_review_date\": \"72h\"}",
  "retrieved_policies_count": 5
}
```

Two details this example gets right that the previous README got wrong:

- **`retrieved_policies_count` is 5**, not 3. The keyword matcher
  ([app/tools.py:71-138](app/tools.py)) hits four topics for this text — `delivery`
  (via "deadline", "shipment"), `price` (via "prices", "increased"), `quality` (via "quality",
  "non-conformances"), and `contract` (via "contract") — and the general Code of Conduct is
  always appended.
- **`action_taken` is a JSON-shaped string**, not `VENDOR_PAUSED | txn_id=...`. The transaction id
  is `TXN-` followed by 12 uppercase hex characters ([app/tools.py:194](app/tools.py)).

**`retrieved_policies_count` is a count, not a list.** Which policies drove the decision is not
returned and not stored — so a decision cannot currently be explained to a vendor who disputes it.
See [ADR-0010](docs/decisions/0010-data-retention.md).

### Known defect: results vary between restarts

`retrieve_policies` collects matched topics into a `set` ([app/tools.py:126](app/tools.py)) and
iterates it to build the policy list ([app/tools.py:135](app/tools.py)). Python randomises string
hashing per process, so **the order of policies in the prompt changes between server restarts** —
which can change the verdict for identical input. The docstring at
[app/agents.py:101](app/agents.py) claiming `temperature=0` "ensures deterministic, reproducible
classifications" is wrong on both counts: temperature 0 reduces sampling variance rather than
guaranteeing determinism, and the prompt itself is not stable.

Fix: sort the topics before building the list. ~15 minutes; ROADMAP work-order step 4.

### Health check

```bash
curl http://localhost:8000/health
```

Returns `200` whenever the process is running — **including** when the API key is invalid and the
policy store is unreachable. It proves Python is alive, nothing more. A real readiness probe is
[ADR-0007](docs/decisions/0007-deployment-target.md) §6.

---

## Replacing the mocks

`app/tools.py` carries `TODO` blocks marking where real integrations go. Both are **stale** and
neither compiles as written:

- [app/tools.py:107-121](app/tools.py) references `OpenAIEmbeddings` and
  `settings.openai_api_key` — a field that does not exist in `Settings`
  ([app/config.py:26-64](app/config.py)).
- [app/tools.py:177-191](app/tools.py) references `settings.procurement_api_base_url` and
  `settings.procurement_api_key` — **neither field exists** either.

Do not follow those snippets. The specified replacements are
[LLD §6.4](docs/LLD.md) (retrieval) and [LLD §6.6](docs/LLD.md) (procurement client).

---

## License

MIT
