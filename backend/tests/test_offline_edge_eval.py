"""Checks for the offline edge study. These tests are not a performance result."""

from __future__ import annotations

import inspect
import random
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import offline_edge_eval as ev  # noqa: E402


def test_module_uses_corrected_stake_cap():
    ev.assert_corrected_stake_cap()
    assert ev.kelly_contract_cap_for_bankroll(10.46, 0.67, fee_multiplier=1, fee_type="quadratic") == 0


def test_forecast_and_decision_have_no_outcome_argument():
    assert "y" not in inspect.signature(ev.forecast_yes_pct).parameters
    assert "y" not in inspect.signature(ev.decide_buy).parameters
    assert "outcome" not in inspect.signature(ev.forecast_yes_pct).parameters


def test_overconfident_forecast_ignores_true_probability():
    first = ev.forecast_yes_pct("overconfident", 0.70, 0.10, random.Random(1))
    second = ev.forecast_yes_pct("overconfident", 0.70, 0.90, random.Random(1))
    assert first == second


def test_small_account_skip_and_honest_market_skip():
    ev.self_check()


def test_zero_fill_scenario_does_not_invent_a_win_rate():
    scenario = next(s for s in ev.scenario_catalog() if s.scenario_id == "eff_truep_spread0")
    row = ev.run_scenario(scenario, n_reps=2)
    assert row["total_fills"] == 0
    assert row["side_win_rate"] is None
    assert row["profitable_fill_rate"] is None
    assert row["mean_net_pnl_per_fill"] is None
    assert row["mean_terminal_return"] == 0.0


def test_same_seed_is_repeatable():
    scenario = next(s for s in ev.scenario_catalog() if s.scenario_id == "eff_null_c10_f0")
    first = ev.run_scenario(scenario, n_reps=2)
    second = ev.run_scenario(scenario, n_reps=2)
    assert first["total_fills"] == second["total_fills"]
    assert first["mean_terminal_return"] == second["mean_terminal_return"]
    assert first["brier_model"] == second["brier_model"]


def test_contracts_keep_forecasts_and_binary_outcomes():
    scenario = next(s for s in ev.scenario_catalog() if s.scenario_id == "eff_null_c500_f0")
    contracts = ev.build_contracts(scenario, rep=4)
    assert len(contracts) == ev.N_WAVES * ev.PER_WAVE
    assert all(c["y"] in (0, 1) for c in contracts)
    assert all(5 <= c["ai_yes_pct"] <= 95 for c in contracts)
