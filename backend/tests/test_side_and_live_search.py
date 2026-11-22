"""A model SKIP still gets a server-chosen side, and in-play search is one call per event."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.clients.xai_client import (
    XAIClient,
    begin_live_lookup_scan,
    live_lookup_tools,
    log_live_lookup_summary,
)
from src.decision_engine.analyzer import DecisionEngine
from src.decision_engine.strategy_math import derive_buy_side
from src.event_live_lookup import attach_lookup_cost, clear_event_search_cache, context_for_market
from src.model_usage import tool_invocation_usd


@pytest.fixture(autouse=True)
def _live_search_on(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "vibe_live_search_enabled", True)


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
    assert no_row["edge_pct"] == 34.0


class _Search:
    def __init__(self, facts):
        self.facts = facts
        self.calls = 0

    async def search_event_facts(self, _title, **_kwargs):
        self.calls += 1
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


def _responses_payload():
    answer = '{"facts":"Home 21, away 18, 3:12 left in Q2. Smith 12 pts."}'
    return {
        "status": "completed",
        "output_text": answer,
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "arguments": '{"facts":"WRONG"}',
            },
            {"type": "custom_tool_call", "name": "x_keyword_search", "id": "xs_1"},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": answer,
                        "annotations": [
                            {"type": "url_citation", "url": "https://example.com/game", "title": "Box score"}
                        ],
                    }
                ],
            },
        ],
        "usage": {
            "input_tokens": 200,
            "output_tokens": 40,
            "server_side_tool_usage_details": {
                "web_search_calls": 1,
                "x_search_calls": 1,
                "x_posts_fetched": 4,
                "x_users_fetched": 0,
            },
        },
    }


def test_responses_lookup_parses_output_text_and_prices_tools(monkeypatch):
    captured = {}

    async def fake_post(**kwargs):
        captured.update(kwargs["payload"])
        return ("ok", _responses_payload())

    monkeypatch.setattr("src.clients.xai_client._post_xai_responses", fake_post)
    begin_live_lookup_scan()
    result = asyncio.run(XAIClient(api_key="x", model="grok-4.3").search_event_facts("Game", max_tool_calls=99))
    assert result["facts"].startswith("Home 21")
    assert "WRONG" not in result["facts"]
    assert result["client_error"] is None
    assert captured["model"] == "grok-4.3"
    assert "search_parameters" not in captured
    assert captured["max_output_tokens"] == 1500
    assert captured["max_tool_calls"] == 8
    today = datetime.now(timezone.utc).date()
    assert captured["tools"] == live_lookup_tools(today)
    assert captured["tools"][1]["from_date"] == today.isoformat()
    assert captured["tools"][1]["to_date"] == (today + timedelta(days=1)).isoformat()
    blob = str(captured["tools"])
    assert "enable_image" not in blob
    assert "enable_video" not in blob
    usage = result["usage"]
    assert usage["web_search_calls"] == 1
    assert usage["x_posts_fetched"] == 4
    tool_usd = tool_invocation_usd(web_search_calls=1, x_posts_fetched=4)
    assert tool_usd == 0.025
    assert usage["estimated_usd"] >= tool_usd
    clear_event_search_cache()
    decision = {"xai_analysis": {"usage": {"estimated_usd": 0.01, "input_tokens": 100}}}
    # The HTTP result is not in the unbilled list until lookup_event_facts records it.
    recorded = asyncio.run(
        context_for_market(
            market={"id": "P", "event_ticker": "EVT"},
            fact={"skip": False, "context": "", "reason": ""},
            client=XAIClient(api_key="x", model="grok-4.3"),
            seconds_until=600,
            title="Game",
        )
    )
    assert recorded[0].startswith("Home 21")
    attach_lookup_cost(decision, "EVT")
    merged = decision["xai_analysis"]["usage"]
    assert merged["estimated_usd"] == round(0.01 + usage["estimated_usd"], 6)
    assert merged["web_search_calls"] == 1
    assert merged["lookup_usd"] == usage["estimated_usd"]


def test_lookup_410_is_logged_once_and_does_not_fall_through(monkeypatch, caplog):
    calls = {"n": 0}

    async def fake_post(**_kwargs):
        calls["n"] += 1
        return ("client", 410, "Live search is deprecated. Please switch to the Agent Tools API.")

    monkeypatch.setattr("src.clients.xai_client._post_xai_responses", fake_post)
    begin_live_lookup_scan()
    clear_event_search_cache()
    client = XAIClient(api_key="x", model="grok-4.3")
    with caplog.at_level(logging.ERROR, logger="xai_client"):
        first = asyncio.run(
            context_for_market(
                market={"id": "A", "event_ticker": "EA"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=300,
                title="Game A",
            )
        )
        second = asyncio.run(
            context_for_market(
                market={"id": "B", "event_ticker": "EB"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=300,
                title="Game B",
            )
        )
    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR and "410" in rec.message]
    assert len(errors) == 1
    assert "HTTP 410" in errors[0].message
    assert calls["n"] == 1
    assert "skipped before the model" in first[1]
    assert "410" in first[1]
    assert "skipped before the model" in second[1]

    async def down(**_kwargs):
        return ("transient", "HTTP 503")

    monkeypatch.setattr("src.clients.xai_client._post_xai_responses", down)
    begin_live_lookup_scan()
    clear_event_search_cache()
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="xai_client"):
        opened = asyncio.run(
            context_for_market(
                market={"id": "C", "event_ticker": "EC"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=300,
                title="Game C",
            )
        )
    assert opened == ("", "")
    assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)


def test_incomplete_or_blank_lookup_falls_through_and_is_counted(monkeypatch, caplog):
    calls = {"n": 0}

    async def fake_post(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                "ok",
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output_text": "",
                    "output": [],
                    "usage": {"input_tokens": 20, "output_tokens": 1500},
                },
            )
        if calls["n"] == 2:
            return ("ok", {"status": "completed", "output_text": "", "output": []})
        if calls["n"] == 3:
            return ("ok", {"status": "completed", "output_text": "no score in prose", "output": []})
        return (
            "ok",
            {
                "status": "completed",
                "output_text": '{"facts":""}',
                "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"facts":""}'}]}],
            },
        )

    monkeypatch.setattr("src.clients.xai_client._post_xai_responses", fake_post)
    begin_live_lookup_scan()
    clear_event_search_cache()
    client = XAIClient(api_key="x", model="grok-4.3")
    with caplog.at_level(logging.WARNING, logger="xai_client"):
        incomplete = asyncio.run(
            context_for_market(
                market={"id": "P1", "event_ticker": "INC"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=400,
                title="Incomplete game",
            )
        )
        shared = asyncio.run(
            context_for_market(
                market={"id": "P2", "event_ticker": "INC"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=400,
                title="Same incomplete game",
            )
        )
        blank = asyncio.run(
            context_for_market(
                market={"id": "P3", "event_ticker": "BLANK"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=400,
                title="Blank game",
            )
        )
        prose = asyncio.run(
            context_for_market(
                market={"id": "P4", "event_ticker": "PROSE"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=400,
                title="Unparseable game",
            )
        )
        explicit = asyncio.run(
            context_for_market(
                market={"id": "P5", "event_ticker": "NONE"},
                fact={"skip": False, "context": "", "reason": ""},
                client=client,
                seconds_until=400,
                title="No data",
            )
        )
    assert incomplete == ("", "")
    assert shared == ("", "")
    assert blank == ("", "")
    assert prose == ("", "")
    assert "skipped before the model" in explicit[1]
    assert calls["n"] == 4
    warnings = [rec.message for rec in caplog.records if rec.levelno == logging.WARNING]
    assert any("incomplete" in msg and "max_output_tokens" in msg for msg in warnings)
    assert any("empty output" in msg for msg in warnings)
    assert any("unparseable" in msg for msg in warnings)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="xai_client"):
        log_live_lookup_summary()
    assert any(
        "facts=0 confirmed-empty=1 transient=3 client-error=0" in rec.message
        for rec in caplog.records
    )
