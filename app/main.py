"""
app/main.py
───────────
FastAPI application — Vendor Negotiation & Risk Matrix.

Exposes two endpoints:
  GET  /health                  Liveness / readiness probe
  POST /webhook/vendor-log      Trigger the LangGraph risk assessment workflow

The compiled graph is imported from `app.graph` (eagerly built at module load).
The `lifespan` context manager handles application-level setup/teardown
(logging configuration, future async clients like Pinecone SDK, etc.) but does
NOT compile the graph — that happens synchronously at import time.

To run:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import configure_logging, get_settings
from app.graph import compiled_graph
from app.models import RiskClassification

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Lifespan — startup / shutdown
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """
    Application lifecycle manager (replaces deprecated @app.on_event).

    Startup:
      - Configure logging from LOG_LEVEL env var.
      - Log settings summary (omitting secrets).
      - Future: initialise async Pinecone client, connection pools, etc.

    Shutdown:
      - Future: gracefully close DB connections, flush queues, etc.
    """
    # ── Startup ──────────────────────────────────────────────────────────────
    configure_logging()
    settings = get_settings()

    logger.info("=== Vendor Negotiation & Risk Matrix — STARTING UP ===")
    logger.info(
        "Settings | model=%s | pinecone_index=%s | log_level=%s",
        settings.gemini_model_name,
        settings.pinecone_index_name,
        settings.log_level,
    )
    logger.info("LangGraph compiled_graph ready | nodes=%s", list(compiled_graph.nodes))

    yield  # Application is running

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("=== Vendor Negotiation & Risk Matrix — SHUTTING DOWN ===")


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Autonomous Vendor Negotiation & Risk Matrix",
    description=(
        "A multi-agent supply chain risk assessment system powered by LangGraph, "
        "LangChain, and OpenAI. Accepts vendor communication logs and returns "
        "a structured risk classification with optional automated remediation."
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ─────────────────────────────────────────────────────────────────────────────
# Global exception handler
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Catch-all handler that returns a structured 500 response with a
    correlation ID so clients can reference specific failures in logs.
    """
    correlation_id = str(uuid.uuid4())
    logger.exception(
        "Unhandled exception | correlation_id=%s | path=%s",
        correlation_id,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Contact support with the correlation_id.",
            "correlation_id": correlation_id,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response models
# ─────────────────────────────────────────────────────────────────────────────


class VendorLogPayload(BaseModel):
    """Incoming webhook payload containing the vendor log to assess."""

    vendor_id: str = Field(
        description="Unique identifier for the vendor (e.g. 'V-1234').",
        examples=["V-1234"],
        min_length=1,
    )
    log_text: str = Field(
        description="The raw vendor communication or operational log text to analyse.",
        examples=[
            "Vendor missed delivery deadline by 15 days and increased prices 40% without notice."
        ],
        min_length=10,
    )


class WorkflowResult(BaseModel):
    """Response returned after the LangGraph workflow completes."""

    vendor_id: str = Field(description="The vendor ID that was assessed.")
    risk_classification: RiskClassification | None = Field(
        description="Structured risk classification produced by the classifier node.",
    )
    action_taken: str = Field(
        description="Description of the action executed (or 'none' if no action was required).",
    )
    retrieved_policies_count: int = Field(
        description="Number of compliance policies retrieved and used in the assessment.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["Ops"])
async def health_check() -> dict[str, Any]:
    """
    Liveness / readiness probe.
    Returns HTTP 200 when the application is running and the graph is compiled.
    """
    return {
        "status": "ok",
        "graph_nodes": list(compiled_graph.nodes),
    }


@app.post(
    "/webhook/vendor-log",
    response_model=WorkflowResult,
    status_code=200,
    tags=["Vendor Risk"],
    summary="Trigger vendor risk assessment workflow",
    description=(
        "Accepts a vendor communication log, runs the multi-agent LangGraph workflow "
        "(policy retrieval → risk classification → optional vendor pause), "
        "and returns the structured assessment result."
    ),
)
async def handle_vendor_log(payload: VendorLogPayload) -> WorkflowResult:
    """
    Main webhook handler. Initialises the LangGraph state and invokes the
    compiled graph asynchronously, then maps the final state to a response.

    Raises:
        HTTPException 422: if the payload fails Pydantic validation (handled by FastAPI).
        HTTPException 500: if the graph execution raises an unhandled error.
    """
    correlation_id = str(uuid.uuid4())

    logger.info(
        "handle_vendor_log | correlation_id=%s | vendor_id=%s | log_length=%d chars",
        correlation_id,
        payload.vendor_id,
        len(payload.log_text),
    )

    # Initialise state with all required keys set to their zero values.
    # `risk_classification` starts as None — set by classifier_node.
    # `action_taken` starts as "none" — overwritten by action_node if triggered.
    initial_state = {
        "vendor_id": payload.vendor_id,
        "raw_log_text": payload.log_text,
        "retrieved_policies": [],
        "risk_classification": None,
        "action_taken": "none",
    }

    try:
        final_state: dict[str, Any] = await compiled_graph.ainvoke(initial_state)
    except Exception as exc:
        logger.exception(
            "handle_vendor_log | graph execution failed | correlation_id=%s | vendor_id=%s",
            correlation_id,
            payload.vendor_id,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Graph execution failed. correlation_id={correlation_id}",
        ) from exc

    retrieved_policies: list[str] = final_state.get("retrieved_policies", [])

    logger.info(
        "handle_vendor_log | COMPLETE | correlation_id=%s | vendor_id=%s | "
        "risk_level=%s | action_taken=%s",
        correlation_id,
        payload.vendor_id,
        final_state.get("risk_classification", {}).risk_level
        if final_state.get("risk_classification")
        else "N/A",
        final_state.get("action_taken", "none"),
    )

    return WorkflowResult(
        vendor_id=final_state["vendor_id"],
        risk_classification=final_state.get("risk_classification"),
        action_taken=final_state.get("action_taken", "none"),
        retrieved_policies_count=len(retrieved_policies),
    )
