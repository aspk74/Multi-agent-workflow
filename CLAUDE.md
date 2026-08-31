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
