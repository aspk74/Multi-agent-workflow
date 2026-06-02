"""
app/graph.py
────────────
LangGraph StateGraph definition for the Vendor Negotiation & Risk Matrix.

Graph topology:
    START → researcher → classifier → [conditional] → action → END
                                                    ↘ END (if LOW/MEDIUM)

The graph is compiled eagerly at module level (`compiled_graph`).
`StateGraph.compile()` is synchronous and requires no event loop, so it is
safe to do this at import time rather than inside a FastAPI lifespan handler.

Usage:
    from app.graph import compiled_graph

    # Synchronous
    result = compiled_graph.invoke(initial_state)

    # Asynchronous (preferred in FastAPI)
    result = await compiled_graph.ainvoke(initial_state)
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents import action_node, classifier_node, researcher_node
from app.state import VendorNegotiationState

logger = logging.getLogger(__name__)

# Risk levels that trigger the action node
_HIGH_RISK_LEVELS = frozenset({"HIGH", "CRITICAL"})


# ─────────────────────────────────────────────────────────────────────────────
# Routing function
# ─────────────────────────────────────────────────────────────────────────────


def route_by_risk(state: VendorNegotiationState) -> str:
    """
    Conditional edge: determine next node based on the risk classification.

    Safely guards against `risk_classification` being None — if the classifier
    node somehow failed to populate it, we route to END rather than crashing.

    Returns:
        "action" if risk_level is HIGH or CRITICAL.
        "end"    otherwise (LOW, MEDIUM, or missing classification).
    """
    risk_classification = state.get("risk_classification")

    if risk_classification is None:
        logger.warning(
            "route_by_risk | risk_classification is None — routing to END as fallback"
        )
        return "end"

    risk_level = risk_classification.risk_level
    decision = "action" if risk_level in _HIGH_RISK_LEVELS else "end"

    logger.info(
        "route_by_risk | risk_level=%s → routing to '%s'",
        risk_level,
        decision,
    )
    return decision


# ─────────────────────────────────────────────────────────────────────────────
# Graph builder
# ─────────────────────────────────────────────────────────────────────────────


def build_graph() -> CompiledStateGraph:
    """
    Construct and compile the Vendor Risk Matrix StateGraph.

    Node registration order does not affect execution order — that is
    determined purely by edges.

    Returns:
        A compiled LangGraph `CompiledGraph` ready for `.invoke()` or
        `.ainvoke()`.
    """
    builder: StateGraph = StateGraph(VendorNegotiationState)

    # ── Register nodes ───────────────────────────────────────────────────────
    builder.add_node("researcher", researcher_node)
    builder.add_node("classifier", classifier_node)
    builder.add_node("action", action_node)

    # ── Define edges ─────────────────────────────────────────────────────────
    # Entry point: always start with policy retrieval
    builder.add_edge(START, "researcher")

    # Researcher always hands off to classifier
    builder.add_edge("researcher", "classifier")

    # Classifier fans out conditionally based on risk level
    builder.add_conditional_edges(
        source="classifier",
        path=route_by_risk,
        path_map={
            "action": "action",  # HIGH / CRITICAL → pause vendor
            "end": END,          # LOW / MEDIUM    → terminate workflow
        },
    )

    # Action node always terminates the workflow
    builder.add_edge("action", END)

    logger.info("build_graph | compiling StateGraph")
    compiled = builder.compile()
    logger.info("build_graph | graph compiled successfully")

    return compiled  # type: CompiledStateGraph


# ─────────────────────────────────────────────────────────────────────────────
# Eagerly compile at module level
# ─────────────────────────────────────────────────────────────────────────────
# `compile()` is synchronous CPU-bound work with no I/O — safe to do at import
# time. This avoids rebuilding the graph on every request and keeps `main.py`
# lifespan logic focused on async resources (DB connections, etc.).

compiled_graph: CompiledStateGraph = build_graph()
