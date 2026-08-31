# CLAUDE.md — Vendor Risk Matrix Codebase Guide

**Last updated:** 2026-08-31

This document is your reference for navigating and contributing to the Autonomous Vendor Negotiation & Risk Matrix project. It explains the codebase structure, development workflows, architectural patterns, and key conventions for AI assistants.

---

## 1. PROJECT OVERVIEW

### What This System Does

A LangGraph pipeline that reads vendor communication logs, retrieves applicable compliance policies, classifies risk with an LLM, and triggers vendor suspension for `HIGH`/`CRITICAL` verdicts.

**Current state:** Prototype, not production.

- Both policy retrieval and vendor pause operations are mocked
- No authentication layer
- Nothing is persisted to a database
- No tests, CI, or deployment infrastructure
- Runs are not durable (no checkpointer)

See `docs/ROADMAP.md` for the execution plan and `docs/HLD.md` / `docs/LLD.md` for the target architecture.

### Stack

| Component | Technology | Status |
|---|---|---|
| **Orchestration** | LangGraph + LangChain | Working |
| **LLM** | Google Gemini (langchain-google-genai) | Working; mocked policy retrieval |
| **API** | FastAPI + Uvicorn | Working |
| **Vector Store** | None (target: Postgres + pgvector) | Missing |
| **Config** | pydantic-settings | Working (partially dead) |
| **Policy Store** | 6-entry Python dict | Mocked |

---

## 2. FILE STRUCTURE

### Root Level

```
Multi-agent-workflow/
├── CLAUDE.md              # This file
├── README.md              # User-facing overview
├── pyproject.toml         # Dependencies, tooling config (ruff, mypy, pytest)
├── .env.example           # Required environment variables
├── .gitignore             # Excludes .env, __pycache__, venv/
└── docs/                  # Architecture, decisions, threat model
```

### Application Code (`app/`)

| File | Responsibility | Lines |
|---|---|---|
| **config.py** | Environment variables (pydantic-settings), logging setup, singleton settings getter | 93 |
| **models.py** | Pydantic `RiskClassification` schema (structured LLM output) | 56 |
| **state.py** | LangGraph `TypedDict` defining workflow state shared across nodes | ~20 |
| **tools.py** | Two mocked `@tool` functions: `retrieve_policies` and `pause_vendor` | 212 |
| **agents.py** | Three workflow nodes: `researcher_node`, `classifier_node`, `action_node` | 228 |
| **graph.py** | StateGraph construction, conditional routing, eager module-level compilation | ~120 |
| **main.py** | FastAPI server, endpoints, lifespan, error handling | 245 |
| **__init__.py** | (empty) | 0 |

### Documentation (`docs/`)

| Document | Purpose |
|---|---|
| **ROADMAP.md** | Source of truth: current state, 6 execution phases, work order, open decisions |
| **HLD.md** | Target architecture: containers, components, request lifecycles, failure model |
| **LLD.md** | Implementation spec: module contracts, DB schema, API contract, timeout matrix |
| **RISK_TAXONOMY.md** | Labelling rubric with 12 worked examples and tiebreak rules |
| **SLOS.md** | Latency, availability, cost, and quality targets |
| **THREAT_MODEL.md** | STRIDE analysis; §6 lists what is exploitable in the prototype |
| **decisions/** | 10 ADRs (ADR-0000 through ADR-0010) documenting architectural choices |

---

## 3. ARCHITECTURE & DATA FLOW

### The Pipeline (Current Prototype)

```
POST /webhook/vendor-log
        │
        ▼
┌───────────────────┐
│ researcher_node   │  Substring match over 6-entry dict
└────────┬──────────┘  (no embeddings, no vector store)
         │
         ▼
┌───────────────────┐
│ classifier_node   │  LLM call: ChatGoogleGenerativeAI + structured output
└────────┬──────────┘  (only node with a model call)
         │
   ┌─────┴──────┐
   │            │
  HIGH/     LOW/MEDIUM
 CRITICAL       │
   │            ▼
   ▼           END
┌──────────────────┐
│   action_node    │  String formatting (mock vendor pause)
└──────────────────┘
```

### Workflow State (`app/state.py`)

TypedDict carries data between nodes:

```python
class AgentState(TypedDict):
    vendor_id: str
    log_text: str
    retrieved_policies: list[str]
    risk_classification: RiskClassification | None
    action_taken: str
    retrieved_policies_count: int
```

### Node Responsibilities

| Node | Function | Line Range | Model Call? |
|---|---|---|---|
| **researcher_node** | Finds policies matching keywords in vendor log | `agents.py:40-66` | No |
| **classifier_node** | Asks LLM to classify risk and return structured output | `agents.py:95-148` | **Yes** |
| **action_node** | Formats mock vendor pause response | `agents.py:165-228` | No |

**Key detail:** This is **not** a multi-agent system. Two of three nodes have no model call. It's a 3-step pipeline with one branch.

---

## 4. CRITICAL ISSUES & KNOWN DEFECTS

### 🔴 P0: Results Vary Between Restarts

**Location:** `app/tools.py:126`

`retrieve_policies` collects matched topics into a Python `set`, which randomizes iteration order per process. This changes policy order in the prompt between server restarts — **directly contradicting the claim at `app/agents.py:101` that `temperature=0` ensures deterministic classifications.**

**Fix:** Sort topics before building policy list. ~15 minutes.

```python
# Current (broken)
matched_topics = set()
# ... add to matched_topics ...
for topic in matched_topics:  # Order varies per restart

# Fixed
for topic in sorted(matched_topics):  # Deterministic order
```

### 🟠 P1: Mocked Integrations & Dead Code

**Location:** `app/tools.py:88-146`, `app/tools.py:154-212`

Both tools are non-functional stubs:
- **Policy retrieval** (`retrieve_policies`): 6-entry hardcoded dict, substring matching, no vector store
- **Vendor pause** (`pause_vendor`): Returns JSON string with random UUID, makes no HTTP call, suspends nothing

Commented-out TODO blocks at `app/tools.py:107-121` and `app/tools.py:177-191` reference fields/classes that don't exist.

### 🟠 P1: No API Authentication

**Location:** `app/main.py:173-245`

`POST /webhook/vendor-log` is completely unauthenticated. Any public user can POST and suspend a vendor. This is documented as exploitable in `docs/THREAT_MODEL.md §6`.

### 🟡 P2: Missing Observability

**Location:** `app/main.py:194`

`correlation_id` is generated but:
- Never returned to the caller (not in `WorkflowResult`, not in response headers)
- Never persisted (no logging to structured store)
- Not tied to LLM usage for cost tracking

### 🟡 P2: No Input Validation on `log_text`

**Location:** `app/main.py:132-138`

Has `min_length=10` but no `max_length`. A 2 MB log bypasses rate limits and hits the model unfiltered.

### 🟡 P2: LLM Client Rebuilt Per Request

**Location:** `app/agents.py:115`

`ChatGoogleGenerativeAI` is constructed inside `classifier_node` on every request instead of once at startup. This:
- Adds latency (API key validation, client initialization)
- Prevents centralized timeout/retry/fallback configuration
- Wastes resources on connection setup

Should move to `lifespan` (see `app/main.py:44-54`).

### 🟡 P2: No Timeout / Retry / Fallback

**Location:** `app/agents.py:115-119`

LLM client has only `model`, `google_api_key`, `temperature`. A Gemini API blip = HTTP 500.

---

## 5. DEVELOPMENT SETUP

### Prerequisites

- Python ≥ 3.11
- Google AI Studio API key: https://aistudio.google.com/apikey

### Installation

```bash
# Clone the repo
cd /home/user/Multi-agent-workflow

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies (including dev tools)
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env and set GEMINI_API_KEY
```

### Environment Variables

| Variable | Purpose | Required | Default |
|---|---|---|---|
| **GEMINI_API_KEY** | Google Gemini API key | Yes | — |
| **GEMINI_MODEL_NAME** | Model ID to use | No | `gemini-3.5-flash` |
| **LOG_LEVEL** | Python logging level | No | `INFO` |
| **APP_HOST** | Uvicorn bind address | No | `0.0.0.0` (dead code) |
| **APP_PORT** | Uvicorn bind port | No | `8000` (dead code) |

**Dead fields:** `APP_HOST`, `APP_PORT`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME` are never read by any code.

---

## 6. RUNNING THE APPLICATION

### Start the Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- **`--reload`** enables hot reload during development
- Server runs on `http://localhost:8000`
- OpenAPI docs at `http://localhost:8000/docs`

### Test the Endpoints

#### Health Check

```bash
curl http://localhost:8000/health
```

Returns `200` if the process is alive (does **not** validate API key or connectivity).

#### Classify a Vendor Log

```bash
curl -X POST http://localhost:8000/webhook/vendor-log \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_id": "V-1234",
    "log_text": "Vendor missed delivery deadline by 15 days and unilaterally increased contract prices by 40% without notice. Multiple quality non-conformances detected in last shipment."
  }'
```

### Example Response

```json
{
  "vendor_id": "V-1234",
  "risk_classification": {
    "risk_level": "CRITICAL",
    "confidence_score": 0.94,
    "risk_factors": ["15-day delivery delay..."],
    "recommended_action": "Suspend purchasing authority...",
    "reasoning": "Breaches POLICY-001..."
  },
  "action_taken": "{\"status\": \"VENDOR_PAUSED\", \"vendor_id\": \"V-1234\", \"transaction_id\": \"TXN-4F2A9C1E7B03\", ...}",
  "retrieved_policies_count": 5
}
```

**Note:** `risk_classification` varies per request due to model sampling. Everything else is deterministic (except policy order, which varies between server restarts due to the `set` bug).

---

## 7. KEY ARCHITECTURAL PATTERNS

### Configuration Management

**File:** `app/config.py`

- Uses `pydantic-settings` to load environment variables with validation
- `SecretStr` for sensitive fields (API keys)
- Singleton pattern with `@lru_cache` decorator for `get_settings()`
- Logging configured from `LOG_LEVEL` env var; noisy dependencies muted

**Pattern for AI assistants:**
```python
from app.config import get_settings

settings = get_settings()
api_key = settings.gemini_api_key.get_secret_value()
```

### State Management

**File:** `app/state.py`

LangGraph uses a `TypedDict` for state. All nodes read/write to it:

```python
class AgentState(TypedDict):
    vendor_id: str
    log_text: str
    retrieved_policies: list[str]
    risk_classification: RiskClassification | None
    action_taken: str
    retrieved_policies_count: int
```

**Pattern for AI assistants:**
When adding fields:
1. Update `AgentState` in `state.py`
2. Update all nodes that use it
3. Update `WorkflowResult` in `main.py` if the field should be returned to the caller

### Conditional Routing

**File:** `app/graph.py:45-72`

```python
def route_by_risk(state: AgentState) -> str:
    """Route to action_node if risk is HIGH/CRITICAL, else END."""
    if state.risk_classification is None:
        return "end"
    if state.risk_classification.risk_level in ("HIGH", "CRITICAL"):
        return "action"
    return "end"
```

Routes are defined as strings and matched in graph construction. Changing route logic requires updating `graph.py`.

### Structured Output

**File:** `app/models.py`

Uses Pydantic with `langchain`'s `with_structured_output()`:

```python
llm = ChatGoogleGenerativeAI(...).with_structured_output(RiskClassification)
result = llm.invoke(prompt)  # Returns RiskClassification, never a string
```

**To modify the schema:** Edit `RiskClassification` in `models.py`; the LLM call in `agents.py` will automatically enforce it.

### Error Handling

**File:** `app/main.py:97-116`, `app/main.py:214-225`

- Global `HTTPException` handler for 400/404/422
- Try/except around graph execution catches and logs unexpected errors
- No partial results; everything degrades to 500

**Pattern for AI assistants:**
If a node raises, the entire request fails. Consider adding a try/except inside the node if you want partial recovery.

---

## 8. TESTING & QUALITY ASSURANCE

### Current State

- **Tests:** None. `pytest` is configured (`pyproject.toml:62-64`) but collects zero tests (no `tests/` directory).
- **Type checking:** `mypy --strict` is configured (`pyproject.toml:57-60`) but has never been run. All three nodes annotate bare `-> dict`, which strict mypy would flag.
- **Linting:** `ruff` is configured (`pyproject.toml:49-55`) but has never been run in CI.
- **CI:** No `.github/` directory. No CI pipeline.

### Running Local Checks

```bash
# Format & lint with ruff
ruff check --fix app/

# Type check
mypy app/

# Run tests (will find zero tests until you add some)
pytest
```

### Where to Add Tests

Create `tests/` directory:

```
tests/
├── __init__.py
├── test_agents.py         # Test node functions
├── test_graph.py          # Test graph routing & integration
├── test_tools.py          # Test tool functions
├── test_main.py           # Test API endpoints (use TestClient)
├── test_models.py         # Test Pydantic schemas
└── conftest.py            # Shared fixtures
```

**Example test:**

```python
# tests/test_main.py
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200

def test_webhook_vendor_log_high_risk():
    response = client.post(
        "/webhook/vendor-log",
        json={
            "vendor_id": "V-1234",
            "log_text": "Vendor missed delivery deadline by 15 days..."
        }
    )
    assert response.status_code == 200
    assert response.json()["risk_classification"]["risk_level"] in ("HIGH", "CRITICAL")
```

---

## 9. TOOLING & DEPENDENCIES

### Key Dependencies

| Package | Version | Purpose |
|---|---|---|
| **langgraph** | ≥0.2.0 | Orchestration engine |
| **langchain** | ≥0.3.0 | LLM framework (utilities, base classes) |
| **langchain-core** | ≥0.3.0 | LLM abstractions |
| **langchain-google-genai** | ≥2.0.0 | Google Gemini integration |
| **fastapi** | ≥0.115.0 | Web framework |
| **uvicorn** | ≥0.34.0 | ASGI server |
| **pydantic** | ≥2.0.0 | Data validation |
| **pydantic-settings** | ≥2.0.0 | Environment config |
| **python-dotenv** | ≥1.0.0 | .env file loading |
| **httpx** | ≥0.27.0 | HTTP client (mock procurement API) |

### Dead Dependencies

- **langchain-pinecone** ≥0.2.0 — Listed but never imported

### Dev Dependencies

- **pytest** — Framework (no tests exist)
- **pytest-asyncio** — Async test support
- **ruff** — Linter
- **mypy** — Type checker

---

## 10. DEPLOYMENT & DEVOPS

### Current State

- **No container:** No `Dockerfile`, no `.dockerignore`
- **No lock file:** Dependencies are open ranges (`>=`), not pinned
- **No IaC:** No Terraform, no Kubernetes manifests
- **No CI:** No GitHub Actions, no deployment pipeline
- **Production run:** Doesn't exist. Only development server (`uvicorn --reload`)

### Target Deployment (from ADR-0007)

See `docs/decisions/0007-deployment-target.md` for the deployment strategy. Phase 2+ work.

---

## 11. CONVENTIONS FOR AI ASSISTANTS

### Code Style

- **Line length:** 100 characters (enforced by ruff)
- **Import style:** isort (automatic via ruff)
- **Type hints:** Required. Use `from __future__ import annotations` for forward references.
- **Docstrings:** One-line docstrings for functions. Multi-line only if behavior is non-obvious.
- **Variable names:** `snake_case`. Avoid single-letter vars except in comprehensions.

### When Adding Features

1. **Update state first:** If the feature requires new data, add fields to `AgentState` in `state.py`.
2. **Update the graph:** Add nodes or edge cases in `graph.py`.
3. **Update the API:** Add fields to `WorkflowResult` in `main.py` if callers need them.
4. **Add tests:** Create test cases in `tests/`.
5. **Update docs:** If the feature changes behavior, update the relevant doc in `docs/`.

### When Fixing Bugs

1. **Reproduce:** Write a test that fails with the bug.
2. **Fix:** Make the minimal change to pass the test.
3. **Verify:** Run the full test suite locally.
4. **Commit:** Reference the issue number if applicable.

### When Refactoring

- Don't introduce abstractions ahead of time. Three similar things → extract; two things → leave alone.
- Keep each PR focused on one concern.
- Run `ruff check --fix` and `mypy` locally before pushing.

### Logging

- Use `logger = logging.getLogger(__name__)` in each module.
- Log at appropriate levels: `DEBUG` for detailed traces, `INFO` for state changes, `WARNING` for recoverable errors, `ERROR` for failures.
- Avoid logging secrets (API keys, vendor IDs in contexts where they shouldn't be).

---

## 12. COMMON TASKS

### Running the Full Workflow End-to-End

```bash
# Terminal 1: Start the server
uvicorn app.main:app --reload

# Terminal 2: Send a vendor log
curl -X POST http://localhost:8000/webhook/vendor-log \
  -H "Content-Type: application/json" \
  -d '{"vendor_id": "V-1234", "log_text": "Your test log here"}'
```

### Viewing the Graph Structure

```python
# In Python REPL
from app.graph import compiled_graph

# Print ASCII representation
print(compiled_graph.get_graph().draw_ascii())
```

### Checking Which Policies Match

```bash
# Hard-coded policies are in app/tools.py:34-69
# Keywords are in app/tools.py:71-85
# Matcher is in app/tools.py:88-146

# To test matching locally:
from app.tools import retrieve_policies

log_text = "Vendor missed delivery deadline..."
policies = retrieve_policies(log_text)
for policy in policies:
    print(policy)
```

### Disabling Structured Output (for debugging)

If the LLM's structured output parsing breaks:

```python
# In agents.py, replace:
llm = ChatGoogleGenerativeAI(...).with_structured_output(RiskClassification)

# With:
llm = ChatGoogleGenerativeAI(...)
# Then manually parse response.content as JSON
```

---

## 13. DECISION LOG & RATIONALE

Key architectural decisions are documented in `docs/decisions/`:

| ADR | Title | Key Decision |
|---|---|---|
| **0001** | LLM Provider | OpenAI (not yet implemented; currently Gemini) |
| **0002** | Vector Store | Postgres + pgvector (not yet implemented) |
| **0003** | Durable Execution | LangGraph with Postgres checkpointer (not yet implemented) |
| **0004** | Ingestion Topology | S3 → SQS → consumer (partially untracked) |
| **0005** | Autonomy Level | Full autonomy on HIGH/CRITICAL, human approval for others (not yet implemented) |
| **0006** | Idempotency | Idempotency key + dedupe database (not yet implemented) |
| **0007** | Deployment | Containerized, scaled, observability-first (not yet implemented) |
| **0008** | API Auth | HMAC signature or API key (not yet implemented) |
| **0009** | Guardrails | Deterministic risk taxonomy, no model drift (partially implemented) |
| **0010** | Data Retention | All data persisted; policies returned with results (not yet implemented) |

**Before making architectural changes**, check the corresponding ADR.

---

## 14. ROADMAP & NEXT STEPS

The project is in Phase 1 (architecture & decisions). Upcoming phases:

- **Phase 2:** Ingestion pipeline (S3 → SQS → consumer)
- **Phase 3:** Real policy retrieval (Postgres + pgvector)
- **Phase 4:** Durable execution (Postgres checkpointer)
- **Phase 5:** Authentication & idempotency
- **Phase 6:** Containerization & deployment

See `docs/ROADMAP.md` for the full work order.

---

## 15. TROUBLESHOOTING

### "ModuleNotFoundError: No module named 'app'"

**Solution:** Ensure you're in the repo root and have run `pip install -e ".[dev]"`.

### "GEMINI_API_KEY is missing"

**Solution:** Create a `.env` file from `.env.example` and set `GEMINI_API_KEY`.

### "The model `gemini-3.5-flash` does not exist"

**Solution:** Set `GEMINI_MODEL_NAME` to a valid model ID, or remove it from `.env` to use the default.

### "Results differ between server restarts"

**This is a known bug.** See §4 (P0: Results Vary Between Restarts). The fix is to sort topics before building the policy list in `app/tools.py:135`.

### Mypy reports "error: Incompatible return type" on a node

**Cause:** Nodes return bare `dict` instead of `AgentState`.

**Solution:** Add type hint `-> AgentState` to the node function.

---

## 16. USEFUL LINKS

- **API Docs:** http://localhost:8000/docs (when server is running)
- **Google AI Studio:** https://aistudio.google.com/
- **LangGraph Docs:** https://langchain-ai.github.io/langgraph/
- **LangChain Docs:** https://python.langchain.com/
- **Pydantic Docs:** https://docs.pydantic.dev/

---

## 17. CONTACT & ATTRIBUTION

- **Repository:** https://github.com/aspk74/Multi-agent-workflow
- **License:** MIT

**Last updated:** 2026-08-31 by Claude Code
