"""
app/state.py
────────────
LangGraph state definition for the Vendor Negotiation & Risk Matrix graph.

The `VendorNegotiationState` TypedDict is the single shared data structure
passed between all nodes in the graph. Each node receives the full state and
returns a *partial* dict containing only the keys it has updated.

Design notes:
  - We deliberately avoid `Annotated` reducers here because every field is
    set exactly once (or overwritten) — no concurrent fan-out accumulation.
  - `risk_classification` is Optional because the classifier node sets it;
    before that node runs, the field is None.
  - `retrieved_policies` is initialised as an empty list by the caller and
    populated by the researcher node.
  - `action_taken` is a human-readable status string (e.g. "none",
    "VENDOR_PAUSED | txn_id=...").

IMPORTANT — LangGraph introspection:
  LangGraph calls `typing.get_type_hints()` on the TypedDict schema at graph
  construction time to resolve all type annotations at runtime. This means
  any forward references (strings or TYPE_CHECKING-guarded imports) will
  raise a NameError during `StateGraph(VendorNegotiationState)`.
  Therefore, `RiskClassification` MUST be a real runtime import here,
  not guarded behind `if TYPE_CHECKING`.
"""

from __future__ import annotations

from typing import Optional

from typing_extensions import TypedDict

# Real runtime import required — LangGraph resolves type hints at graph build time.
from app.models import RiskClassification  # noqa: F401 (used in type hints below)


class VendorNegotiationState(TypedDict):
    """
    The shared state that flows through every node of the LangGraph workflow.

    Fields:
        vendor_id           Unique identifier for the vendor being assessed.
        raw_log_text        The raw vendor communication / log text to analyse.
        retrieved_policies  List of compliance policy strings returned by the
                            researcher node. Currently a keyword match over a
                            6-entry dict (app/tools.py:88-146) — no vector store
                            is involved. Target: Postgres + pgvector, returning
                            policy IDs rather than bare strings. See
                            docs/decisions/0002-vector-store.md.
        risk_classification Structured Pydantic model produced by the
                            classifier node. None until that node executes.
        action_taken        Human-readable description of the action executed
                            by the action node, or "none" if no action taken.
    """

    vendor_id: str
    raw_log_text: str
    retrieved_policies: list[str]
    risk_classification: Optional[RiskClassification]
    action_taken: str
