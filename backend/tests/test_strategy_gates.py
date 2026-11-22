"""Autonomous strategy guardrails."""

from src.decision_engine.strategy_gates import (
    MAX_EDGE_TO_BUY_PCT,
    autonomous_buy_gate_failure,
    effective_min_edge_for_market,
    effective_scan_min_volume,
    is_sports_market_title,
)


def test_is_sports_market_title():
    assert is_sports_market_title("Team A vs Team B: Total Goals")
    assert not is_sports_market_title("Bitcoin price on May 17")


def test_effective_scan_min_volume_sports():
    assert effective_scan_min_volume(1500, "A vs B") >= 2000


def test_effective_min_edge_matches_settings():
    assert effective_min_edge_for_market(5, "A vs B") == 5
    assert effective_min_edge_for_market(5, "Bitcoin") == 5


def test_autonomous_max_edge():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=70, edge_pct=30, entry_price_dollars=0.5)


def test_autonomous_overconfidence_guard_is_gone():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=80, edge_pct=12, entry_price_dollars=0.72) is None


def test_autonomous_passes_sweet_spot():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=68, edge_pct=12, entry_price_dollars=0.62) is None


def test_autonomous_passes_high_confidence_favorite():
    """85% on NO at 73¢ (market favorite) should not be blocked by the global AI cap."""
    assert (
        autonomous_buy_gate_failure(side="NO", ai_yes_pct=15, edge_pct=12.0, entry_price_dollars=0.73)
        is None
    )


def test_ai_probability_cap_is_off():
    # 95% on the NO side at 73¢ used to hit the 90% cap. The cap is off.
    assert (
        autonomous_buy_gate_failure(side="NO", ai_yes_pct=5, edge_pct=12.0, entry_price_dollars=0.73)
        is None
    )


def test_constants_sane():
    assert MAX_EDGE_TO_BUY_PCT == 22
