"""Stop-loss behavior at the configured 0.80 fraction.

0.80 means an 80% drop from entry price to Est. Value, per contract, fees
excluded. These tests do not enable selling and do not change that fraction.
A trigger is not an order and not a guaranteed fill.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.loop import monitor_positions
from src.bot.scan_eligibility import compute_order_search_scan_labels
from src.config import DEFAULT_STOP_LOSS_DRAWDOWN_PCT, Settings
from src.database.models import Base, Position
from src.reconcile.open_positions import (
    display_estimated_price_optional,
    stop_loss_triggered_from_position,
    stop_loss_value_drawdown_triggered,
)
from src.util.datetimes import utc_now

# Kevin's checked fraction. Kept as a literal so a future edit cannot silently
# retarget these cases at a tighter stop.
CONFIGURED_FRACTION = 0.8
ENTRY = 0.50
# (0.50 - est) / 0.50 = 0.80 when est is 0.10
EST_AT_THRESHOLD = 0.10


def _position(**overrides) -> Position:
    fields = dict(
        id="p-stop",
        market_id="KXTEST-STOP",
        market_title="Test contract",
        event_ticker="KXTEST",
        side="YES",
        quantity=4,
        entry_price=ENTRY,
        entry_cost=ENTRY * 4,
        current_price=ENTRY,
        bid_price=ENTRY,
        estimated_price=ENTRY,
        status="open",
        trade_mode="live",
        opened_at=utc_now() - timedelta(hours=2),
        fees_paid=1.25,
        stop_loss_drawdown_pct_at_entry=0.50,
        close_time="2030-01-01T00:00:00Z",
        expected_expiration_time="2030-01-01T00:00:00Z",
    )
    fields.update(overrides)
    return Position(**fields)


def test_code_default_matches_fraction_not_percent_points():
    assert DEFAULT_STOP_LOSS_DRAWDOWN_PCT == 0.80
    fresh = Settings(_env_file=None, stop_loss_drawdown_pct=0.8, stop_loss_selling_enabled=False)
    assert fresh.stop_loss_drawdown_pct == 0.8
    assert fresh.stop_loss_selling_enabled is False
    # A whole number 80 is not read as 80%. The loader clamps it into 0.01–0.90.
    clamped = Settings(_env_file=None, stop_loss_drawdown_pct=80)
    assert clamped.stop_loss_drawdown_pct == 0.90


def test_eighty_percent_boundary_uses_entry_price_and_ignores_fees():
    assert stop_loss_value_drawdown_triggered(
        entry_price_per_contract=ENTRY,
        estimated_price_per_contract=EST_AT_THRESHOLD,
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
    )
    assert not stop_loss_value_drawdown_triggered(
        entry_price_per_contract=ENTRY,
        estimated_price_per_contract=EST_AT_THRESHOLD + 0.001,
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
    )
    assert stop_loss_value_drawdown_triggered(
        entry_price_per_contract=ENTRY,
        estimated_price_per_contract=EST_AT_THRESHOLD - 0.01,
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
    )
    # Fees are outside the formula. 0.11 is only a 78% price drop.
    held = _position(estimated_price=0.11, fees_paid=5.0, entry_cost=3.0)
    assert stop_loss_triggered_from_position(held, stop_loss_drawdown_pct=CONFIGURED_FRACTION) is False
    one_lot = _position(quantity=1, entry_cost=ENTRY, estimated_price=EST_AT_THRESHOLD)
    many = _position(quantity=7, entry_cost=ENTRY * 7, estimated_price=EST_AT_THRESHOLD)
    assert stop_loss_triggered_from_position(one_lot, stop_loss_drawdown_pct=CONFIGURED_FRACTION)
    assert stop_loss_triggered_from_position(many, stop_loss_drawdown_pct=CONFIGURED_FRACTION)


def test_bid_collapse_does_not_replace_est_value():
    held = _position(estimated_price=0.40, bid_price=0.01, current_price=0.01)
    assert display_estimated_price_optional(held) == 0.40
    assert stop_loss_triggered_from_position(held, stop_loss_drawdown_pct=CONFIGURED_FRACTION) is False


def test_missing_or_non_finite_est_does_not_trigger():
    assert stop_loss_value_drawdown_triggered(
        entry_price_per_contract=ENTRY,
        estimated_price_per_contract=None,
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
    ) is False
    assert stop_loss_value_drawdown_triggered(
        entry_price_per_contract=ENTRY,
        estimated_price_per_contract=float("nan"),
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
    ) is False
    past_end = _position(
        kalshi_market_status="closed",
        close_time="2020-01-01T00:00:00Z",
        expected_expiration_time="2020-01-01T00:00:00Z",
        estimated_price=0.01,
        bid_price=0.01,
    )
    assert display_estimated_price_optional(past_end) is None
    assert stop_loss_triggered_from_position(past_end, stop_loss_drawdown_pct=CONFIGURED_FRACTION) is False


def test_stored_entry_snapshot_is_not_the_live_threshold():
    # 80% drop. A stored 95% rule would not fire. The passed 0.80 rule does.
    held = _position(estimated_price=EST_AT_THRESHOLD, stop_loss_drawdown_pct_at_entry=0.95)
    assert stop_loss_triggered_from_position(held, stop_loss_drawdown_pct=CONFIGURED_FRACTION) is True


def test_low_account_balance_is_not_a_stop_loss():
    active, label = compute_order_search_scan_labels(
        "play",
        SimpleNamespace(bot_max_open_positions=30),
        100.0,
        total_portfolio_value_usd=4.0,
        open_position_count=0,
    )
    assert active is False
    assert "under $5" in label


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stops.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session


def _run_monitor(tmp_path, monkeypatch, position, settings, *, allow_exits=True, sell_result=None):
    factory = _session_factory(tmp_path)
    db = factory()
    db.add(position)
    db.commit()
    db.close()

    async def _reconcile(*_a, **_k):
        return (1, 0, 0, 0, 0, 0, 0, 0, 0)

    monkeypatch.setattr(
        "src.reconcile.kalshi_live_sync.reconcile_live_positions_from_kalshi",
        _reconcile,
    )
    monkeypatch.setattr("src.bot.loop.fresh_unresolved_order_hold", lambda *_a, **_k: None)
    monkeypatch.setattr("src.bot.loop.make_order_submit_gate", lambda _payload: (lambda _cid: True))
    monkeypatch.setattr("src.bot.loop.record_order_ack", lambda *_a, **_k: True)
    monkeypatch.setattr("src.bot.loop.record_activity", lambda *_a, **_k: True)

    class Kalshi:
        def __init__(self):
            self.calls = []

        async def place_sell_market(self, market_id, side, quantity, before_submit=None):
            if before_submit is not None and not before_submit("cid-test"):
                return {"error": "intent not saved", "order_not_sent": True, "client_order_id": "cid-test"}
            self.calls.append((market_id, side, quantity))
            return sell_result or {
                "error": "HTTP 400: rejected",
                "status_code": 400,
                "client_order_id": "cid-test",
            }

    kalshi = Kalshi()

    async def _broadcast(_payload):
        return None

    asyncio.run(
        monitor_positions(
            kalshi,
            factory,
            _broadcast,
            settings,
            allow_exits=allow_exits,
            bot_state_label="play" if allow_exits else "stop",
        )
    )
    db = factory()
    stored = db.query(Position).one()
    status = stored.status
    awaiting = bool(stored.awaiting_settlement)
    db.close()
    return kalshi.calls, status, awaiting


def _monitor_settings(*, selling: bool):
    return SimpleNamespace(
        stop_loss_drawdown_pct=CONFIGURED_FRACTION,
        stop_loss_selling_enabled=selling,
        exit_grace_minutes=10.0,
    )


def test_disabled_selling_does_not_send_an_exit_at_the_threshold(tmp_path, monkeypatch):
    calls, status, _awaiting = _run_monitor(
        tmp_path,
        monkeypatch,
        _position(estimated_price=EST_AT_THRESHOLD, bid_price=0.01),
        _monitor_settings(selling=False),
    )
    assert calls == []
    assert status == "open"


@pytest.mark.parametrize(
    ("est", "expect_sell"),
    [
        (EST_AT_THRESHOLD + 0.001, False),
        (EST_AT_THRESHOLD, True),
        (0.05, True),
    ],
)
def test_enabled_selling_follows_the_fraction_and_does_not_promise_a_fill(tmp_path, monkeypatch, est, expect_sell):
    calls, status, _awaiting = _run_monitor(
        tmp_path,
        monkeypatch,
        _position(estimated_price=est, bid_price=est),
        _monitor_settings(selling=True),
    )
    if expect_sell:
        assert calls == [("KXTEST-STOP", "YES", 4)]
        assert status == "open"
    else:
        assert calls == []
        assert status == "open"


def test_stop_inside_grace_or_bot_stop_does_not_send(tmp_path, monkeypatch):
    fresh = _position(estimated_price=0.05, opened_at=utc_now())
    calls, status, _awaiting = _run_monitor(
        tmp_path,
        monkeypatch,
        fresh,
        _monitor_settings(selling=True),
    )
    assert calls == []
    assert status == "open"

    other = tmp_path / "bot-stopped"
    other.mkdir()
    calls, status, _awaiting = _run_monitor(
        other,
        monkeypatch,
        _position(id="p-stop-2", estimated_price=0.05),
        _monitor_settings(selling=True),
        allow_exits=False,
    )
    assert calls == []
    assert status == "open"


def test_dead_book_records_intent_but_does_not_close(tmp_path, monkeypatch):
    calls, status, awaiting = _run_monitor(
        tmp_path,
        monkeypatch,
        _position(estimated_price=0.05),
        _monitor_settings(selling=True),
        sell_result={"skipped_dead_book": True, "status": "skipped_dead_book"},
    )
    assert calls == [("KXTEST-STOP", "YES", 4)]
    assert status == "open"
    assert awaiting is True
