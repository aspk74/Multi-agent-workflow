"""
app/agents.py
─────────────
LangGraph node functions for the Vendor Negotiation & Risk Matrix workflow.

Each node:
  1. Accepts the full `VendorNegotiationState` dict.
  2. Performs its specific action (tool call, LLM inference, etc.).
  3. Returns a *partial* state dict containing only the keys it updated.
     LangGraph merges this partial dict back into the shared state.

Node execution order (defined in graph.py):
    researcher_node → classifier_node → (conditional) action_node
"""

from __future__ import annotations

import logging
from textwrap import dedent

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import get_settings
from app.models import RiskClassification
from app.state import VendorNegotiationState
from app.tools import execute_vendor_pause, retrieve_policies

logger = logging.getLogger(__name__)

# High-risk levels that trigger the action node
_HIGH_RISK_LEVELS = frozenset({"HIGH", "CRITICAL"})


# ─────────────────────────────────────────────────────────────────────────────
# Node 1 — Researcher
# ─────────────────────────────────────────────────────────────────────────────


def researcher_node(state: VendorNegotiationState) -> dict:
    """
    Retrieve compliance policies relevant to the vendor log from the policy store.

    Invokes the `retrieve_policies` BaseTool via `.invoke()` (required when
    using the @tool decorator — plain function call bypasses schema validation).

    Updates:
        retrieved_policies: list of policy strings for the classifier to use.
    """
    vendor_id = state["vendor_id"]
    raw_log_text = state["raw_log_text"]

    logger.info("researcher_node | START | vendor_id=%s", vendor_id)

    # Must use .invoke() — @tool wraps the function into a BaseTool instance
    policies: list[str] = retrieve_policies.invoke(
        {"vendor_id": vendor_id, "log_text": raw_log_text}
    )

    logger.info(
        "researcher_node | DONE | vendor_id=%s | retrieved %d policies",
        vendor_id,
        len(policies),
    )

    return {"retrieved_policies": policies}


# ─────────────────────────────────────────────────────────────────────────────
# Node 2 — Classifier
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = dedent("""\
    You are a senior Supply Chain Risk Analyst at a global procurement firm.
    Your role is to assess vendor communications and operational logs against
    internal compliance policies and classify the risk they represent.

    You will be given:
      1. A raw vendor communication or operational log.
      2. A set of relevant internal compliance policies.

    Your task is to:
      - Identify all specific risk factors present in the log.
      - Cross-reference each risk factor against the provided policies.
      - Assign an overall risk_level: LOW, MEDIUM, HIGH, or CRITICAL.
      - Provide a confidence_score (0.0–1.0) for your classification.
      - Recommend a concrete, actionable next step for the procurement team.
      - Explain your reasoning step-by-step.

    Be precise, evidence-based, and cite specific policy references (e.g., POLICY-001)
    in your reasoning wherever applicable.
""")


def classifier_node(state: VendorNegotiationState) -> dict:
    """
    Classify the vendor risk using an LLM with structured output.

    Uses `ChatGoogleGenerativeAI.with_structured_output(RiskClassification)` to
    guarantee the response conforms to the Pydantic schema — no manual parsing
    needed.

    NOTE — this node is NOT deterministic, despite temperature=0:
      - temperature=0 reduces sampling variance; it does not guarantee identical
        output across calls, model versions, or provider-side changes.
      - The prompt itself varies between processes: `retrieve_policies` builds its
        result from a `set` (app/tools.py:126,135), and Python randomises string
        hashing per process, so policy ORDER changes between server restarts.
    See docs/SLOS.md §4 — determinism is a measured target (>=98% agreement across
    runs), not a property this code provides.

    Updates:
        risk_classification: a validated RiskClassification Pydantic model.
    """
    vendor_id = state["vendor_id"]
    raw_log_text = state["raw_log_text"]
    retrieved_policies = state["retrieved_policies"]

    logger.info("classifier_node | START | vendor_id=%s", vendor_id)

    settings = get_settings()

    # Build the structured LLM — temperature=0 for deterministic output
    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model_name,
        google_api_key=settings.gemini_api_key.get_secret_value(),
        temperature=0,
    )
    structured_llm = llm.with_structured_output(RiskClassification)

    # Construct the user prompt with all context
    policies_block = "\n\n".join(
        f"[{i + 1}] {policy}" for i, policy in enumerate(retrieved_policies)
    )
    user_prompt = dedent(f"""\
        ## Vendor Communication Log
        Vendor ID: {vendor_id}

        {raw_log_text}

        ---

        ## Applicable Compliance Policies
        {policies_block}

        ---

        Please classify the risk level of this vendor communication against the
        policies above and return your structured assessment.
    """)

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=user_prompt),
    ]

    risk_classification: RiskClassification = structured_llm.invoke(messages)

    logger.info(
        "classifier_node | DONE | vendor_id=%s | risk_level=%s | confidence=%.2f",
        vendor_id,
        risk_classification.risk_level,
        risk_classification.confidence_score,
    )

    return {"risk_classification": risk_classification}


# ─────────────────────────────────────────────────────────────────────────────
# Node 3 — Action
# ─────────────────────────────────────────────────────────────────────────────


def action_node(state: VendorNegotiationState) -> dict:
    """
    Execute a remediation action based on the risk classification.

    This node is only reached when the conditional edge in graph.py routes
    here (i.e., risk_level is HIGH or CRITICAL). However, we still guard
    defensively with a None check.

    If risk is HIGH or CRITICAL:
        Calls `execute_vendor_pause` to suspend the vendor in the
        procurement system and logs the outcome.

    Updates:
        action_taken: a human-readable description of the action performed.
    """
    vendor_id = state["vendor_id"]
    risk_classification = state.get("risk_classification")

    logger.info("action_node | START | vendor_id=%s", vendor_id)

    if risk_classification is None:
        logger.error(
            "action_node | risk_classification is None for vendor_id=%s — "
            "this node should only be reached after classifier_node runs. "
            "Skipping action.",
            vendor_id,
        )
        return {"action_taken": "error:missing_risk_classification"}

    risk_level = risk_classification.risk_level

    if risk_level not in _HIGH_RISK_LEVELS:
        # Safety guard — should not normally be reached given the conditional edge
        logger.info(
            "action_node | vendor_id=%s | risk_level=%s — no action required",
            vendor_id,
            risk_level,
        )
        return {"action_taken": f"no_action_required:{risk_level}"}

    # Build a concise reason string from the classification
    reason = (
        f"[{risk_level}] {risk_classification.recommended_action} "
        f"| Factors: {'; '.join(risk_classification.risk_factors[:3])}"
    )

    logger.warning(
        "action_node | vendor_id=%s | risk_level=%s | executing vendor pause",
        vendor_id,
        risk_level,
    )

    # Must use .invoke() — @tool wraps the function into a BaseTool instance
    confirmation: str = execute_vendor_pause.invoke(
        {"vendor_id": vendor_id, "reason": reason}
    )

    logger.warning(
        "action_node | DONE | vendor_id=%s | confirmation=%s",
        vendor_id,
        confirmation,
    )

    return {"action_taken": confirmation}
