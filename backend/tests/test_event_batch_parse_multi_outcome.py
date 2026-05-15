"""Event-batch JSON: mutually exclusive outcome map + P(YES) reconciliation."""

import json

from src.clients.xai_client import _parse_event_batch_json


def test_partition_map_reconciles_ai_yes_for_chosen_leg():
    allowed = {"M1", "M2", "M3"}
    content = json.dumps(
        {
            "best_market_id": "M2",
            "direction": "YES",
            "ai_probability_yes_pct": 90,
            "outcome_probability_pct_by_market_id": {"M1": 35, "M2": 42, "M3": 23},
            "reasoning": "partition",
            "real_time_context": "x",
            "key_factors": [],
            "evidence": [],
        }
    )
    p = _parse_event_batch_json(content, allowed_ids=allowed)
    assert p["ai_probability_yes_pct"] == 42
    assert p["outcome_probability_pct_by_market_id"]["M2"] == 42


def test_partial_map_keeps_model_ai_yes():
    allowed = {"M1", "M2", "M3"}
    content = json.dumps(
        {
            "best_market_id": "M2",
            "direction": "YES",
            "ai_probability_yes_pct": 55,
            "outcome_probability_pct_by_market_id": {"M1": 40, "M2": 60},
            "reasoning": "incomplete partition",
            "real_time_context": "x",
            "key_factors": [],
            "evidence": [],
        }
    )
    p = _parse_event_batch_json(content, allowed_ids=allowed)
    assert p["ai_probability_yes_pct"] == 55
    assert "outcome_probability_pct_by_market_id" in p
    assert set(p["outcome_probability_pct_by_market_id"].keys()) == {"M1", "M2"}


def test_bad_sum_ignores_reconciliation():
    allowed = {"M1", "M2", "M3"}
    content = json.dumps(
        {
            "best_market_id": "M2",
            "direction": "YES",
            "ai_probability_yes_pct": 55,
            "outcome_probability_pct_by_market_id": {"M1": 10, "M2": 20, "M3": 30},
            "reasoning": "sum 60",
            "real_time_context": "x",
            "key_factors": [],
            "evidence": [],
        }
    )
    p = _parse_event_batch_json(content, allowed_ids=allowed)
    assert p["ai_probability_yes_pct"] == 55


def test_ladder_batch_skips_partition_map_and_reconciliation():
    allowed = {"M1", "M2", "M3"}
    content = json.dumps(
        {
            "best_market_id": "M2",
            "direction": "YES",
            "ai_probability_yes_pct": 55,
            "outcome_probability_pct_by_market_id": {"M1": 35, "M2": 42, "M3": 23},
            "reasoning": "x",
            "real_time_context": "x",
            "key_factors": [],
            "evidence": [],
        }
    )
    p = _parse_event_batch_json(content, allowed_ids=allowed, ladder_stat_line_batch=True)
    assert p["ai_probability_yes_pct"] == 55
    assert "outcome_probability_pct_by_market_id" not in p
