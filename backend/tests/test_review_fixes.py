"""Review fixes: fill cashflows, blend gate, cooldown, residue, and caps."""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from src.bot.reanalysis import should_skip_unchanged_quote
from src.bot.exit_backoff import clear_exit_backoff, exit_is_backed_off, note_empty_exit, reset_for_tests
from src.config import Settings
from src.decision_engine.blend import quantity_after_blend
from src.decision_engine.strategy_gates import premodel_entry_block
from src.ops_status import alerts, clear_unresolved, note_unresolved, reset_for_tests as reset_ops
from src.reconcile.realized_cashflow import fill_yes_price, fills_to_cashflows
from src.shadow_buys import rotate_ids
from tools.repair_realized_pnl import plan_repairs, running_block_reason

FIXTURES = Path(__file__).parent / "fixtures"


def _position(ticker, side, qty, reason="settlement"):
    return {
        "id": ticker,
        "market_id": ticker,
        "side": side,
        "status": "closed",
        "quantity": qty,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "exit_reason": reason,
    }


def test_synthetic_fills_classify_yes_wins_no_entries_and_stops():
    blob = json.loads((FIXTURES / "fills_mapped.json").read_text(encoding="utf-8"))
    positions = [
        _position("MKT-YES-WIN", "YES", 2),
        _position("MKT-NO-LOSS", "NO", 2),
        _position("MKT-YES-STOP", "YES", 2, "stop_loss"),
        _position("MKT-PARTIAL", "YES", 4, "stop_loss"),
    ]
    plans = plan_repairs(positions, blob["fills"], blob["settlements"])
    by_ticker = {plan["ticker"]: plan for plan in plans}
    # 2 contracts bought at 0.40, fee 0.04, settle YES: 2 - 0.80 - 0.04
    assert abs(by_ticker["MKT-YES-WIN"]["new_realized_pnl"] - 1.16) < 1e-9
    # NO entry is sell/no at no price 0.60. Result YES is a loss. Not a stop.
    assert by_ticker["MKT-NO-LOSS"]["new_exit_reason"] == "settlement"
    assert by_ticker["MKT-NO-LOSS"]["new_realized_pnl"] < 0
    # Full stop: sale 0.40 minus cost 1.00 minus fees 0.03
    assert abs(by_ticker["MKT-YES-STOP"]["new_realized_pnl"] - (0.40 - 1.00 - 0.03)) < 1e-9
    # Partial sale 0.50 plus $1 on each of the 2 left, minus the $2 cost.
    assert abs(by_ticker["MKT-PARTIAL"]["new_realized_pnl"] - 0.50) < 1e-9


def test_api_field_names_are_not_read_as_zero():
    raw = {
        "ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count_fp": "2.00",
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "fee_cost": "0.0400",
    }
    assert fill_yes_price(raw) == 0.4
    cash = fills_to_cashflows([raw], {"market_result": "yes", "side": "YES"}, held_side="YES")
    assert abs(cash["realized_pnl"] - (2.0 - 0.8 - 0.04)) < 1e-9


def test_two_positions_on_one_ticker_are_not_both_given_the_full_pnl():
    fills = [
        {"ticker": "KX", "action": "buy", "side": "yes", "count_fp": "4", "yes_price": "0.50", "fee": "0.08"},
    ]
    positions = [
        {"id": "a", "market_id": "KX", "side": "YES", "status": "closed", "quantity": 1, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
        {"id": "b", "market_id": "KX", "side": "YES", "status": "closed", "quantity": 3, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
    ]
    plans = plan_repairs(positions, fills, [{"ticker": "KX", "market_result": "yes"}])
    assert abs(sum(plan["new_realized_pnl"] for plan in plans) - (4 - 2 - 0.08)) < 1e-9
    assert plans[0]["new_realized_pnl"] != plans[1]["new_realized_pnl"]


def test_blend_gate_off_floors_at_one_and_on_can_skip():
    assert Settings().vibe_blend_ev_gate is False
    floor, tag = quantity_after_blend(
        blend_ev_gate=False,
        kelly_quantity=0,
        blended_ev=-0.05,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (floor, tag) == (1, "blend_floor")
    blocked, blocked_tag = quantity_after_blend(
        blend_ev_gate=True,
        kelly_quantity=3,
        blended_ev=-0.05,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (blocked, blocked_tag) == (0, "blend_ev")
    kept, kept_tag = quantity_after_blend(
        blend_ev_gate=False,
        kelly_quantity=4,
        blended_ev=-0.02,
        cap_contracts=2,
        one_contract_fits_cash=True,
    )
    assert (kept, kept_tag) == (2, "quarter_kelly")


def test_cooldown_skips_even_when_price_moved_and_a_quiet_market_stays_quiet():
    assert should_skip_unchanged_quote(
        analyzed_recently=True,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.55,
        no_price=0.45,
    )
    assert should_skip_unchanged_quote(
        analyzed_recently=False,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.404,
        no_price=0.596,
    )
    assert not should_skip_unchanged_quote(
        analyzed_recently=False,
        previous_yes=0.40,
        previous_no=0.60,
        yes_price=0.42,
        no_price=0.58,
    )


def test_cash_below_one_contract_skips_the_model():
    market = {
        "id": "KX",
        "series_ticker": "KX",
        "yes_ask": 0.80,
        "no_ask": 0.80,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    block = premodel_entry_block(market, 0.50)
    assert block is not None
    assert block["kind"] == "cash_short"
    assert "cannot buy one contract" in block["message"]


def test_unresolved_alert_clears_when_nothing_is_pending():
    reset_ops()
    note_unresolved("Unconfirmed buy KX stays reserved.")
    assert any("reserved" in line for line in alerts())
    clear_unresolved()
    assert not any("reserved" in line for line in alerts())


def test_shadow_rotation_does_not_starve_later_tickers():
    first, cursor = rotate_ids(["a", "b", "c", "d"], "", 2)
    assert first == ["a", "b"]
    second, _cursor = rotate_ids(["a", "b", "c", "d"], cursor, 2)
    assert second == ["c", "d"]


def test_exit_backoff_clears_when_asked():
    reset_for_tests()
    note_empty_exit("KX", now=0)
    assert exit_is_backed_off("KX", now=10)
    clear_exit_backoff("KX")
    assert not exit_is_backed_off("KX", now=10)


def test_fractional_residue_is_only_the_part_past_the_whole_contracts():
    filled = Decimal("5.01")
    whole = int(filled.to_integral_value(rounding=ROUND_DOWN))
    assert whole == 5
    assert filled - whole == Decimal("0.01")


def test_missing_settlement_is_skipped_instead_of_booked_as_a_loss():
    plans = plan_repairs(
        [_position("MKT-OPEN", "YES", 2)],
        [{"ticker": "MKT-OPEN", "action": "buy", "side": "yes", "count_fp": "2", "yes_price": "0.40", "fee": "0.04"}],
        [],
    )
    assert plans[0]["write"] is False
    assert plans[0]["new_realized_pnl"] == ""
    assert "no settlement" in plans[0]["note"]


def test_selling_half_or_more_of_four_still_settles_the_remainder():
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.database.models import Base, Position, Trade
    from src.reconcile.kalshi_closed_position_finalize import finalize_live_closed_positions_from_kalshi
    from src.reconcile.realized_cashflow import sale_split
    from src.util.datetimes import utc_now

    for sold in (1, 2, 3):
        assert sale_split(position_qty=4 - sold, sold_qty=sold, side="YES")["kind"] == "partial"
    assert sale_split(
        position_qty=4,
        sold_qty=4,
        settlement={"yes_count_fp": "4.00", "no_count_fp": "4.00"},
        side="YES",
    )["kind"] == "full"

    class _Noop:
        async def get_order(self, _oid):
            return {}

    for sold in (1, 2, 3):
        for result, won in (("yes", True), ("no", False)):
            rem = 4 - sold
            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            db = sessionmaker(bind=engine)()
            now = utc_now()
            sale_pnl = sold * 0.25 - sold * 0.50
            db.add(
                Position(
                    id=f"p-{sold}-{result}",
                    market_id="MKT",
                    market_title="t",
                    side="YES",
                    quantity=rem,
                    entry_price=0.50,
                    entry_cost=rem * 0.50,
                    current_price=0.25,
                    status="closed",
                    trade_mode="live",
                    closed_at=now,
                    exit_reason="stop_loss",
                    realized_pnl=0.0,
                    fees_paid=0.0,
                    kalshi_closure_finalized=False,
                )
            )
            db.add(
                Trade(
                    id=f"sell-{sold}-{result}",
                    market_id="MKT",
                    market_title="t",
                    action="sell",
                    side="YES",
                    quantity=sold,
                    price=0.25,
                    total_cost=sold * 0.25,
                    realized_pnl=sale_pnl,
                    trade_mode="live",
                    timestamp=now,
                )
            )
            db.commit()
            settlement = {
                "ticker": "MKT",
                "market_result": result,
                "yes_count_fp": f"{rem:.2f}",
                "no_count_fp": "0.00",
                "revenue": rem * 100 if won else 0,
                "fee_cost": "0",
            }
            asyncio.run(
                finalize_live_closed_positions_from_kalshi(
                    db,
                    trade_mode="live",
                    kalshi_client=_Noop(),
                    settlement_rows=[settlement],
                )
            )
            db.commit()
            pos = db.query(Position).one()
            addon = (rem * 1.0 if won else 0.0) - rem * 0.50
            assert abs(float(pos.realized_pnl) - (sale_pnl + addon)) < 1e-6


def test_void_settlement_uses_revenue_instead_of_zero():
    cash = fills_to_cashflows(
        [{"action": "buy", "side": "yes", "count_fp": "2", "yes_price": "0.40", "fee": "0"}],
        {"market_result": "void", "revenue": 150, "side": "YES"},
        held_side="YES",
    )
    assert abs(cash["realized_pnl"] - (1.50 - 0.80)) < 1e-9


def test_incomplete_settlement_read_fails_instead_of_an_empty_list():
    import asyncio

    from tools.repair_realized_pnl import fetch_fills_and_settlements

    class Client:
        async def _reconcile_pages(self, *_args, **_kwargs):
            return {"state": "complete", "rows": []}

        async def get_settlements_complete(self):
            raise RuntimeError("settlements pagination incomplete")

    try:
        asyncio.run(fetch_fills_and_settlements(Client()))
    except RuntimeError as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("incomplete settlements read was swallowed")


def test_apply_uses_the_configured_port(monkeypatch):
    from src.config import settings
    from tools.repair_realized_pnl import repair_listen_port

    monkeypatch.setattr(settings, "port", 8123)
    assert repair_listen_port() == 8123


def test_same_exchange_order_is_not_booked_twice():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.bot.fill_bookkeeping import apply_cumulative_fill
    from src.database.models import Base, Position

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    order = {"order_id": "exch-1", "side": "yes", "action": "buy", "fill_count_fp": "3.00", "yes_price_dollars": "0.40"}
    first = apply_cumulative_fill(
        db,
        action="buy",
        side="YES",
        market_id="MKT",
        cumulative_qty=3,
        cumulative_fees=0.02,
        price=0.40,
        client_order_id="intent-a",
        exchange_order_id="exch-1",
        order=order,
    )
    db.commit()
    second = apply_cumulative_fill(
        db,
        action="buy",
        side="YES",
        market_id="MKT",
        cumulative_qty=3,
        cumulative_fees=0.02,
        price=0.40,
        client_order_id="intent-b",
        exchange_order_id="exch-1",
        order=order,
    )
    db.commit()
    assert first["ok"] is True and first.get("replay") is not True
    assert second.get("replay") is True
    assert int(db.query(Position).one().quantity) == 3


def test_apply_is_refused_while_a_port_is_open():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        reason = running_block_reason(Path("/tmp/does-not-matter.db"), port=port)
    finally:
        sock.close()
    assert reason is not None
    assert "listening" in reason
