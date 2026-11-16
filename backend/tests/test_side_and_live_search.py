"""A model SKIP still gets a server-chosen side, and in-play search is one call per event."""

from __future__ import annotations

import asyncio

from src.clients.xai_client import XAIClient, live_search_parameters
from src.decision_engine.analyzer import DecisionEngine
from src.decision_engine.strategy_math import derive_buy_side
from src.event_live_lookup import clear_event_search_cache, context_for_market


def test_side_follows_the_larger_executable_edge():
    # AI 68 vs a 45¢ YES ask: YES edge is +23, NO edge is negative.
    assert derive_buy_side(68, 0.45, 0.55, 0.45, 0.55) == "YES"
    # AI 25 vs a 59¢ YES ask and a 41¢ NO ask: the NO side carries the edge.
    assert derive_buy_side(25, 0.59, 0.41, 0.59, 0.41) == "NO"


def test_model_skip_still_prices_the_side_and_keeps_kelly():
    engine = DecisionEngine(xai_api_key="test")

    async def fake(**_kwargs):
        return {
            "direction": "SKIP",
            "ai_probability_yes_pct": 68,
            "reasoning": "calibration rules say skip on a large divergence",
            "real_time_context": "None found",
            "key_factors": [],
            "evidence": [],
            "uncertainty": {"level": "high", "missing": ["score"]},
        }

    engine.xai.analyze_market = fake
    row = asyncio.run(
        engine.analyze_market(
            market_id="KXNCAAMBSPREAD-26NOV15PEPPORE-ORE17",
            market_title="Pepperdine",
            market_description="",
            current_prices={
                "yes": 0.45,
                "no": 0.55,
                "yes_ask": 0.45,
                "no_ask": 0.55,
                "fee_type": "quadratic",
                "fee_multiplier": 1,
            },
            volume=8000,
            deployable_balance=100,
        )
    )
    assert row["decision"] == "BUY_YES"
    assert row["ai_probability_yes_pct"] == 68
    assert row["ai_probability_for_side_pct"] == 68
    assert row["edge_pct"] == 23.0
    assert row["kelly_contracts"] > 0
    assert row["xai_analysis"]["blended_side_pct"] is not None
    assert row["xai_analysis"]["model_direction"] == "SKIP"
    assert row["xai_analysis"]["uncertainty"]["level"] == "high"

    async def low_yes(**_kwargs):
        return {
            "direction": "SKIP",
            "ai_probability_yes_pct": 25,
            "reasoning": "diverges from the market",
            "real_time_context": "",
            "key_factors": [],
            "evidence": [],
        }

    engine.xai.analyze_market = low_yes
    no_row = asyncio.run(
        engine.analyze_market(
            market_id="KXNFLTOTAL-26NOV15SFDAL-71",
            market_title="Total",
            market_description="",
            current_prices={
                "yes": 0.59,
                "no": 0.41,
                "yes_ask": 0.59,
                "no_ask": 0.41,
                "fee_type": "quadratic",
                "fee_multiplier": 1,
            },
            volume=8000,
            deployable_balance=100,
        )
    )
    assert no_row["decision"] == "BUY_NO"
    assert no_row["ai_probability_for_side_pct"] == 75
    assert no_row["xai_analysis"]["blended_side_pct"] is not None
    assert no_row["edge_pct"] == 34.0


class _Search:
    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    async def search_event_facts(self, _title, *, max_results=4):
        self.calls += 1
        assert max_results == 4
        return self.facts


def test_one_search_is_shared_and_an_empty_result_skips_before_the_model():
    clear_event_search_cache()
    client = _Search("Home 21, away 18, 3:12 left")
    market = {"id": "PROP-1", "event_ticker": "GAME-1"}
    first = asyncio.run(
        context_for_market(
            market=market,
            fact={"skip": False, "context": "", "reason": ""},
            client=client,
            seconds_until=600,
            title="Home vs Away",
        )
    )
    second = asyncio.run(
        context_for_market(
            market={"id": "PROP-2", "event_ticker": "GAME-1"},
            fact={"skip": False, "context": "", "reason": ""},
            client=client,
            seconds_until=600,
            title="Home vs Away player points",
        )
    )
    assert first == ("Home 21, away 18, 3:12 left", "")
    assert second == first
    assert client.calls == 1

    clear_event_search_cache()
    empty = _Search("")
    skipped = asyncio.run(
        context_for_market(
            market=market,
            fact={"skip": False, "context": "", "reason": ""},
            client=empty,
            seconds_until=600,
            title="Home vs Away",
        )
    )
    assert skipped[0] == ""
    assert "skipped before the model" in skipped[1]
    again = asyncio.run(
        context_for_market(
            market={"id": "PROP-2", "event_ticker": "GAME-1"},
            fact={"skip": False, "context": "", "reason": ""},
            client=empty,
            seconds_until=500,
            title="same game",
        )
    )
    assert again[1] == skipped[1]
    assert empty.calls == 1


def test_a_live_feed_or_a_search_error_does_not_add_a_lookup():
    clear_event_search_cache()
    client = _Search("should not be asked")
    fed = asyncio.run(
        context_for_market(
            market={"id": "M", "event_ticker": "E"},
            fact={"skip": False, "context": "ESPN: tied 10-10", "reason": ""},
            client=client,
            seconds_until=100,
            title="Game",
        )
    )
    assert fed == ("ESPN: tied 10-10", "")
    assert client.calls == 0

    pregame = asyncio.run(
        context_for_market(
            market={"id": "M2", "event_ticker": "E2"},
            fact={"skip": False, "context": "", "reason": ""},
            client=client,
            seconds_until=20000,
            title="Tomorrow",
        )
    )
    assert pregame == ("", "")
    assert client.calls == 0

    clear_event_search_cache()
    failed = _Search(None)
    still_open = asyncio.run(
        context_for_market(
            market={"id": "M3", "event_ticker": "E3"},
            fact={"skip": False, "context": "", "reason": ""},
            client=failed,
            seconds_until=400,
            title="Live game",
        )
    )
    assert still_open == ("", "")
    assert failed.calls == 1


def test_search_payload_uses_web_and_x_only(monkeypatch):
    captured = {}

    async def fake_post(**kwargs):
        captured.update(kwargs["payload"])

        class Response:
            def json(self):
                return {"choices": [{"message": {"content": '{"facts":"none found"}'}}]}

        return "ok", Response()

    monkeypatch.setattr("src.clients.xai_client._post_xai_chat_with_retries", fake_post)
    text = asyncio.run(XAIClient(api_key="x", model="grok-4.3").search_event_facts("Game", max_results=99))
    assert text == ""
    assert captured["search_parameters"] == live_search_parameters(99)
    assert captured["search_parameters"]["sources"] == [{"type": "web"}, {"type": "x"}]
    assert captured["search_parameters"]["max_search_results"] == 8
    assert "search_parameters" not in captured["messages"][0]
