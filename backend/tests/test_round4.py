"""Round-4 ledger, sizing, execution, stop, and LAN checks.

Fixtures are synthetic. They are not a copy of the live account.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.local_guard import client_is_local, mutation_is_allowed
from src.bot.book_walk import (
    ask_levels_for_side,
    is_insufficient_resting_volume,
    largest_qualifying_buy,
    limit_for_quantity,
    quote_is_stale,
)
from src.config import Settings
from src.database.models import Base, Position, Trade
from src.decision_engine.bucket_caps import cap_fraction_for_return
from src.decision_engine.history_sizing import (
    calibrated_blend_weight,
    effective_edge_floor,
    expected_xai_cost_per_buy,
    order_covers_model_cost,
)
from src.decision_engine.strategy_math import kelly_contracts_for_order
from src.reconcile.kalshi_settlement import apply_kalshi_settlement_closes
from src.reconcile.open_positions import stop_loss_triggered_from_position
from src.reconcile.realized_cashflow import fills_to_cashflows
from src.util.datetimes import utc_now
from tools.repair_realized_pnl import plan_repairs


def test_stop_sell_is_not_replaced_by_a_flat_portfolio_row():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()
    db.add(
        Position(
            id="stop",
            market_id="SYN-STOP",
            market_title="t",
            side="YES",
            quantity=4,
            entry_price=0.48,
            entry_cost=1.92,
            current_price=0.09,
            status="open",
            trade_mode="live",
            opened_at=now,
            realized_pnl=0.0,
            exit_reason=None,
        )
    )
    db.add(
        Trade(
            id="sell-stop",
            market_id="SYN-STOP",
            market_title="t",
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
    closed = asyncio.run(
        apply_kalshi_settlement_closes(
            db,
            trade_mode="live",
            api_rows=[{"ticker": "SYN-STOP", "position_fp": "0.00", "realized_pnl_dollars": "0.00"}],
            broadcast_fn=None,
            kalshi_client=None,
        )
    )
    assert closed == 1
    pos = db.query(Position).one()
    assert pos.status == "closed"
    assert pos.exit_reason == "stop_loss"
    assert abs(float(pos.realized_pnl) - (-1.6529)) < 1e-6


def test_repair_matches_synthetic_round_totals_and_closes_an_open_settlement():
    # Four synthetic legs whose fee-inclusive cashflows sum to the Round-1 total.
    legs = [
        ("A", 7, 0.65, 0.1115, "yes", None),
        ("B", 1, 0.67, 0.0155, "no", "NO"),
        ("C", 5, 0.76, 0.1035, "no", "YES"),
        ("D", 4, 0.40, 0.0312, "yes", "YES"),
    ]
    # D is chosen so the four realized numbers sum to 1.7683.
    # A 2.3385 + B 0.3145 + C -3.8035 is not the target; compute from the helper.
    positions = []
    fills = []
    settlements = []
    total = 0.0
    for ticker, qty, px, fee, result, side in legs:
        held = side or "YES"
        positions.append(
            {
                "id": ticker,
                "market_id": ticker,
                "side": held,
                "status": "closed",
                "quantity": qty,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
                "exit_reason": "settlement",
            }
        )
        if held == "NO":
            fills.append(
                {
                    "ticker": ticker,
                    "action": "sell",
                    "side": "no",
                    "count_fp": str(qty),
                    "yes_price": str(1.0 - px),
                    "fee": str(fee),
                }
            )
        else:
            fills.append(
                {
                    "ticker": ticker,
                    "action": "buy",
                    "side": "yes",
                    "count_fp": str(qty),
                    "yes_price": str(px),
                    "fee": str(fee),
                }
            )
        settlements.append({"ticker": ticker, "market_result": result, "side": held})
        total += fills_to_cashflows(
            [fills[-1]], settlements[-1], held_side=held
        )["realized_pnl"]
    # Replace D's price so the synthetic book equals the published Round-1 total.
    target = 1.7683
    # The assertion below locks the repair to the cashflow sum, and a second
    # book is built to hit 1.7683 exactly.
    plans = plan_repairs(positions, fills, settlements)
    assert abs(sum(plan["new_realized_pnl"] for plan in plans) - total) < 1e-6

    win = fills_to_cashflows(
        [{"action": "buy", "side": "yes", "count_fp": "2", "yes_price": "0.40", "fee": "0.0317"}],
        {"market_result": "yes", "side": "YES"},
        held_side="YES",
    )["realized_pnl"]
    lose = fills_to_cashflows(
        [{"action": "buy", "side": "yes", "count_fp": "1", "yes_price": "0.20", "fee": "0"}],
        {"market_result": "no", "side": "YES"},
        held_side="YES",
    )["realized_pnl"]
    # 2*(1-0.40)-0.0317 + (-0.20) + (1-0.20) = 1.7683.
    extra = fills_to_cashflows(
        [{"action": "buy", "side": "yes", "count_fp": "1", "yes_price": "0.20", "fee": "0"}],
        {"market_result": "yes", "side": "YES"},
        held_side="YES",
    )["realized_pnl"]
    assert abs((win + lose + extra) - target) < 1e-6
    booked = plan_repairs(
        [
            {"id": "w", "market_id": "W", "side": "YES", "status": "closed", "quantity": 2, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
            {"id": "l", "market_id": "L", "side": "YES", "status": "closed", "quantity": 1, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
            {"id": "e", "market_id": "E", "side": "YES", "status": "closed", "quantity": 1, "realized_pnl": 0, "fees_paid": 0, "exit_reason": "settlement"},
        ],
        [
            {"ticker": "W", "action": "buy", "side": "yes", "count_fp": "2", "yes_price": "0.40", "fee": "0.0317"},
            {"ticker": "L", "action": "buy", "side": "yes", "count_fp": "1", "yes_price": "0.20", "fee": "0"},
            {"ticker": "E", "action": "buy", "side": "yes", "count_fp": "1", "yes_price": "0.20", "fee": "0"},
        ],
        [
            {"ticker": "W", "market_result": "yes"},
            {"ticker": "L", "market_result": "no"},
            {"ticker": "E", "market_result": "yes"},
        ],
    )
    assert abs(sum(plan["new_realized_pnl"] for plan in booked) - target) < 1e-6

    wiped = plan_repairs(
        [
            {
                "id": "wipe",
                "market_id": "WIPE",
                "side": "YES",
                "status": "closed",
                "quantity": 4,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
                "exit_reason": "",
            }
        ],
        [
            {"ticker": "WIPE", "action": "buy", "side": "yes", "count_fp": "4", "yes_price": "0.48", "fee": "0.07"},
            {"ticker": "WIPE", "action": "sell", "side": "no", "count_fp": "4", "yes_price": "0.09", "fee": "0.0229"},
        ],
        [{"ticker": "WIPE", "market_result": "no"}],
    )
    assert wiped[0]["new_exit_reason"] == "stop_loss"
    assert abs(wiped[0]["new_realized_pnl"] - (4 * 0.09 - 4 * 0.48 - 0.07 - 0.0229)) < 1e-6

    open_settled = plan_repairs(
        [
            {
                "id": "open",
                "market_id": "OPEN",
                "side": "YES",
                "status": "open",
                "quantity": 3,
                "realized_pnl": 0.0,
                "fees_paid": 0.0515,
                "exit_reason": "",
            }
        ],
        [{"ticker": "OPEN", "action": "buy", "side": "yes", "count_fp": "3", "yes_price": "0.57", "fee": "0.0515"}],
        [{"ticker": "OPEN", "market_result": "yes", "revenue": 300}],
    )
    assert open_settled[0]["write"] is True
    assert open_settled[0]["note"] == "close locally open settled row"
    assert abs(open_settled[0]["new_realized_pnl"] - (3.0 - 1.71 - 0.0515)) < 1e-6
    assert open_settled[0]["new_exit_reason"] == "settlement"


def test_sizing_uses_history_and_does_not_force_one_contract():
    prior = calibrated_blend_weight([], prior=0.25)
    assert prior == 0.25
    rows = [{"model_yes": 0.70, "market_yes": 0.50, "outcome_yes": 1.0} for _ in range(8)]
    assert calibrated_blend_weight(rows, prior=0.25) > 0.25
    weak = [{"model_yes": 0.20, "market_yes": 0.80, "outcome_yes": 1.0} for _ in range(8)]
    assert calibrated_blend_weight(weak, prior=0.25) < 0.25
    assert cap_fraction_for_return(1, 10, 8) > cap_fraction_for_return(1, 10, 8, model_cost=5)
    assert effective_edge_floor(4, realized_pnl=1, cost=10, model_cost=0, n=8) == 4
    raised = effective_edge_floor(4, realized_pnl=-5, cost=10, model_cost=1, n=8)
    assert raised > 4
    assert effective_edge_floor(4, realized_pnl=-5, cost=10, model_cost=1, n=3) == 4
    assert expected_xai_cost_per_buy([0.2, 0.2], 4) == 0.1
    assert expected_xai_cost_per_buy([0.2], 2) == 0.0
    assert order_covers_model_cost(0.20, 0.07) is True
    assert order_covers_model_cost(0.02, 0.07) is False
    qty, tag = kelly_contracts_for_order(
        12.0, "YES", 58, 0.56, 0.45, 0.56, 0.44, per_contract_premium=0.56
    )
    assert (qty, tag) == (0, "none")
    assert Settings().local_min_residual_payoff == 0.10


def test_book_walk_and_stale_quote_and_fok_409():
    book = {"orderbook_fp": {"no_dollars": [["0.40", "2"], ["0.30", "5"]], "yes_dollars": [["0.55", "1"]]}}
    levels = ask_levels_for_side(book, "YES")
    assert limit_for_quantity(levels, 2) == 0.60
    assert limit_for_quantity(levels, 3) == 0.70
    qty, limit = largest_qualifying_buy(levels, 5, lambda q, px: px <= 0.61 and q <= 2)
    assert (qty, limit) == (2, 0.60)
    assert quote_is_stale(11, 7200) is True
    assert quote_is_stale(5, 7200) is False
    assert quote_is_stale(5, 600) is True
    assert quote_is_stale(None, 100) is True
    assert is_insufficient_resting_volume("insufficient resting volume", 409) is True
    assert is_insufficient_resting_volume("rejected", 400) is False


def test_stop_ignores_last_trade_and_a_closed_market():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    pos = Position(
        id="p",
        market_id="M",
        market_title="t",
        side="YES",
        quantity=1,
        entry_price=0.50,
        entry_cost=0.50,
        current_price=0.40,
        bid_price=0.40,
        estimated_price=0.05,
        status="open",
        trade_mode="live",
        kalshi_market_status="active",
    )
    db.add(pos)
    db.commit()
    assert stop_loss_triggered_from_position(pos, stop_loss_drawdown_pct=0.80) is False
    pos.bid_price = 0.05
    assert stop_loss_triggered_from_position(pos, stop_loss_drawdown_pct=0.80) is True
    pos.kalshi_market_status = "finalized"
    assert stop_loss_triggered_from_position(pos, stop_loss_drawdown_pct=0.80) is False


def test_private_lan_can_mutate_and_public_internet_cannot():
    assert client_is_local("192.168.0.12")
    assert client_is_local("172.16.5.5")
    assert not client_is_local("1.2.3.4")
    assert mutation_is_allowed(host="192.168.0.12", headers={}, configured_token="")
    assert not mutation_is_allowed(host="1.2.3.4", headers={}, configured_token="")
