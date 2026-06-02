"""
app/tools.py
────────────
LangChain `@tool`-decorated functions used by the agent nodes.

IMPORTANT — invocation:
    Because `@tool` wraps functions into `BaseTool` instances, they MUST be
    called via `.invoke({"arg_name": value})`, NOT as plain Python callables.

    ✅  retrieve_policies.invoke({"vendor_id": "V-1234", "log_text": "..."})
    ❌  retrieve_policies("V-1234", "...")  # bypasses LangChain validation

Both tools are mocked for local/demo use. TODO comments mark exactly where
real Pinecone / procurement-API code would be wired in.
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
# In production this is replaced by a Pinecone similarity search.
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

    Simulates a semantic similarity search against a Pinecone vector database
    of supply-chain compliance rules. In production, replace the mock logic
    below with a real Pinecone query.

    Args:
        vendor_id: The unique vendor identifier (used for audit logging).
        log_text: The raw vendor communication text to match policies against.

    Returns:
        A list of relevant policy strings to be used as context by the
        classifier node.
    """
    logger.info("retrieve_policies | vendor_id=%s | querying policy store", vendor_id)

    # ── TODO: Replace mock with real Pinecone retrieval ─────────────────────
    # from langchain_pinecone import PineconeVectorStore
    # from langchain_openai import OpenAIEmbeddings
    # from app.config import get_settings
    #
    # settings = get_settings()
    # embeddings = OpenAIEmbeddings(
    #     api_key=settings.openai_api_key.get_secret_value()
    # )
    # vectorstore = PineconeVectorStore(
    #     index_name=settings.pinecone_index_name,
    #     embedding=embeddings,
    # )
    # results = vectorstore.similarity_search(log_text, k=5)
    # policies = [doc.page_content for doc in results]
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

    # ── TODO: Replace mock with real procurement API call ───────────────────
    # import httpx
    # from app.config import get_settings
    #
    # settings = get_settings()
    # async with httpx.AsyncClient() as client:
    #     response = await client.post(
    #         f"{settings.procurement_api_base_url}/vendors/{vendor_id}/pause",
    #         json={"reason": reason, "initiated_by": "risk-matrix-agent"},
    #         headers={"Authorization": f"Bearer {settings.procurement_api_key.get_secret_value()}"},
    #         timeout=10.0,
    #     )
    #     response.raise_for_status()
    #     return response.text
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
