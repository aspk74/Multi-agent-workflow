"""
app/config.py
─────────────
Centralised configuration via pydantic-settings.

All settings are loaded from environment variables (with .env file support).
The `get_settings()` function uses `@lru_cache` to return a singleton — safe
to call from anywhere without re-loading the environment on every call.

Usage:
    from app.config import get_settings
    settings = get_settings()
    print(settings.gemini_model_name)
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.
    Pydantic-settings will also load from a `.env` file if present.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # silently discard unknown env vars
    )

    # ── Google Gemini ────────────────────────────────────────────────────────
    gemini_api_key: SecretStr = Field(
        description="Google AI Studio / Vertex AI API key — required for the classifier LLM node.",
    )
    gemini_model_name: str = Field(
        default="gemini-3.5-flash",
        description="Gemini model to use for risk classification.",
    )

    # ── Pinecone — DEAD, scheduled for removal in Phase 2 ───────────────────
    # Neither field is read by any code path. `langchain-pinecone` is a declared
    # dependency (pyproject.toml:19) that is never imported. The vector store
    # decision was settled as Postgres + pgvector — see
    # docs/decisions/0002-vector-store.md. Removing these is a breaking config
    # change, tracked as docs/LLD.md §8 row 6.
    pinecone_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="DEAD — never read. Removal scheduled for Phase 2.",
    )
    pinecone_index_name: str = Field(
        default="vendor-compliance-policies",
        description="DEAD — never read. Removal scheduled for Phase 2.",
    )

    # ── Application ─────────────────────────────────────────────────────────
    log_level: str = Field(
        default="INFO",
        description="Python logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL",
    )
    # DEAD — never read by any code. uvicorn takes host/port from its own CLI
    # args (see README). Removal scheduled for Phase 2, docs/LLD.md §8 row 6.
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.
    Uses lru_cache so the .env file is only parsed once across the process.
    """
    return Settings()  # type: ignore[call-arg]


def configure_logging() -> None:
    """
    Configure the root logger using the LOG_LEVEL from settings.
    Call once at application startup (main.py lifespan).
    """
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )

    # Silence overly verbose third-party loggers at DEBUG level
    for noisy_logger in ("httpx", "httpcore", "google", "pinecone"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
