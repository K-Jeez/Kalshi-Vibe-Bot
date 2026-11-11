"""Series fees are loaded before the pre-model cash-cap gate."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.loop import (
    _note_premodel_skip,
    _premodel_skip_logged_at,
    attach_series_fee_schedules,
    scan_and_trade,
    series_ticker_for_market,
)
from src.clients.kalshi_client import KalshiClient
from src.database.models import Base, BotState
from src.decision_engine.strategy_gates import (
    MAX_KELLY_BANKROLL_PCT,
    all_in_entry_dollars,
    either_side_can_buy_one_capped_contract,
    premodel_entry_block,
)

EXAMPLE = "KXATPEXACTMATCH-26NOV11KECRUN-RUN20"
SERIES = "KXATPEXACTMATCH"


def _quadratic_market(**extra) -> dict:
    market = {
        "id": EXAMPLE,
        "series_ticker": SERIES,
        "yes_ask": 0.29,
        "no_ask": 0.90,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    market.update(extra)
    return market


def test_gate_passes_typical_asks_when_the_quadratic_schedule_is_known():
    balance = 100.60
    cap = balance * MAX_KELLY_BANKROLL_PCT
    assert cap == 2.515
    market = _quadratic_market()
    for ask in (0.29, 0.90):
        cost = all_in_entry_dollars(1, ask, fee_multiplier=1, fee_type="quadratic")
        assert cost is not None and cost < cap
    assert either_side_can_buy_one_capped_contract(market, balance)
    assert premodel_entry_block(market, balance) is None


def test_unknown_schedule_fails_closed_with_its_own_message():
    bare = _quadratic_market()
    bare.pop("fee_type")
    bare.pop("fee_multiplier")
    block = premodel_entry_block(bare, 100.60)
    assert block["kind"] == "fee_unknown"
    assert block["message"] == f"fee schedule unknown for series {SERIES} — skipping"
    assert "cash cap" not in block["message"]
    flat = _quadratic_market(fee_type="flat")
    flat_block = premodel_entry_block(flat, 100.60)
    assert flat_block["kind"] == "fee_unknown"
    assert "cash cap" not in flat_block["message"]


def test_cash_cap_skip_names_asks_fee_all_in_and_cap():
    market = _quadratic_market(id="KXSMALL", series_ticker="KXSMALL", yes_ask=0.90, no_ask=0.80)
    block = premodel_entry_block(market, 10)
    assert block["kind"] == "cash_cap"
    text = block["message"]
    assert "KXSMALL" in text
    assert "0.90" in text and "0.80" in text
    assert "estimated fee 0.01" in text
    assert "estimated fee 0.02" in text
    assert "all-in 0.91" in text
    assert "all-in 0.82" in text
    assert "$0.25" in text


def test_same_skip_is_not_logged_on_every_scan(caplog):
    _premodel_skip_logged_at.clear()
    counts: dict = {}
    block = {
        "kind": "fee_unknown",
        "series": SERIES,
        "ticker": EXAMPLE,
        "message": f"fee schedule unknown for series {SERIES} — skipping",
    }
    with caplog.at_level(logging.INFO):
        _note_premodel_skip(counts, block)
        _note_premodel_skip(counts, block)
    assert counts["fee_unknown"] == 2
    assert caplog.text.count(f"fee schedule unknown for series {SERIES}") == 1
    _premodel_skip_logged_at.clear()


def _client():
    client = KalshiClient.__new__(KalshiClient)
    client._series_fee_cache = {}
    client._series_fee_locks = {}
    return client


def test_series_fee_cache_serves_a_failed_read_and_a_known_schedule():
    class Response:
        status_code = 200

        def json(self):
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}

    calls = []

    async def scenario():
        failing = _client()

        async def boom(_path, timeout=10.0):
            calls.append("fail")
            raise RuntimeError("down")

        failing._http_get_signed = boom
        first = await failing.get_series_fee(SERIES)
        second = await failing.get_series_fee(SERIES)
        assert first == {"known": False, "reason": "series fee read failed"}
        assert second == first
        assert calls == ["fail"]
        failing._series_fee_cache[SERIES] = (time.monotonic() - 3601, dict(first))
        await failing.get_series_fee(SERIES)
        assert calls == ["fail", "fail"]

        loaded = _client()

        async def ok(path, timeout=10.0):
            calls.append(path)
            return Response()

        loaded._http_get_signed = ok
        markets = [
            {"id": EXAMPLE, "series_ticker": SERIES, "yes_ask": 0.29, "no_ask": 0.90},
            {
                "id": "KXATPEXACTMATCH-26NOV11KECRUN-RUN21",
                "event_ticker": "KXATPEXACTMATCH-26NOV11KECRUN",
                "yes_ask": 0.40,
                "no_ask": 0.62,
            },
        ]
        assert series_ticker_for_market(markets[1]) == SERIES
        before = len(calls)
        await attach_series_fee_schedules(loaded, markets)
        await attach_series_fee_schedules(loaded, markets)
        assert calls[before:] == [f"/trade-api/v2/series/{SERIES}"]
        assert markets[0]["fee_multiplier"] == 1
        assert markets[1]["fee_type"] == "quadratic"
        assert either_side_can_buy_one_capped_contract(markets[0], 100.60)

    asyncio.run(scenario())


def test_scan_stays_up_when_the_series_fee_read_fails(tmp_path, monkeypatch, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(BotState(id=1, state="play"))
    db.commit()
    db.close()
    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)

    async def _none():
        return None

    monkeypatch.setattr("src.api.portfolio.get_xai_prepaid_balance_usd_cached", _none)
    _premodel_skip_logged_at.clear()
    analyzed = []

    close = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    market = {
        "id": EXAMPLE,
        "title": "Exact match",
        "event_ticker": "KXATPEXACTMATCH-26NOV11KECRUN",
        "series_ticker": SERIES,
        "market_type": "binary",
        "status": "open",
        "volume": 5000.0,
        "yes_price": 0.28,
        "no_price": 0.72,
        "yes_bid": 0.27,
        "yes_ask": 0.29,
        "yes_spread": 0.02,
        "yes_ask_size": 50.0,
        "no_bid": 0.69,
        "no_ask": 0.71,
        "no_spread": 0.02,
        "no_ask_size": 50.0,
        "close_time": close,
        "vetting_horizon_time": close,
        "expected_expiration_time": close,
    }

    class Kalshi:
        async def get_portfolio(self):
            return {"cash": 100.60, "portfolio_value": 100.60}

        async def list_orders(self, **_kwargs):
            return []

        async def get_markets(self, _filters=None):
            return [dict(market)]

        async def get_series_fee(self, _series):
            raise RuntimeError("series down")

    class Engine:
        async def analyze_market(self, **_kwargs):
            analyzed.append("market")
            return {"decision": "SKIP"}

        async def analyze_event_batch(self, **_kwargs):
            analyzed.append("batch")
            return {"decision": "SKIP"}

    settings = SimpleNamespace(
        bot_max_hours=4,
        bot_markets_fetch_max_close_hours=6,
        bot_min_volume=1.0,
        bot_max_spread=0.15,
        bot_min_top_size=1.0,
        local_min_residual_payoff=0.0,
        bot_max_open_positions=12,
        reentry_cooldown_minutes=0,
        bot_scan_interval=10,
        min_edge_to_buy_pct=5,
        min_ai_win_prob_buy_side_pct=60,
        stop_loss_drawdown_pct=0.80,
        stop_loss_selling_enabled=False,
    )

    async def _broadcast(_payload):
        return None

    with caplog.at_level(logging.INFO):
        asyncio.run(scan_and_trade(Kalshi(), Engine(), factory, _broadcast, settings))
    assert analyzed == []
    assert "Scan sweep finished" in caplog.text
    assert f"fee schedule unknown for series {SERIES} — skipping" in caplog.text
    assert "under the existing cash cap" not in caplog.text
    assert "fee_schedule_unknown=" in caplog.text
