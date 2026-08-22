"""
app/tools.py
────────────
LangChain `@tool`-decorated functions used by the agent nodes.

IMPORTANT — invocation:
    Because `@tool` wraps functions into `BaseTool` instances, they MUST be
    called via `.invoke({"arg_name": value})`, NOT as plain Python callables.

    ✅  retrieve_policies.invoke({"vendor_id": "V-1234", "log_text": "..."})
    ❌  retrieve_policies("V-1234", "...")  # bypasses LangChain validation

Both tools are MOCKED. Neither makes a network call:
  - retrieve_policies   keyword-matches a 6-entry dict (no embeddings, no vector store)
  - execute_vendor_pause formats a string with a random UUID (nothing is suspended)

TODO comments below mark where the real implementations go. Target design:
docs/LLD.md §6.4 (pgvector retrieval) and §6.6 (procurement client).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Tool 1: Compliance Policy Retrieval
# ─────────────────────────────────────────────────────────────────────────────

# Mock policy database keyed by topic keywords.
# Target: a real corpus in data/policies/, chunked and embedded into Postgres
# pgvector (docs/decisions/0002-vector-store.md). Six paragraphs is not a
# compliance corpus.
_MOCK_POLICY_DB: dict[str, str] = {
    "delivery": (
        "POLICY-001 (On-Time Delivery): Vendors must maintain ≥ 95% on-time delivery "
        "rate measured monthly. A delay exceeding 7 calendar days on any critical-path "
        "shipment constitutes a Major Non-Conformance (MNC) and requires a corrective "
        "action plan within 5 business days."
    ),
    "price": (
        "POLICY-002 (Price Stability): Vendors may not unilaterally increase contracted "
        "prices by more than 5% within any 12-month period without 60-days written "
        "notice and written approval from the Chief Procurement Officer. Unauthorised "
        "price increases above 10% constitute grounds for contract termination."
    ),
    "quality": (
        "POLICY-003 (Quality Non-Conformance): Any shipment with a defect rate exceeding "
        "0.5% triggers a mandatory Quality Stop and root-cause analysis. Two or more "
        "MNCs within 90 days automatically escalates the vendor to CRITICAL risk tier "
        "and initiates the Vendor Improvement Programme (VIP)."
    ),
    "communication": (
        "POLICY-004 (Communication & Transparency): Vendors are required to proactively "
        "notify the procurement team of any supply disruption, capacity constraint, or "
        "pricing change within 48 hours of becoming aware. Failure to notify is itself "
        "a compliance breach."
    ),
    "contract": (
        "POLICY-005 (Contract Compliance): Vendors must comply with all terms set out in "
        "the Master Supply Agreement (MSA). Repeated or wilful violations of the MSA may "
        "result in suspension of purchasing authority pending a formal Vendor Review Board."
    ),
    "default": (
        "POLICY-000 (General Vendor Code of Conduct): All vendors must adhere to ethical "
        "sourcing standards, maintain accurate record-keeping, and cooperate fully with "
        "any audit requested by the procurement or compliance team."
    ),
}

_POLICY_KEYWORDS: list[tuple[str, str]] = [
    ("delay", "delivery"),
    ("deadline", "delivery"),
    ("shipment", "delivery"),
    ("price", "price"),
    ("cost", "price"),
    ("increase", "price"),
    ("quality", "quality"),
    ("defect", "quality"),
    ("non-conformance", "quality"),
    ("communication", "communication"),
    ("notify", "communication"),
    ("contract", "contract"),
    ("agreement", "contract"),
]


@tool
def retrieve_policies(vendor_id: str, log_text: str) -> list[str]:
    """
    Retrieve relevant compliance policy documents for a given vendor log.

    MOCK. Substring-matches 13 hardcoded keywords against the log text and
    returns the matching entries from a 6-entry dict. This is not a similarity
    search and there is no vector store.

    KNOWN DEFECT: `matched_topics` is a set (below), so the ORDER of the returned
    policies is not stable across processes — Python randomises string hashing
    per process. Since this list is rendered into the classifier prompt
    (app/agents.py:123-125), identical input can yield different verdicts after a
    server restart. Fix: sort before building the list. ROADMAP work-order step 4.

    Args:
        vendor_id: The unique vendor identifier (used for audit logging).
        log_text: The raw vendor communication text to match policies against.

    Returns:
        A list of relevant policy strings to be used as context by the
        classifier node.
    """
    logger.info("retrieve_policies | vendor_id=%s | querying policy store", vendor_id)

    # ── TODO(Phase 2): replace this mock with pgvector retrieval ────────────
    # Do NOT use Pinecone. That decision was reversed — see
    # docs/decisions/0002-vector-store.md. The replacement lives in
    # app/retrieval.py and is specified in docs/LLD.md §6.4:
    #   - policy chunks + embeddings in Postgres (pgvector, 1536 dims)
    #   - hybrid query: top-8 by cosine, unioned with literal POLICY-NNN mentions
    #   - ORDER BY embedding <=> $1 ASC, policy_id ASC, chunk_index ASC
    #     (the trailing tiebreakers are what make ordering stable across processes)
    #   - returns PolicyChunk objects, so the IDs used can be persisted and shown
    #     to a vendor who disputes the decision
    # An earlier version of this comment referenced `settings.openai_api_key`,
    # which has never existed in Settings (app/config.py:26-64).
    # ────────────────────────────────────────────────────────────────────────

    # Mock: keyword-based policy matching
    log_lower = log_text.lower()
    matched_topics: set[str] = set()

    for keyword, topic in _POLICY_KEYWORDS:
        if keyword in log_lower:
            matched_topics.add(topic)

    if not matched_topics:
        matched_topics.add("default")

    policies = [_MOCK_POLICY_DB[topic] for topic in matched_topics]
    # Always include the general code of conduct
    if "default" not in matched_topics:
        policies.append(_MOCK_POLICY_DB["default"])

    logger.info(
        "retrieve_policies | vendor_id=%s | matched %d policies: %s",
        vendor_id,
        len(policies),
        list(matched_topics),
    )
    return policies


# ─────────────────────────────────────────────────────────────────────────────
# Tool 2: Vendor Pause Execution
# ─────────────────────────────────────────────────────────────────────────────


@tool
def execute_vendor_pause(vendor_id: str, reason: str) -> str:
    """
    Suspend a vendor's purchasing authority in the procurement system.

    Simulates an HTTP POST to an internal procurement API. In production,
    replace the mock below with a real authenticated API call (e.g., via httpx).

    Args:
        vendor_id: The unique vendor identifier to pause.
        reason: A human-readable explanation of why the vendor is being paused,
                derived from the risk classification.

    Returns:
        A JSON-formatted confirmation string containing the transaction ID,
        timestamp, and paused vendor details.
    """
    logger.warning(
        "execute_vendor_pause | vendor_id=%s | INITIATING VENDOR PAUSE | reason=%s",
        vendor_id,
        reason,
    )

    # ── TODO(Phase 5): replace this mock with the real procurement client ───
    # Specified in docs/LLD.md §6.6. The snippet that used to live here does not
    # work: `settings.procurement_api_base_url` and `settings.procurement_api_key`
    # do not exist in Settings (app/config.py:26-64) and must be added first.
    #
    # Three requirements that the old snippet omitted, all load-bearing:
    #   1. Exactly-once. INSERT action_execution(status='pending') with
    #      UNIQUE(action_key) BEFORE the HTTP call; IntegrityError means a pause
    #      already exists — return its txn id and make no call at all.
    #      See docs/decisions/0006-idempotency.md.
    #   2. Pass action_key as the downstream Idempotency-Key so the procurement
    #      system can dedupe independently of us.
    #   3. This call must not be reachable without a human approval while
    #      AUTONOMY_MODE=approve_required. See docs/decisions/0005-autonomy-level.md.
    #
    # OPEN QUESTION blocking this work: does the procurement system expose an
    # un-pause endpoint? If not, autonomy is permanently off the table.
    # ROADMAP Open Decision 8.
    # ────────────────────────────────────────────────────────────────────────

    # Mock: generate a realistic-looking procurement system response
    transaction_id = f"TXN-{uuid.uuid4().hex[:12].upper()}"
    timestamp = datetime.now(tz=timezone.utc).isoformat()

    confirmation = (
        f'{{"status": "VENDOR_PAUSED", '
        f'"vendor_id": "{vendor_id}", '
        f'"transaction_id": "{transaction_id}", '
        f'"paused_at": "{timestamp}", '
        f'"reason": "{reason[:200]}", '
        f'"initiated_by": "risk-matrix-agent", '
        f'"next_review_date": "72h"}}'
    )

    logger.warning(
        "execute_vendor_pause | vendor_id=%s | PAUSED SUCCESSFULLY | txn_id=%s",
        vendor_id,
        transaction_id,
    )
    return confirmation
