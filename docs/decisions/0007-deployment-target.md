# ADR-0007 — Deployment: ECS Fargate behind an ALB, RDS Postgres, lockfile-pinned image

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 5 (implementation)
- **Relates to:** [ADR-0002](0002-vector-store.md), [ADR-0003](0003-durable-execution.md), [ADR-0004](0004-ingestion-topology.md)

## Context

The only documented way to run this system is `uvicorn app.main:app --reload` (`README.md:78`) —
a development server with a file watcher, single-process, no supervision. There is no Dockerfile,
no `.dockerignore`, no compose file, no CI, and no infrastructure definition anywhere in the repo.

Every dependency is an **open `>=` range** (`pyproject.toml:13-34`) with **no lockfile**. Two
`pip install` runs a month apart produce different code. For a system whose output quality depends
on library behaviour — LangGraph, LangChain, and the OpenAI SDK all move fast — that makes an eval
score unattributable to a commit.

Workload shape: LLM calls of 2–30 seconds, low-to-moderate concurrency, a long-lived Postgres
connection pool, and a durable checkpoint that must survive shutdown. The ingest side is already
AWS-native (S3 + SQS, ROADMAP §2.3).

## Decision

**Deploy two Fargate services in one ECS cluster, behind an internal ALB, with RDS Postgres
(Multi-AZ) as the single data tier. Build the image from a lockfile.**

```
                     ┌──────────── VPC ────────────┐
  source systems ──► S3 ──► SQS ──► [ingest-worker] │   Fargate service, 1–4 tasks, no inbound
                                          │         │
                                          ▼         │
                                    internal ALB    │
                                          │         │
                                    [vrm-api]       │   Fargate service, 2–10 tasks
                                          │         │
                          ┌───────────────┼─────────┴──────────┐
                          ▼               ▼                    ▼
                    RDS Postgres     S3 (archive)        OpenAI API
                    (Multi-AZ,                          via NAT gateway
                     pgvector)
```

### Binding rules

1. **Two services, one image.** `vrm-api` and `ingest-worker` ship the same container with
   different entrypoints. One build, one scan, one version — the worker can never run against a
   different code version than the API it calls.
2. **Reproducible builds.** `uv.lock` is committed and CI fails if it is stale. The Dockerfile is
   multi-stage: a builder resolving **from the lockfile only** (`--frozen`), and a slim runtime
   stage running as a **non-root** user with no build toolchain.
3. **Internal ALB, no public ingress.** Nothing about this system should be reachable from the
   internet. Combined with [ADR-0008](0008-api-authentication.md), that is defence in depth, not
   an excuse to skip auth.
4. **Timeout ladder, outermost to innermost** — each layer must be strictly longer than the one
   inside it, or a timeout fires at the wrong altitude and the inner work is orphaned:

   | Layer | Value |
   |---|---|
   | SQS visibility timeout | 360 s |
   | ALB idle timeout | 120 s |
   | Uvicorn / app request budget | 60 s |
   | LLM call, per attempt | 35 s |
   | Postgres `statement_timeout` | 5 s |

5. **Graceful shutdown.** ECS `stopTimeout: 60`, SIGTERM → stop accepting, drain in-flight runs to
   their next checkpoint, close the pool, exit. ALB deregistration delay 30 s.
6. **Health endpoints are distinct.** ALB targets `GET /health` (liveness — process is up).
   `GET /ready` additionally checks Postgres, pgvector, and LLM reachability and returns **503**
   when a dependency is down, so a broken task is removed from rotation. Today's `/health`
   (`app/main.py:161-170`) returns 200 with an invalid API key and an unreachable database; it
   proves only that Python is running.
7. **Secrets** come from AWS Secrets Manager, injected as ECS task-definition secrets. No secret
   is ever in an image layer, an env file in the repo, or a task-definition plaintext env var.
8. **Two environments**, `staging` and `prod`, identical Terraform with different `.tfvars`.
   Staging runs the nightly eval ([ROADMAP Phase 6](../ROADMAP.md)) against a stubbed procurement
   API so drift detection never touches a real vendor.
9. **Autoscaling** on SQS queue depth for the worker (a queue-length target tracks the real work
   backlog; CPU does not, because the worker is I/O-bound) and on request count per target for the
   API. API minimum is **2** tasks — one task is not a deployment, it is an outage waiting for a
   deploy.
10. **Rollback is a task-definition revision revert**, tested and documented in `docs/DEPLOY.md`.
    Image tags are immutable (`git sha`), never `latest`.

## Consequences

### Positive
- No servers to patch; the AWS-native ingest path stays in one account and one VPC.
- Long LLM calls fit the model naturally — no cold-start or execution-limit gymnastics.
- Multi-AZ RDS gives the checkpointer and vector index real durability with no extra components.
- Reproducible images mean an eval score is attributable to a commit.

### Negative
- **Cost floor at zero traffic:** two API tasks + one worker + Multi-AZ RDS + NAT gateway ≈ a
  nontrivial monthly minimum regardless of volume. Accepted as the price of a real availability
  story; the NAT gateway is often the largest single line and can be traded for a VPC endpoint
  later.
- Fargate cold start on scale-out is 30–60 s. Handled by keeping minimum capacity at 2 and scaling
  on queue depth (which leads demand) rather than on CPU (which lags it).
- Terraform is now a skill the project requires.

### Neutral
- ECS ties the project to AWS. Given S3 + SQS + RDS are already assumed, that ship has sailed;
  the portable part is the container, which runs anywhere.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Kubernetes (EKS)** | Better long-run portability and a richer ops ecosystem, but a control-plane bill plus a cluster to operate for two services. Revisit past ~6 services or when a platform team exists. |
| **Lambda** | Cheaper at low volume, but 15-minute ceilings sit awkwardly with LLM latency, cold starts hurt p99, and per-invocation retry semantics fight the durable-checkpoint model in [ADR-0003](0003-durable-execution.md). |
| **Docker Compose on one EC2 host** | Perfectly adequate for the mock-data phase, and genuinely tempting. Rejected because it has no HA and no rolling deploys, so it must be migrated before production data lands — and migrations under deadline are how outages happen. `compose.yaml` is still shipped, for **local development only**. |
| **App Runner** | Simplest managed option, but weak VPC/SQS integration and little control over shutdown behaviour, which the checkpointer depends on. |

## Compliance / verification

- `docker build -t vrm .` succeeds from a clean clone; `docker run --rm -p 8000:8000 --env-file .env vrm`
  then `curl -fsS localhost:8000/health` returns HTTP 200.
- Two builds of the same commit yield identical dependency sets:
  `docker run --rm vrm uv pip freeze | sha256sum` matches.
- `docker run --rm --entrypoint whoami vrm` prints a non-root user.
- `GET /ready` returns 503 against a blackholed LLM endpoint while `GET /health` still returns 200.
- **End-to-end kill test:** with the worker running, `docker kill` the API mid-run → the SQS
  message returns after its visibility timeout, the run resumes from its checkpoint on restart,
  and exactly one pause is recorded. No manual repair.
- `.github/workflows/ci.yml` runs `ruff check .`, `mypy app`, `pytest`, and `docker build`, all
  green on `main`.

## Revisit when

- Monthly infrastructure cost exceeds the value of the automation at current volume.
- A third or fourth deployable service appears, tipping the balance toward Kubernetes.
- Traffic becomes spiky enough that per-request billing beats reserved capacity.
