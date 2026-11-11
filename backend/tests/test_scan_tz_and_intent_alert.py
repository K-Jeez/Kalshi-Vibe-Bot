"""Naive SQLite close times in the scan, and current versus historical order alerts."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.activity_log import persist_order_intent, record_order_ack
from src.api.health import public_health_body
from src.bot.loop import losing_exit_cooldown_market_ids, scan_and_trade
from src.database.models import ActivityEvent, Base, BotState, Position
from src.util.datetimes import utc_now


async def _none():
    return None


def test_cooldown_accepts_the_naive_sqlite_close_from_the_failed_scan():
    # 7:19 PM MST on Nov 8 2026 is 02:19 UTC Nov 9. The stored close has no offset.
    now = datetime(2026, 11, 9, 2, 19, tzinfo=timezone.utc)
    naive_close = datetime(2026, 11, 9, 0, 32, 8, 595521)
    tickers = losing_exit_cooldown_market_ids(
        [("KXTEST-CLOSE", naive_close, -1.5), ("KXTEST-WIN", naive_close, 2.0)],
        now=now,
        cooldown_minutes=120,
    )
    assert tickers == {"KXTEST-CLOSE"}
    older = datetime(2026, 11, 8, 0, 0, 0)
    assert losing_exit_cooldown_market_ids(
        [("KXTEST-CLOSE", older, -1.0)],
        now=now,
        cooldown_minutes=120,
    ) == set()
    # A later aware winning close clears an earlier naive loss. A later naive loss wins over an earlier aware win.
    aware_win = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)
    naive_loss = datetime(2026, 11, 9, 1, 30, 0)
    assert losing_exit_cooldown_market_ids(
        [("KXTEST-MIX", naive_close, -1.0), ("KXTEST-MIX", aware_win, 3.0)],
        now=now,
        cooldown_minutes=120,
    ) == set()
    assert losing_exit_cooldown_market_ids(
        [("KXTEST-MIX", aware_win, 3.0), ("KXTEST-MIX", naive_loss, -0.4)],
        now=now,
        cooldown_minutes=120,
    ) == {"KXTEST-MIX"}


def test_scan_reaches_the_sweep_when_sqlite_closed_at_is_naive(tmp_path, monkeypatch, caplog):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(BotState(id=1, state="play"))
    db.add(
        Position(
            id="closed-1",
            market_id="KXTEST-CLOSE",
            market_title="Closed",
            side="YES",
            quantity=1,
            entry_price=0.4,
            entry_cost=0.4,
            current_price=0.2,
            status="closed",
            trade_mode="live",
            realized_pnl=-1.0,
            closed_at=utc_now() - timedelta(minutes=30),
        )
    )
    db.commit()
    db.close()
    # SQLite hands this column back without a timezone. Force that shape.
    raw = sqlite3.connect(tmp_path / "scan.db")
    raw.execute("UPDATE positions SET closed_at = ?", ("2026-11-09 00:32:08.595521",))
    raw.commit()
    stored = raw.execute("SELECT closed_at FROM positions").fetchone()[0]
    raw.close()
    assert "+" not in stored and "Z" not in stored

    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)
    monkeypatch.setattr("src.api.portfolio.get_xai_prepaid_balance_usd_cached", lambda: _none())

    seen = {}

    def _spy(rows, *, now, cooldown_minutes):
        seen["rows"] = list(rows)
        seen["now"] = now
        return losing_exit_cooldown_market_ids(rows, now=now, cooldown_minutes=cooldown_minutes)

    monkeypatch.setattr("src.bot.loop.losing_exit_cooldown_market_ids", _spy)

    class Kalshi:
        async def get_portfolio(self):
            return {"cash": 100.0, "portfolio_value": 100.0}

        async def list_orders(self, **_kwargs):
            return []

        async def get_markets(self, _filters=None):
            return []

    settings = SimpleNamespace(
        bot_max_hours=4,
        bot_markets_fetch_max_close_hours=6,
        bot_min_volume=1.0,
        bot_max_spread=0.15,
        bot_min_top_size=1.0,
        local_min_residual_payoff=0.0,
        bot_max_open_positions=12,
        reentry_cooldown_minutes=100000,
        bot_scan_interval=10,
        min_edge_to_buy_pct=5,
        min_ai_win_prob_buy_side_pct=60,
        stop_loss_drawdown_pct=0.80,
        stop_loss_selling_enabled=False,
    )

    async def _broadcast(_payload):
        return None

    with caplog.at_level("INFO"):
        asyncio.run(scan_and_trade(Kalshi(), object(), factory, _broadcast, settings))
    assert "can't compare offset-naive and offset-aware datetimes" not in caplog.text
    assert "Scan sweep:" in caplog.text
    assert seen["rows"], "scan did not load the naive close into the cooldown comparison"
    closed_at = seen["rows"][0][1]
    assert closed_at.tzinfo is None
    assert seen["now"].tzinfo is not None
    assert "KXTEST-CLOSE" in {str(row[0]) for row in seen["rows"]}


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_health_alert_uses_latest_status_not_every_pending_row(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    monkeypatch.setattr("src.activity_log.get_session_local", lambda: factory)
    filled = persist_order_intent(
        {"market_id": "M1", "action": "buy", "side": "YES", "quantity": 1, "limit_price": 0.4},
        session_factory=factory,
    )
    record_order_ack(
        {"client_order_id": filled, "order_id": "ord-1", "taker_fees_dollars": 0.0, "maker_fees_dollars": 0.0},
        {"market_id": "M1", "action": "buy", "side": "YES", "client_order_id": filled},
        session_factory=factory,
    )
    db = factory()
    ack = (
        db.query(ActivityEvent)
        .filter(ActivityEvent.client_order_id == filled, ActivityEvent.event_type == "order_ack")
        .one()
    )
    ack.order_status = "filled"
    db.commit()
    reconciled = persist_order_intent(
        {"market_id": "M2", "action": "sell", "side": "NO", "quantity": 1, "limit_price": 0.3},
        session_factory=factory,
    )
    db = factory()
    db.add(
        ActivityEvent(
            id="ack-reconciled",
            seq=50,
            ts_utc=utc_now(),
            run_id="run",
            event_type="order_ack",
            market_id="M2",
            action="sell",
            client_order_id=reconciled,
            order_status="reconciled_filled",
        )
    )
    db.add(
        ActivityEvent(
            id="still-ambiguous",
            seq=1,
            ts_utc=utc_now() - timedelta(minutes=5),
            run_id="run",
            event_type="order_intent",
            market_id="M3",
            action="buy",
            client_order_id="cid-amb",
            order_status="pending",
        )
    )
    db.add(
        ActivityEvent(
            id="still-ambiguous-late",
            seq=2,
            ts_utc=utc_now() - timedelta(minutes=5),
            run_id="run",
            event_type="order_ack",
            market_id="M3",
            action="buy",
            client_order_id="cid-amb",
            order_status="ambiguous",
        )
    )
    db.add(
        ActivityEvent(
            id="open-at-exchange",
            seq=3,
            ts_utc=utc_now(),
            run_id="run",
            event_type="order_ack",
            market_id="M4",
            action="buy",
            client_order_id="cid-open",
            order_status="open_at_exchange",
        )
    )
    same_ts = utc_now()
    db.add(
        ActivityEvent(
            id="tie-pending",
            seq=10,
            ts_utc=same_ts,
            run_id="run",
            event_type="order_intent",
            market_id="M6",
            action="buy",
            client_order_id="cid-tie",
            order_status="pending",
        )
    )
    db.add(
        ActivityEvent(
            id="tie-filled",
            seq=11,
            ts_utc=same_ts,
            run_id="run",
            event_type="order_ack",
            market_id="M6",
            action="buy",
            client_order_id="cid-tie",
            order_status="filled",
        )
    )
    db.add(
        ActivityEvent(
            id="legacy",
            seq=4,
            ts_utc=utc_now(),
            run_id="run",
            event_type="order_intent",
            market_id="M5",
            action="buy",
            client_order_id=None,
            order_status="pending",
        )
    )
    db.commit()
    db.close()
    body = public_health_body()
    assert body["current_unresolved_intent_count"] == 2
    assert body["historical_pending_row_count"] >= 4
    assert body["legacy_unresolved_without_client_order_id"] == 1
    text = " ".join(body["alerts"])
    assert "still pending" not in text
    assert "2 orders are still unconfirmed" in text
    assert "1 legacy activity row" in text
    assert "unconfirmed order intent" not in text


def test_health_fails_closed_when_the_intent_log_cannot_be_read(monkeypatch):
    def boom():
        raise RuntimeError("db")

    monkeypatch.setattr("src.activity_log.get_session_local", boom)
    body = public_health_body()
    assert "Order intent log could not be read." in body["alerts"]
    assert body["current_unresolved_intent_count"] is None
    assert "still pending" not in " ".join(body["alerts"])
