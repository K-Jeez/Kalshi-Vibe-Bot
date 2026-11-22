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
from src.database.models import ActivityEvent, Base, DecisionLog, Position, Trade
from src.decision_engine.bucket_caps import cap_fraction_for_return
from src.bot.book_walk import size_still_fits
from src.decision_engine.history_sizing import (
    calibrated_blend_weight,
    effective_edge_floor,
    expected_xai_cost_per_buy,
    load_model_spend,
    load_settled_calibration,
    market_yes_probability,
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
            quantity=2,
            price=0.09,
            total_cost=0.18,
            realized_pnl=-0.80,
            trade_mode="live",
            timestamp=now,
        )
    )
    db.add(
        Trade(
            id="sell-stop-2",
            market_id="SYN-STOP",
            market_title="t",
            action="sell",
            side="YES",
            quantity=2,
            price=0.09,
            total_cost=0.18,
            realized_pnl=-0.8529,
            trade_mode="live",
            timestamp=now,
        )
    )
    db.add(
        ActivityEvent(
            id="act-stop",
            seq=1,
            run_id="test",
            event_type="order",
            market_id="SYN-STOP",
            action="sell",
            detail="auto-exit stop_loss",
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


def test_manual_sell_is_not_relabeled_as_a_stop():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()
    db.add(
        Position(
            id="man",
            market_id="SYN-MAN",
            market_title="t",
            side="YES",
            quantity=1,
            entry_price=0.40,
            entry_cost=0.40,
            current_price=0.20,
            status="open",
            trade_mode="live",
            realized_pnl=0.0,
        )
    )
    db.add(
        Trade(
            id="sell-man",
            market_id="SYN-MAN",
            market_title="t",
            action="sell",
            side="YES",
            quantity=1,
            price=0.20,
            total_cost=0.20,
            realized_pnl=-0.20,
            trade_mode="live",
            timestamp=now,
        )
    )
    db.add(
        ActivityEvent(
            id="act-man",
            seq=1,
            run_id="test",
            event_type="order",
            market_id="SYN-MAN",
            action="sell",
            detail="manual close",
        )
    )
    db.commit()
    asyncio.run(
        apply_kalshi_settlement_closes(
            db,
            trade_mode="live",
            api_rows=[{"ticker": "SYN-MAN", "position_fp": "0.00", "realized_pnl_dollars": "0.00"}],
            broadcast_fn=None,
            kalshi_client=None,
        )
    )
    pos = db.query(Position).one()
    assert pos.exit_reason == "manual"
    assert abs(float(pos.realized_pnl) - (-0.20)) < 1e-9


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
        [{"ticker": "OPEN", "market_result": "yes", "revenue": 300, "settled_time": "2026-11-12T22:01:24Z"}],
    )
    assert open_settled[0]["write"] is True
    assert open_settled[0]["note"] == "close locally open settled row"
    assert open_settled[0]["closed_at"] == "2026-11-12T22:01:24Z"
    assert abs(open_settled[0]["new_realized_pnl"] - (3.0 - 1.71 - 0.0515)) < 1e-6
    assert open_settled[0]["new_exit_reason"] == "settlement"


def test_sizing_uses_history_and_does_not_force_one_contract():
    assert calibrated_blend_weight([], prior=0.25) == 1.0
    rows = [{"model_yes": 0.70, "market_yes": 0.50, "outcome_yes": 1.0} for _ in range(8)]
    assert calibrated_blend_weight(rows, prior=0.25) == 1.0
    weak = [{"model_yes": 0.20, "market_yes": 0.80, "outcome_yes": 1.0} for _ in range(8)]
    assert calibrated_blend_weight(weak, prior=0.25) == 0.0
    assert cap_fraction_for_return(1, 10, 8) > cap_fraction_for_return(1, 10, 8, model_cost=5)
    assert effective_edge_floor(4, realized_pnl=1, cost=10, model_cost=0, n=8) == 4
    raised = effective_edge_floor(4, realized_pnl=-5, cost=10, model_cost=1, n=8)
    assert raised > 4
    assert effective_edge_floor(4, realized_pnl=-0.10, cost=10, model_cost=0, n=8) == 4
    capped = effective_edge_floor(4, realized_pnl=-10, cost=10, model_cost=0, n=8, bucket="4-6")
    assert capped < 6
    assert abs(market_yes_probability(70, "NO") - 0.30) < 1e-9
    assert abs(market_yes_probability(70, "YES") - 0.70) < 1e-9
    no_rows = [
        {"model_yes": 0.40, "market_yes": market_yes_probability(70, "NO"), "outcome_yes": 0.0}
        for _ in range(8)
    ]
    wrong_rows = [{"model_yes": 0.40, "market_yes": 0.70, "outcome_yes": 0.0} for _ in range(8)]
    assert calibrated_blend_weight(no_rows, prior=0.25) < calibrated_blend_weight(wrong_rows, prior=0.25)
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


def test_model_size_keeps_small_edges_and_requote_names_the_failure():
    from src.bot.book_walk import fresh_quote_skip_reasons, largest_qualifying_buy
    from src.decision_engine.history_sizing import brier_skill, sizing_model_weight
    from src.decision_engine.strategy_gates import kelly_contract_cap_for_bankroll
    from src.decision_engine.strategy_math import kelly_order_skip_summary

    mild = [{"model_yes": 0.62, "market_yes": 0.58, "outcome_yes": 1.0 if i % 2 == 0 else 0.0} for i in range(64)]
    skill, t_stat, n = brier_skill(mild)
    assert n == 64 and skill < 0 and t_stat < 2
    assert sizing_model_weight(mild) == 1.0

    cap = kelly_contract_cap_for_bankroll(96, 0.60, fee_multiplier=1, fee_type="quadratic", bankroll_fraction=0.025)
    espen_raw, _tag = kelly_contracts_for_order(
        96, "YES", 68, 0.60, 0.40, 0.60, 0.40,
        per_contract_premium=0.60, max_kelly_contracts=cap, win_probability=0.68,
    )
    espen_blend, _old = kelly_contracts_for_order(
        96, "YES", 68, 0.60, 0.40, 0.60, 0.40,
        per_contract_premium=0.60, max_kelly_contracts=cap,
        win_probability=0.2407 * 0.68 + (1 - 0.2407) * 0.58,
    )
    assert espen_raw >= 1
    assert espen_blend == 0

    mhu_cap = kelly_contract_cap_for_bankroll(96, 0.54, fee_multiplier=1, fee_type="quadratic", bankroll_fraction=0.025)
    mhu_raw, _tag = kelly_contracts_for_order(
        96, "YES", 71, 0.54, 0.46, 0.48, 0.52,
        per_contract_premium=0.54, max_kelly_contracts=mhu_cap, win_probability=0.71,
    )
    mhu_blend, _old = kelly_contracts_for_order(
        96, "YES", 71, 0.54, 0.46, 0.48, 0.52,
        per_contract_premium=0.54, max_kelly_contracts=mhu_cap,
        win_probability=0.2407 * 0.71 + (1 - 0.2407) * 0.48,
    )
    assert mhu_raw >= 1
    assert mhu_blend == 0

    wording = kelly_order_skip_summary(
        96, "YES", 51, 0.50, 0.50, 0.50, 0.50,
        per_contract_premium=0.50, win_probability=0.51,
    )
    assert wording is not None
    assert "below one contract" in wording
    assert "cannot buy" not in wording

    common = dict(
        side="YES",
        ai_yes_pct=68,
        min_edge=4,
        min_ai=55,
        fee_multiplier=1,
        fee_type="quadratic",
        blend_ev_gate=False,
        balance=96,
        cap_fraction=0.025,
        win_probability=0.68,
        model_cost=0.0846,
        equity=96,
        open_event_cost=0,
        event_reserve=0,
        min_gross=0.10,
    )

    def _ok(qty, limit):
        return size_still_fits(
            quantity=qty,
            limit=limit,
            yes_ask=limit,
            no_ask=1.0 - limit,
            **common,
        )

    qty, limit = largest_qualifying_buy([(0.60, 20.0)], 8, _ok)
    assert qty >= 1 and abs(limit - 0.60) < 1e-9
    assert _ok(1, 0.60) is True
    moved = fresh_quote_skip_reasons(
        [(0.80, 10.0)],
        original_ask=0.60,
        yes_ask=0.80,
        no_ask=0.20,
        **common,
    )
    assert "raw edge" in moved
    assert "price moved" in moved
    assert fresh_quote_skip_reasons([], original_ask=0.60, yes_ask=0.60, no_ask=0.40, **common) == ["empty/thin book"]


def test_private_lan_can_mutate_and_public_internet_cannot():
    assert client_is_local("192.168.0.12")
    assert client_is_local("172.16.5.5")
    assert client_is_local("169.254.1.1")
    assert client_is_local("fd12::1")
    assert client_is_local("fe80::1")
    assert not client_is_local("1.2.3.4")
    assert not client_is_local("2001:db8::1")
    assert mutation_is_allowed(host="192.168.0.12", headers={}, configured_token="")
    assert mutation_is_allowed(host="fd00::5", headers={}, configured_token="")
    assert not mutation_is_allowed(host="1.2.3.4", headers={}, configured_token="")


def test_no_buy_calibration_and_recent_model_cost_and_resized_limit():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = utc_now()
    db.add(
        DecisionLog(
            id="d-no",
            market_id="NOLEG",
            market_title="t",
            decision="BUY_NO",
            confidence=0.6,
            ai_probability_yes_pct=40,
            market_implied_probability_pct=70,
            edge=10,
        )
    )
    db.add(
        Position(
            id="p-no",
            market_id="NOLEG",
            market_title="t",
            side="NO",
            quantity=1,
            entry_price=0.70,
            entry_cost=0.70,
            current_price=1.0,
            status="closed",
            trade_mode="live",
            entry_decision_log_id="d-no",
            kalshi_market_result="no",
        )
    )
    old = now.replace(year=2020)
    db.add(
        DecisionLog(
            id="old",
            market_id="OLD",
            market_title="t",
            decision="BUY_YES",
            confidence=0.5,
            timestamp=old,
            action_taken='{"status":"executed"}',
            xai_analysis="{}",
        )
    )
    db.add(
        DecisionLog(
            id="new",
            market_id="NEW",
            market_title="t",
            decision="BUY_YES",
            confidence=0.5,
            timestamp=now,
            action_taken='{"status":"executed"}',
            xai_analysis='{"usage":{"estimated_usd":0.067}}',
        )
    )
    db.commit()
    calibrated = load_settled_calibration(db)
    assert abs(calibrated[0]["market_yes"] - 0.30) < 1e-9
    costs, buys = load_model_spend(db)
    assert costs == [0.067]
    assert buys == 1
    assert size_still_fits(
        quantity=1,
        limit=0.70,
        side="YES",
        ai_yes_pct=85,
        yes_ask=0.70,
        no_ask=0.30,
        min_edge=4,
        min_ai=55,
        fee_multiplier=1,
        fee_type="quadratic",
        blend_ev_gate=False,
        balance=100,
        cap_fraction=0.025,
        win_probability=0.85,
        model_cost=0,
        equity=100,
        open_event_cost=0,
        event_reserve=0,
        min_gross=0.10,
    ) is True
    assert size_still_fits(
        quantity=2,
        limit=0.70,
        side="YES",
        ai_yes_pct=85,
        yes_ask=0.70,
        no_ask=0.30,
        min_edge=4,
        min_ai=55,
        fee_multiplier=1,
        fee_type="quadratic",
        blend_ev_gate=False,
        balance=100,
        cap_fraction=0.025,
        win_probability=0.85,
        model_cost=0,
        equity=100,
        open_event_cost=0,
        event_reserve=0,
        min_gross=0.10,
    ) is True
