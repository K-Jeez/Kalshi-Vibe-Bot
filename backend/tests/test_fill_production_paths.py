"""Import and run the live fill paths. No Kalshi network and no server."""

from __future__ import annotations

import ast
import asyncio
import importlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.fill_bookkeeping import normalize_order_side
from src.database.models import Base, Position, Trade
from src.util.datetimes import utc_now

_SRC = Path(__file__).resolve().parents[1] / "src"
_BOT = _SRC / "bot"


def test_production_modules_import_the_real_fill_book():
    files = (
        _BOT / "loop.py",
        _BOT / "order_reconcile.py",
        _BOT / "fill_bookkeeping.py",
        _SRC / "fixed_point.py",
    )
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"))
    sources = {
        "loop": (_BOT / "loop.py").read_text(encoding="utf-8"),
        "reconcile": (_BOT / "order_reconcile.py").read_text(encoding="utf-8"),
    }
    for label, text in sources.items():
        assert "fills_bookkeeping" not in text, label
        assert "src.bot.fill_bookkeeping" in text, label
    for module_name in (
        "src.bot.fill_bookkeeping",
        "src.bot.order_reconcile",
        "src.bot.loop",
        "src.fixed_point",
    ):
        importlib.import_module(module_name)
    loop = importlib.import_module("src.bot.loop")
    book = importlib.import_module("src.bot.fill_bookkeeping")
    assert loop.apply_cumulative_fill is book.apply_cumulative_fill
    assert loop.ensure_fill_trade is book.ensure_fill_trade


def test_quote_side_is_not_treated_as_the_contract_side():
    assert normalize_order_side("yes", {"side": "ask", "book_side": "ask"}) == "YES"
    assert normalize_order_side("NO", {"outcome_side": "no"}) == "NO"
    try:
        normalize_order_side(None, {"side": "ask", "book_side": "bid"})
    except ValueError as exc:
        assert "yes or no" in str(exc)
    else:
        raise AssertionError("ask was accepted as a contract side")
    try:
        normalize_order_side("YES", {"outcome_side": "NO"})
    except ValueError as exc:
        assert "conflict" in str(exc)
    else:
        raise AssertionError("conflicting sides were accepted")


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fills.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_monitor_sell_fallback_books_one_trade(tmp_path, monkeypatch):
    from src.bot.loop import monitor_positions

    factory = _factory(tmp_path)
    db = factory()
    db.add(
        Position(
            id="p-sell",
            market_id="KXTEST-SELL",
            market_title="Sell contract",
            event_ticker="KXTEST",
            side="YES",
            quantity=4,
            entry_price=0.50,
            entry_cost=2.0,
            current_price=0.50,
            estimated_price=0.10,
            bid_price=0.10,
            status="open",
            trade_mode="live",
            opened_at=utc_now() - timedelta(hours=2),
            fees_paid=0.04,
        )
    )
    db.commit()
    db.close()

    async def _no_portfolio_sync(*_a, **_k):
        return (0, 0, 0, 0, 0, 0, 0, 0, 0)

    async def _sync_fails(*_a, **_k):
        return False

    monkeypatch.setattr(
        "src.reconcile.kalshi_live_sync.reconcile_live_positions_from_kalshi",
        _no_portfolio_sync,
    )
    monkeypatch.setattr("src.bot.loop.sync_open_position_qty_cost_from_kalshi", _sync_fails)
    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)

    class Kalshi:
        def __init__(self):
            self.sells = 0

        async def place_sell_market(self, market_id, side, quantity, before_submit=None):
            self.sells += 1
            cid = "cid-sell-1"
            if before_submit is not None:
                before_submit(cid)
            return {
                "order_id": "ord-sell-1",
                "client_order_id": cid,
                "status": "executed",
                "side": "yes",
                "fill_count_fp": "4.00",
                "remaining_count_fp": "0.00",
                "yes_price_dollars": "0.1000",
                "taker_fill_cost_dollars": "0.4000",
                "taker_fees_dollars": "0.0400",
            }

        async def refresh_order_fill_snapshot(self, order):
            return order

    kalshi = Kalshi()
    settings = SimpleNamespace(
        stop_loss_drawdown_pct=0.80,
        stop_loss_selling_enabled=True,
        exit_grace_minutes=0.0,
    )

    async def _broadcast(_payload):
        return None

    asyncio.run(monitor_positions(kalshi, factory, _broadcast, settings, allow_exits=True, bot_state_label="play"))
    asyncio.run(monitor_positions(kalshi, factory, _broadcast, settings, allow_exits=True, bot_state_label="play"))
    db = factory()
    pos = db.query(Position).one()
    trades = db.query(Trade).all()
    db.close()
    assert kalshi.sells == 1
    assert pos.status == "closed"
    assert pos.exit_reason == "stop_loss"
    assert len(trades) == 1
    assert trades[0].id == "ord-sell-1:sell:4"
    assert trades[0].action == "sell"
    assert trades[0].quantity == 4


def test_scan_buy_fill_uses_the_shared_book(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from src.bot.loop import scan_and_trade
    from src.database.models import BotState

    factory = _factory(tmp_path)
    db = factory()
    db.add(BotState(id=1, state="play"))
    db.commit()
    db.close()
    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)

    async def _prepaid():
        return 10.0

    monkeypatch.setattr("src.api.portfolio.get_xai_prepaid_balance_usd_cached", _prepaid)
    close = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    market = {
        "id": "KXTEST-BUY",
        "title": "Buy contract",
        "event_ticker": "KXTEST",
        "series_ticker": "KXTEST",
        "market_type": "binary",
        "status": "open",
        "volume": 5000,
        "yes_price": 0.55,
        "no_price": 0.45,
        "yes_bid": 0.54,
        "yes_ask": 0.55,
        "no_bid": 0.44,
        "no_ask": 0.45,
        "yes_spread": 0.01,
        "no_spread": 0.01,
        "yes_ask_size": 20,
        "no_ask_size": 20,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "close_time": close,
        "expected_expiration_time": close,
        "vetting_horizon_time": close,
    }

    class Kalshi:
        async def get_portfolio(self):
            return {"cash": 100.0, "portfolio_value": 100.0}

        async def list_orders(self, **_kwargs):
            return []

        async def get_markets(self, _filters=None):
            return [dict(market)]

        async def get_series_fee(self, _ticker):
            return {"known": True, "fee_type": "quadratic", "fee_multiplier": 1}

        async def place_buy_ioc_limit(self, ticker, side, quantity, limit_price, client_order_id=None, exchange_index=None):
            return {
                "order_id": "ord-buy-1",
                "client_order_id": client_order_id,
                "status": "executed",
                "side": side,
                "fill_count_fp": f"{int(quantity)}.00",
                "remaining_count_fp": "0.00",
                "yes_price_dollars": f"{float(limit_price):.4f}",
                "taker_fill_cost_dollars": f"{float(limit_price) * int(quantity):.4f}",
                "taker_fees_dollars": "0.0200",
            }

    class Engine:
        async def analyze_market(self, **_kwargs):
            return {
                "decision": "BUY_YES",
                "direction": "YES",
                "ai_probability_yes_pct": 70,
                "market_implied_probability_pct": 55,
                "edge_pct": 15.0,
                "kelly_contracts": 1,
                "kelly_fraction": 0.2,
                "confidence": 0.8,
                "yes_confidence": 80,
                "no_confidence": 20,
                "reasoning": "test",
                "real_time_context": "",
                "key_factors": [],
                "evidence": [],
                "xai_analysis": {"provider": "xai", "model": "test", "direction": "YES", "ai_probability_yes_pct": 70},
                "action_summary": "buy",
            }

        async def analyze_event_batch(self, **_kwargs):
            raise AssertionError("single market should not batch")

    settings = SimpleNamespace(
        bot_max_hours=24,
        bot_markets_fetch_max_close_hours=24,
        bot_min_volume=1.0,
        bot_max_spread=0.15,
        bot_min_top_size=1.0,
        local_min_residual_payoff=0.0,
        bot_max_open_positions=12,
        min_edge_to_buy_pct=5,
        min_ai_win_prob_buy_side_pct=60,
        stop_loss_drawdown_pct=0.80,
        stop_loss_selling_enabled=False,
        xai_model="test",
        bot_loop_scan_timeout_sec=30,
    )

    async def _broadcast(_payload):
        return None

    asyncio.run(scan_and_trade(Kalshi(), Engine(), factory, _broadcast, settings))
    db = factory()
    positions = db.query(Position).filter(Position.status == "open").all()
    trades = db.query(Trade).filter(Trade.action == "buy").all()
    db.close()
    assert len(positions) == 1
    assert positions[0].side == "YES"
    assert positions[0].quantity >= 1
    assert float(positions[0].fees_paid) > 0
    assert len(trades) == 1
