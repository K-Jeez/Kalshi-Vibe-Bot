"""Autonomous strategy guardrails."""

from src.decision_engine.strategy_gates import (
    MAX_EDGE_TO_BUY_PCT,
    MAX_KELLY_BANKROLL_PCT,
    autonomous_buy_gate_failure,
    effective_min_edge_for_market,
    effective_scan_min_volume,
    exit_grace_minutes_for_market,
    is_sports_market_title,
    kelly_contract_cap_for_bankroll,
)


def test_is_sports_market_title():
    assert is_sports_market_title("Team A vs Team B: Total Goals")
    assert not is_sports_market_title("Bitcoin price on May 17")


def test_effective_scan_min_volume_sports():
    assert effective_scan_min_volume(1500, "A vs B") >= 2000


def test_effective_min_edge_matches_settings():
    assert effective_min_edge_for_market(5, "A vs B") == 5
    assert effective_min_edge_for_market(5, "Bitcoin") == 5


def test_exit_grace_is_removed():
    assert exit_grace_minutes_for_market(10, "A vs B") == 0
    assert exit_grace_minutes_for_market(10, "Bitcoin") == 0


def test_autonomous_max_edge():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=70, edge_pct=30, entry_price_dollars=0.5)


def test_autonomous_overconfidence_mid_chalk():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=80, edge_pct=12, entry_price_dollars=0.55)


def test_autonomous_passes_sweet_spot():
    assert autonomous_buy_gate_failure(side="YES", ai_yes_pct=68, edge_pct=12, entry_price_dollars=0.48) is None


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


def test_kelly_contract_cap_scales_with_bankroll():
    assert kelly_contract_cap_for_bankroll(75, 0.50, fee_multiplier=1, fee_type="quadratic") == 3
    assert kelly_contract_cap_for_bankroll(1000, 0.50, fee_multiplier=1, fee_type="quadratic") == 48


def test_kelly_contract_cap_small_bankroll_skips_instead_of_full_cash():
    # 2.5% of $10.46 cannot buy one contract at 67¢. Do not size up to full cash.
    assert kelly_contract_cap_for_bankroll(10.46, 0.67, fee_multiplier=1, fee_type="quadratic") == 0


def test_history_floor_skip_names_the_add_on():
    from src.decision_engine.history_sizing import edge_floor_skip_text

    text = edge_floor_skip_text(4.0, 5.0, 4.0, "4-6")
    assert "edge 4.0 below 5.0" in text
    assert "your setting is 4" in text
    assert "past 4-6 point trades lost money" in text


def test_constants_sane():
    assert MAX_EDGE_TO_BUY_PCT == 22
    assert MAX_KELLY_BANKROLL_PCT == 0.025
