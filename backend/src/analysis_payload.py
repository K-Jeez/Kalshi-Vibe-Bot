"""Normalize AI provider fields on analysis API / WebSocket payloads."""

from typing import Any, Dict

from src.ai_provider import normalize_ai_provider
from src.config import settings


def enrich_analysis_ai_provider(payload: Dict[str, Any]) -> None:
    """Ensure ``ai_provider`` and ``xai_analysis.provider`` / ``model`` for UI (mutates *payload*)."""
    escalated = bool(payload.get("escalated_to_xai") or payload.get("escalated_to_ai"))
    if not escalated:
        return

    xa = payload.get("xai_analysis")
    if not isinstance(xa, dict):
        xa = {}
        payload["xai_analysis"] = xa

    prov = str(payload.get("ai_provider") or xa.get("provider") or "").lower().strip()
    if prov not in ("gemini", "xai"):
        model = str(xa.get("model") or "").lower()
        if "gemini" in model:
            prov = "gemini"
        elif "grok" in model:
            prov = "xai"
        else:
            prov = normalize_ai_provider(settings.default_ai_provider)

    xa["provider"] = prov
    if not str(xa.get("model") or "").strip():
        xa["model"] = (
            getattr(settings, "gemini_model", "gemini-2.5-flash")
            if prov == "gemini"
            else getattr(settings, "xai_model", "grok-3")
        )

    payload["ai_provider"] = prov
    payload["ai_analysis"] = xa
    payload["escalated_to_ai"] = True
