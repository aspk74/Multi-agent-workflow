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
