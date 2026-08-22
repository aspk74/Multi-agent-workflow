# ADR-0008 — Authentication: HMAC-signed ingest, separate operator credentials, scoped roles

- **Status:** Accepted
- **Date:** 2026-08-22
- **Deciders:** anushkasirpurkar
- **Phase:** 1 (decision) → 5 (implementation)
- **Relates to:** [ADR-0005](0005-autonomy-level.md), [ADR-0006](0006-idempotency.md), [THREAT_MODEL](../THREAT_MODEL.md)

## Context

There is **no authentication anywhere** in `app/main.py`. No dependency, no API-key check, no
signature verification. `POST /webhook/vendor-log` (`app/main.py:173-245`) is an unauthenticated
endpoint whose side effect is **suspending a supplier's purchasing authority**
(`app/tools.py:154-212`). Anyone who can reach the port can halt a supply line.

There is also no request-size cap: `log_text` has `min_length=10` and **no maximum**
(`app/main.py:132-138`). A 2 MB body goes straight into an LLM prompt — an unauthenticated,
uncapped path to spending the operator's OpenAI budget.

## Decision

**Two credential classes, different mechanisms, different scopes, enforced by a FastAPI
dependency — never by middleware alone.**

### Ingest credentials — HMAC-SHA256 request signature

`POST /webhook/vendor-log` requires:

```
X-VRM-Key-Id:    <key identifier>
X-VRM-Timestamp: <unix seconds>
X-VRM-Signature: sha256=<hex hmac>
```

signed over `f"{method}\n{path}\n{timestamp}\n{sha256(body)}"`.

Rules:

- Compared with `hmac.compare_digest`. Never `==` — string comparison leaks timing.
- Timestamp skew > **300 s** → `401`. Bounds the replay window; [ADR-0006](0006-idempotency.md)
  handles replays inside it.
- `X-VRM-Key-Id` selects the secret, so **rotation is additive**: publish the new key, migrate
  senders, retire the old one. No flag day.
- Signature covers the **body hash**, so a proxy cannot alter the log text in flight.

HMAC over a bearer key because the sender is a service we control ([ADR-0004](0004-ingestion-topology.md)),
the payload is high-consequence, and signing additionally proves *integrity*, which a bearer token
does not.

### Operator credentials — separate, scoped, never the ingest key

`GET /approvals*` and `POST /approvals/{id}/decide` require an **operator** credential with scope
`approvals:read` / `approvals:write`. The ingest key **cannot** approve anything.

This is the control that makes [ADR-0005](0005-autonomy-level.md) meaningful: if one credential
could both submit a log and approve its consequence, human-in-the-loop would be decorative. The
`decided_by` field on every approval row is the key id, so approvals are attributable.

### Scope table

| Route | Scope | Credential class |
|---|---|---|
| `POST /webhook/vendor-log` | `logs:write` | ingest (HMAC) |
| `GET /approvals`, `GET /approvals/{id}` | `approvals:read` | operator |
| `POST /approvals/{id}/decide` | `approvals:write` | operator |
| `POST /actions/{id}/reverse` | `actions:reverse` | operator (elevated) |
| `GET /health`, `GET /ready` | none | — |
| `GET /metrics` | network-restricted to the scrape SG | — |
| `GET /docs`, `/redoc` | **disabled in production** | — |

### Supporting controls (same phase, same PR)

1. **Body cap 256 KB** at the ASGI layer, before parsing — a `Content-Length` over the cap is
   `413` without reading the body. `log_text` additionally gets `max_length=50_000`.
   Currently unbounded (`app/main.py:132-138`).
2. **Rate limit** per key id: 100 req/min sustained, burst 200 → `429` with `Retry-After`.
3. **Failing closed:** the auth dependency raises before any parsing, any DB write, and any LLM
   call. A missing configured secret is a **boot failure**, not a bypass.
4. **`/docs` and `/redoc` off in production.** Today they are always on (`app/main.py:87-88`) and
   they publish the exact schema of the endpoint that suspends vendors.
5. **Auth failures never echo the input.** A `401` body carries only a correlation id.

### Reversal — ROADMAP Open Decision 8

`POST /actions/{action_execution_id}/reverse` is specified here, but **whether it can be built
depends on a fact nobody has confirmed: does the procurement system expose an un-pause
endpoint?** That answer is a hard prerequisite for ROADMAP work-order step 25, and per
[ADR-0005](0005-autonomy-level.md) §Promotion criterion 4, **a "no" makes autonomy permanently
unreachable.** Confirm it before Phase 5 begins.

## Consequences

### Positive
- The irreversible action is no longer reachable by anyone who can route a packet to the ALB.
- Integrity, not just authentication — a tampered log body fails verification.
- Credential separation makes the approval gate a genuine control rather than theatre.
- Rotation without downtime, by construction.
- The body cap closes an unauthenticated route to unbounded LLM spend.

### Negative
- HMAC is more work for clients than a bearer token: canonical string construction must match
  exactly. Mitigated by shipping a signing helper in `worker/` and documenting the canonical form
  with a worked example in [LLD §5.1](../LLD.md#51-post-webhookvendor-log).
- Clock skew becomes an operational concern. NTP on every sender; skew is a metric.
- Secret storage and rotation are now real procedures ([ADR-0007](0007-deployment-target.md) §7).

### Neutral
- Auth is a FastAPI **dependency**, not middleware, so it composes with per-route scopes and is
  visible in the OpenAPI schema and in tests.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Static bearer API key** | Simpler, but no integrity, no replay bound, and rotation is a flag day. Acceptable for a read-only endpoint; not for one that suspends suppliers. |
| **mTLS** | Strongest option and a reasonable future step, but certificate lifecycle for every sender is heavy for the current sender count (one). |
| **OAuth2 / OIDC via an IdP** | Right answer once humans use a UI. Overkill for machine-to-machine with one client, and adds an IdP to the critical path. Revisit alongside the approval-UI ADR. |
| **Network isolation only** (private ALB, SG rules) | Already doing this ([ADR-0007](0007-deployment-target.md) §3), but a VPC is a perimeter, not an authorization decision. Anything that lands inside the VPC would inherit the power to suspend vendors. |
| **IP allowlist** | Brittle with Fargate's dynamic addressing, and provides no attribution for the `decided_by` audit field. |

## Compliance / verification

- Unauthenticated `POST /webhook/vendor-log` → `401`. (ROADMAP Phase 5 exit criterion.)
- Valid signature, body mutated by one byte → `401`.
- Timestamp 10 minutes old → `401`.
- Ingest credential calling `POST /approvals/{id}/decide` → `403`, not `401` — authenticated but
  unauthorized, and the distinction must be visible in logs.
- `Content-Length: 300000` → `413` with **zero** LLM calls recorded.
- Production `GET /docs` → `404`.
- `tests/integration/test_auth.py` covers every row of the scope table.

## Revisit when

- A human-facing UI is built (→ OIDC, session cookies, CSRF — a new ADR).
- A second, less-trusted ingest client appears (→ per-client scopes and quotas).
- Compliance requires non-repudiation stronger than a shared secret (→ asymmetric signatures).
