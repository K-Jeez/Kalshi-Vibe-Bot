"""AI provider helpers (xAI / Grok only)."""

from src.ai_provider import ai_provider_display_name, ai_provider_log_label, normalize_ai_provider


def test_normalize_ai_provider_always_xai():
    assert normalize_ai_provider(None) == "xai"
    assert normalize_ai_provider("") == "xai"
    assert normalize_ai_provider("xai") == "xai"
    assert normalize_ai_provider("XAI") == "xai"
    assert normalize_ai_provider("anything-else") == "xai"


def test_ai_provider_display_name():
    assert ai_provider_display_name(None) == "xAI"
    assert ai_provider_display_name("xai") == "xAI"


def test_ai_provider_log_label():
    assert ai_provider_log_label(None) == "xAI"
    assert ai_provider_log_label("xai") == "xAI"
