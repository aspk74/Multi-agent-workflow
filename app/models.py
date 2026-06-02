"""
app/models.py
─────────────
Pydantic v2 data models for the Vendor Risk Matrix system.

Separating models from state keeps concerns clean:
  - RiskClassification: LLM-produced structured output (validated by Pydantic)
  - VendorNegotiationState: LangGraph state flow (defined in state.py as TypedDict)
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RiskClassification(BaseModel):
    """
    Structured output produced by the classifier LLM node.
    Maps directly to the JSON schema sent to OpenAI via `with_structured_output`.
    """

    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(
        description=(
            "The overall risk level assigned to this vendor based on the log analysis. "
            "LOW = minor/isolated issues, MEDIUM = notable concerns requiring monitoring, "
            "HIGH = serious violations requiring immediate review, "
            "CRITICAL = contract-threatening violations requiring emergency action."
        )
    )

    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence in the risk classification, between 0.0 (uncertain) and 1.0 (certain).",
    )

    risk_factors: list[str] = Field(
        description="Exhaustive list of specific risk factors identified in the vendor log.",
        min_length=1,
    )

    recommended_action: str = Field(
        description=(
            "A concise, actionable recommendation (1–2 sentences) for the procurement team "
            "based on the identified risk level."
        )
    )

    reasoning: str = Field(
        description=(
            "Step-by-step reasoning explaining how the risk factors and retrieved policies "
            "together led to the final risk_level classification."
        )
    )
