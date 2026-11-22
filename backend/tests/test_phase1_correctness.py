"""Phase-1 accounting, whole-contract fills, match cap, and retry rules."""

from __future__ import annotations

import asyncio
import csv
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.bot.buy_retry import gates_still_pass_at_ask, retry_limit
from src.bot.exit_backoff import note_empty_exit, reset_for_tests as reset_exits
from src.bot.match_exposure import match_exposure_key
from src.bot.order_outcome import order_status_after_submit
from src.database.models import Base, Position, Trade
from src.fixed_point import is_whole_count
from src.model_usage import estimate_usd
from src.reconcile.kalshi_closed_position_finalize import finalize_live_closed_positions_from_kalshi
from src.reconcile.kalshi_positions import sync_recent_closed_from_flat_portfolio_rows
from src.reconcile.realized_cashflow import fees_without_double_count, fills_to_cashflows, realized_from_cashflows
from src.util.datetimes import utc_now

FIXTURE = Path(__file__).parent / "fixtures" / "positions_lifetime.csv"


def _rows():
    with FIXTURE.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fills_for(row: dict) -> tuple[list, dict]:
    qty = float(row["contracts"])
    side = row["side"].upper()
    if side == "NO":
        fills = [
            {
                "ticker": row["ticker"],
                "action": "sell",
                "side": "no",
                "count_fp": qty,
                "yes_price": 1.0 - float(row["entry_price"]),
                "fee": float(row["entry_fees"]),
            }
        ]
    else:
        fills = [
            {
                "ticker": row["ticker"],
                "action": "buy",
                "side": "yes",
                "count_fp": qty,
                "yes_price": float(row["entry_price"]),
                "fee": float(row["entry_fees"]),
            }
        ]
    settlement = {"ticker": row["ticker"], "market_result": row["market_result"], "side": row["side"]}
    if row["exit_type"] == "stop-loss sell":
        fills.append(
            {
                "ticker": row["ticker"],
                "action": "sell",
                "side": "no",
                "count_fp": float(row["sold_contracts"]),
                "yes_price": float(row["exit_price"]),
                "fee": float(row["exit_fees"]),
            }
        )
    return fills, settlement


def test_lifetime_rows_match_the_snapshot_totals():
    totals = {"R1 Nov8": 0.0, "R2 Nov11": 0.0}
    closed = 0
    for row in _rows():
        if row["app_status"] != "closed":
            continue
        closed += 1
        fills, settlement = _fills_for(row)
        got = fills_to_cashflows(fills, settlement, held_side=row["side"])
        assert abs(got["realized_pnl"] - float(row["true_realized_pnl"])) < 0.02
        totals[row["round"]] += got["realized_pnl"]
    assert closed == 31
    assert abs(totals["R1 Nov8"] - 1.77) < 0.05
    assert abs(totals["R2 Nov11"] - (-6.24)) < 0.05
    assert abs(sum(totals.values()) - (-4.48)) < 0.05


def test_bug_a_flat_reconcile_does_not_add_kalshi_realized_again():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()
    db.add(
        Position(
            id="run20",
            market_id="KXATPEXACTMATCH-26NOV11KECRUN-RUN20",
            market_title="Rune",
            side="YES",
            quantity=4,
            entry_price=0.48,
            entry_cost=1.92,
            current_price=0.09,
            status="closed",
            trade_mode="live",
            closed_at=now,
            exit_reason="stop_loss",
            realized_pnl=-1.6529,
            fees_paid=0.0929,
            kalshi_flat_reconcile_pending=True,
            kalshi_closure_finalized=False,
        )
    )
    db.add(
        Trade(
            id="sell-run20",
            market_id="KXATPEXACTMATCH-26NOV11KECRUN-RUN20",
            market_title="Rune",
            action="sell",
            side="YES",
            quantity=4,
            price=0.09,
            total_cost=0.36,
            realized_pnl=-1.6529,
            trade_mode="live",
            timestamp=now,
        )
    )
    db.commit()
    sync_recent_closed_from_flat_portfolio_rows(
        db,
        trade_mode="live",
        api_rows=[
            {
                "ticker": "KXATPEXACTMATCH-26NOV11KECRUN-RUN20",
                "position_fp": "0.00",
                "realized_pnl_dollars": "-1.56",
                "fees_paid_dollars": "0.0929",
            }
        ],
    )
    db.commit()
    pos = db.query(Position).one()
    assert abs(float(pos.realized_pnl) - (-1.6529)) < 1e-4
    assert abs(float(pos.fees_paid) - 0.0929) < 1e-4
    assert pos.kalshi_flat_reconcile_pending is False


def test_bug_b_settlement_does_not_flip_a_stopped_position():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()
    db.add(
        Position(
            id="run3",
            market_id="KXATPGSPREAD-26NOV11KECRUN-RUN3",
            market_title="Rune spread",
            side="YES",
            quantity=3,
            entry_price=0.60,
            entry_cost=1.80,
            current_price=0.14,
            status="closed",
            trade_mode="live",
            closed_at=now,
            exit_reason="stop_loss",
            realized_pnl=-1.4557,
            fees_paid=0.0757,
            kalshi_flat_reconcile_pending=False,
            kalshi_closure_finalized=False,
        )
    )
    db.add(
        Trade(
            id="sell-run3",
            market_id="KXATPGSPREAD-26NOV11KECRUN-RUN3",
            market_title="Rune spread",
            action="sell",
            side="YES",
            quantity=3,
            price=0.14,
            total_cost=0.42,
            realized_pnl=-1.4557,
            trade_mode="live",
            timestamp=now,
        )
    )
    db.commit()
    settlement = {
        "ticker": "KXATPGSPREAD-26NOV11KECRUN-RUN3",
        "yes_count_fp": "3.00",
        "no_count_fp": "3.00",
        "market_result": "yes",
        "fee_cost": "0.0757",
        "revenue": 300,
    }

    class _Noop:
        async def get_order(self, _oid):
            return {}

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
    assert float(pos.realized_pnl) < 0
    assert abs(float(pos.realized_pnl) - (-1.4557)) < 1e-4
    assert pos.exit_reason == "stop_loss"


def test_bug_c_and_doubled_fees_stay_on_the_sell():
    shw = realized_from_cashflows(
        buy_notional=2.00,
        buy_fees=0.07,
        sell_proceeds=0.20,
        sell_fees=0.0133,
        settlement_payout=0.0,
        closed_by_sell=True,
    )
    assert abs(shw["realized_pnl"] - (-1.8833)) < 1e-3
    assert abs(fees_without_double_count(0.1115, 0.1115) - 0.1115) < 1e-6
    corners = next(row for row in _rows() if row["ticker"].endswith("ATMBAR-8"))
    fills, settlement = _fills_for(corners)
    got = fills_to_cashflows(fills, settlement)
    assert abs(got["realized_pnl"] - (-3.2535)) < 0.02
    assert got["closed_by_sell"] is True


def test_fractional_fill_is_not_booked_as_a_whole_position():
    residue = Decimal("0.01")
    assert is_whole_count(residue) is False
    assert not (is_whole_count(residue) and int(residue) >= 1)
    assert order_status_after_submit("fill_or_kill", 0) == "canceled"


def test_match_key_groups_the_rune_legs_and_not_a_different_match():
    keys = {
        match_exposure_key("KXATPEXACTMATCH-26NOV11KECRUN-RUN20"),
        match_exposure_key("KXATPGSPREAD-26NOV11KECRUN-RUN3"),
        match_exposure_key("KXATPSETWINNER-26NOV11KECRUN-1-RUN"),
        match_exposure_key("KXATPGTOTAL-26NOV11KECRUN-23"),
    }
    assert keys == {"match:26NOV11:KECRUN"}
    assert match_exposure_key("KXNFLTD-26NOV08GBNE-NERSTEVENSON38-1") == "match:26NOV08:GBNE"
    assert match_exposure_key("KXETH-26NOV1117-B2080") == "asset:ETH:26NOV11"
    assert match_exposure_key("KXETHD-26NOV11-B2000") == "asset:ETH:26NOV11"
    assert match_exposure_key("KXBTC-26NOV11-B100") == match_exposure_key("KXBTCD-26NOV11-T100")
    assert match_exposure_key("KXHIGHNY-26NOV11") != match_exposure_key("KXHIGHCHI-26NOV11")


def test_retry_limit_is_two_cents_and_gates_can_refuse():
    assert retry_limit(0.50, 0.52) == 0.52
    assert retry_limit(0.50, 0.53) is None
    assert gates_still_pass_at_ask(
        side="YES",
        ai_yes_pct=56,
        ask=0.54,
        yes_ask=0.54,
        no_ask=0.46,
        min_edge=4,
        min_ai=55,
        fee_multiplier=1,
        fee_type="quadratic",
    ) is False


def test_empty_book_exit_backs_off_then_stops():
    reset_exits()
    assert note_empty_exit("KXNCAAWBSPREAD-26NOV11QUINSJU-SJU23", now=0) == 1
    assert note_empty_exit("KXNCAAWBSPREAD-26NOV11QUINSJU-SJU23", now=0) == 2
    assert note_empty_exit("KXNCAAWBSPREAD-26NOV11QUINSJU-SJU23", now=0) == 3


def test_grok_43_price_is_not_the_fast_rate():
    cost = estimate_usd(1_000_000, 1_000_000, 0)
    assert abs(cost - (1.25 + 2.50)) < 1e-9
    cached = estimate_usd(1_000_000, 0, 1_000_000)
    assert abs(cached - 0.20) < 1e-9
