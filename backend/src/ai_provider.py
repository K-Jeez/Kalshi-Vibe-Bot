"""AI provider helpers (live app uses xAI / Grok only)."""

from __future__ import annotations

from typing import Literal

from src.config import AI_PROVIDER

AiProvider = Literal["xai"]


def normalize_ai_provider(value: object = None, *, default: str = AI_PROVIDER) -> AiProvider:
    return AI_PROVIDER


def ai_provider_display_name(provider: object = None) -> str:
    return "xAI"


def ai_provider_log_label(provider: object = None) -> str:
    """Short label for log messages (matches client logger names where possible)."""
    return "xAI"
