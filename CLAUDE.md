# CLAUDE.md — Vendor Risk Matrix Codebase Guide

**Last updated:** 2026-09-13

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
