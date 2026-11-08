"""Analysis payload provider enrichment."""

from src.analysis_payload import enrich_analysis_ai_provider


def test_enrich_event_batch_missing_provider_defaults_to_xai_legacy(monkeypatch):
    monkeypatch.setattr(
        "src.analysis_payload.settings",
        type("S", (), {"xai_model": "grok-4.3"})(),
    )
    row = {
        "escalated_to_xai": True,
        "xai_analysis": {"event_batch": True, "event_ticker": "EV1"},
    }
    enrich_analysis_ai_provider(row)
    assert row["ai_provider"] == "xai"
    assert row["xai_analysis"]["provider"] == "xai"
    assert row["xai_analysis"]["model"] == "grok-4.3"


def test_enrich_preserves_explicit_provider():
    row = {
        "escalated_to_xai": True,
        "xai_analysis": {"provider": "xai", "model": "grok-4.3"},
    }
    enrich_analysis_ai_provider(row)
    assert row["ai_provider"] == "xai"
    assert row["xai_analysis"]["model"] == "grok-4.3"
