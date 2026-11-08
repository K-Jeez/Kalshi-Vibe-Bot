"""Tuning API payload (xAI-only)."""

from types import SimpleNamespace

from src.api.tuning import tuning_state_payload
from src.config import settings as app_settings


def test_tuning_state_payload_includes_xai_model(monkeypatch):
    monkeypatch.setattr(app_settings, "xai_model", "grok-4.3-test")
    row = SimpleNamespace(
        stop_loss_drawdown_pct=0.80,
        min_edge_to_buy_pct=3,
        stop_loss_selling_enabled=False,
        min_ai_win_prob_buy_side_pct=60,
        max_open_positions=30,
        ai_provider="xai",
        updated_at=None,
    )
    p = tuning_state_payload(row)
    assert p["ai_provider"] == "xai"
    assert p["xai_model"] == "grok-4.3-test"
    assert "xai_model" in p
