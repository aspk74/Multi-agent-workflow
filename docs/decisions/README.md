# Architecture Decision Records

One file per decision. An ADR is written when a choice is **expensive to reverse** or when a
future reader would otherwise ask *"why on earth is it like this?"*

Never edit an accepted ADR's Decision section to reflect a change of mind. Write a new ADR and
mark the old one `Superseded by ADR-XXXX`. The value of this directory is the record of what was
believed and when — rewriting history destroys it.

## Index

| ADR | Title | Status | Resolves ROADMAP §6 | Binds phase |
|---|---|---|---|---|
| [0001](0001-llm-provider.md) | LLM provider, model pinning, and the client abstraction | Accepted | Decision 5 | 3 |
| [0002](0002-vector-store.md) | Policy corpus storage and retrieval: Postgres + pgvector | Accepted | Decision 3 | 2 |
| [0003](0003-durable-execution.md) | Durable execution: LangGraph + Postgres checkpointer, deterministic `thread_id` | Accepted | — | 3 |
| [0004](0004-ingestion-topology.md) | Ingestion topology: S3 → SQS → worker → authenticated webhook | Accepted | Decisions 1, 2 | 2, 5 |
| [0005](0005-autonomy-level.md) | Autonomy: the agent proposes; a human disposes | Accepted | Decisions 4, 8 | 3, 5, 6 |
| [0006](0006-idempotency.md) | Two-layer idempotency: HTTP replay cache + unique constraint | Accepted | — | 5 |
| [0007](0007-deployment-target.md) | Deployment: ECS Fargate behind an ALB, RDS Postgres | Accepted | Decision 7 | 5 |
| [0008](0008-api-authentication.md) | Authentication: HMAC-signed ingest, separate operator credentials | Accepted | Decision 8 (partly) | 5 |
| [0009](0009-deterministic-guardrails.md) | Deterministic guardrails: the model can only raise the risk level | Accepted | — | 3 |
| [0010](0010-data-retention.md) | Audit trail, data retention, and what a decision must prove | Accepted | — | 2, 5 |

`0000-adr-template.md` is the template. Copy it; do not edit it in place.

## ROADMAP §6 open decisions — resolution map

| # | Question | Resolved by | Answer |
|---|---|---|---|
| 1 | Uncommitted parent-directory work — keep or restart? | [0004](0004-ingestion-topology.md) §7 | **Keep.** Commit verbatim to a branch first, refactor second. Do not ship the exception-swallowing S3 write. |
| 2 | S3+SQS or direct webhook? | [0004](0004-ingestion-topology.md) | **Both.** The webhook is the only domain entry point; the SQS worker is a client of it. |
| 3 | Pinecone, pgvector, or local? | [0002](0002-vector-store.md) | **pgvector**, in the same Postgres as the checkpointer. Drop `langchain-pinecone`. |
| 4 | Does the agent act or propose? | [0005](0005-autonomy-level.md) | **Propose.** Human approval for HIGH and CRITICAL, with a five-part numeric promotion gate. |
| 5 | Which model; abstract the provider? | [0001](0001-llm-provider.md) | **OpenAI**, pinned id, verified at boot, behind `app/llm.py`. No fallback provider. |
| 6 | Where does labeled data come from? | [0005](0005-autonomy-level.md) §Consequences | 150 synthetic + 50 real; the **approval queue is the real-log labeling pipeline**. |
| 7 | Deploy target? | [0007](0007-deployment-target.md) | **ECS Fargate + internal ALB + Multi-AZ RDS.** |
| 8 | Does a reversal API exist? | [0008](0008-api-authentication.md) §Reversal | ⚠️ **UNANSWERED — external fact.** Blocks work-order step 25 and makes [0005](0005-autonomy-level.md)'s promotion gate unreachable if the answer is "no". |

## Still open after Phase 1

These are not ADRs yet because they need information the project does not have.

| Question | Blocked on | Needed by |
|---|---|---|
| Does the procurement system expose an un-pause endpoint? | An answer from the procurement system's owner | Phase 5, step 25 |
| Approval UI: Slack, web, or stay API-only? | Throughput data from the API-only queue | Phase 6 |
| Retention schedule sign-off | Legal/compliance review of [0010](0010-data-retention.md) | Before production data |
| Provider zero-retention terms confirmed in writing | Contract check with OpenAI | Before production data |
| Is an SME available to label 50 real logs? | Staffing | Phase 2 |
