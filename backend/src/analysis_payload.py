"""Normalize AI provider fields on analysis API / WebSocket payloads."""

from typing import Any, Dict

from src.config import AI_PROVIDER, settings


def enrich_analysis_ai_provider(payload: Dict[str, Any]) -> None:
    """Ensure ``ai_provider`` and ``xai_analysis.provider`` / ``model`` for UI (mutates *payload*)."""
    escalated = bool(payload.get("escalated_to_xai") or payload.get("escalated_to_ai"))
    if not escalated:
        return

    xa = payload.get("xai_analysis")
    if not isinstance(xa, dict):
        xa = {}
        payload["xai_analysis"] = xa

    model_raw = str(xa.get("model") or "").strip()
    prov = AI_PROVIDER
    xa["provider"] = prov
    if not model_raw:
        xa["model"] = getattr(settings, "xai_model", "grok-4.3")

    payload["ai_provider"] = prov
    payload["ai_analysis"] = xa
    payload["escalated_to_ai"] = True
