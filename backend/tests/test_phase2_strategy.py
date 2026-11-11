"""Blend for sizing, edge-bucket cash caps, and shadow outcomes of skipped buys."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Position, ShadowBuy
from src.decision_engine.blend import blend_snapshot, blend_yes_probability, fee_free_yes_mid
from src.decision_engine.bucket_caps import cap_for_edge, cap_fraction_for_return, caps_from_closed
from src.decision_engine.strategy_gates import MAX_EDGE_TO_BUY_PCT, fee_inclusive_ev_dollars
from src.decision_engine.strategy_math import kelly_contracts_for_order
from src.shadow_buys import hypothetical_pnl, record_shadow_buy, resolve_shadow_row
from src.util.datetimes import utc_now


def test_blend_uses_the_fee_free_mid_and_keeps_a_quarter_weight():
    mid, source = fee_free_yes_mid(yes_bid=0.40, yes_ask=0.60)
    assert source == "fee-free mid"
    assert abs(mid - 0.50) < 1e-9
    blended = blend_yes_probability(80, mid, 0.25)
    assert abs(blended - 0.575) < 1e-9
    missing_bid, fallback = fee_free_yes_mid(yes_bid=None, yes_ask=0.62)
    assert fallback == "ask-implied"
    assert abs(missing_bid - 0.62) < 1e-9
    snap = blend_snapshot(side="NO", model_yes_pct=40, yes_bid=0.40, yes_ask=0.60, weight=0.25)
    # Model YES 40 and mid 50 blend to 47.5, so P(NO) is 52.5.
    assert abs(snap["win_probability"] - 0.525) < 1e-9


def test_raw_edge_can_pass_while_blended_ev_does_not():
    """Gates stay on the raw model. Size and EV use the blend."""
    assert MAX_EDGE_TO_BUY_PCT == 22
    raw_edge = 70 - 62
    assert raw_edge >= 4
    blend = blend_yes_probability(70, 0.60, 0.25)
    ev = fee_inclusive_ev_dollars(blend, 0.62, 1, fee_multiplier=1, fee_type="quadratic")
    raw_ev = fee_inclusive_ev_dollars(0.70, 0.62, 1, fee_multiplier=1, fee_type="quadratic")
    assert raw_ev is not None and raw_ev > 0
    assert ev is not None and ev <= 0
    raw_qty, _ = kelly_contracts_for_order(
        1000, "YES", 70, 0.62, 0.40, 0.60, 0.40, per_contract_premium=0.62, win_probability=0.70
    )
    blend_qty, _ = kelly_contracts_for_order(
        1000, "YES", 70, 0.62, 0.40, 0.60, 0.40, per_contract_premium=0.62, win_probability=blend
    )
    assert blend_qty < raw_qty


def test_bucket_cap_stays_put_until_eight_closes_then_steps():
    assert cap_fraction_for_return(-50, 10, 7) == 0.025
    shrunk = cap_fraction_for_return(-1, 10, 8)
    assert 0.01 <= shrunk < 0.025
    grown = cap_fraction_for_return(1, 10, 8)
    assert 0.025 < grown <= 0.05
    assert cap_fraction_for_return(-100, 1, 8) == 0.01
    assert cap_fraction_for_return(100, 1, 8) == 0.05
    table = caps_from_closed(
        [{"edge": 5, "realized_pnl": -1, "cost": 10} for _ in range(8)]
        + [{"edge": 14, "realized_pnl": 1, "cost": 10} for _ in range(8)]
    )
    assert cap_for_edge(table, 5.5) < 0.025
    assert cap_for_edge(table, 14) > 0.025
    assert cap_for_edge(table, 9) == 0.025


def test_shadow_buy_is_scored_once_when_the_market_resolves():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    assert record_shadow_buy(
        db,
        market_id="KXTEST-YES",
        side="YES",
        reason="Skipped — edge 24.0 pts exceeds autonomous cap (22 pts; often model overconfidence)",
        edge_pct=24,
        ai_yes_pct=90,
        ask=0.40,
        blended_side_pct=70,
    )
    assert record_shadow_buy(
        db,
        market_id="KXTEST-YES",
        side="YES",
        reason="duplicate",
        edge_pct=24,
        ai_yes_pct=90,
        ask=0.40,
    ) is False
    db.commit()
    row = db.query(ShadowBuy).one()
    assert resolve_shadow_row(row, "yes")
    db.commit()
    pnl = hypothetical_pnl("YES", 0.40, "yes")
    assert pnl is not None and pnl > 0
    assert abs(float(row.hypothetical_pnl) - pnl) < 1e-9
    assert row.resolved_result == "yes"
    miss = ShadowBuy(
        id="miss",
        market_id="KXMISS",
        side="YES",
        reason="Missed buy. Price gap +0.02",
        edge_pct=6,
        ai_yes_pct=60,
        ask=0.55,
        created_at=utc_now(),
    )
    db.add(miss)
    db.add(
        Position(
            id="p",
            market_id="KXMISS",
            market_title="m",
            side="YES",
            quantity=1,
            entry_price=0.55,
            entry_cost=0.55,
            current_price=0,
            status="closed",
            trade_mode="live",
            kalshi_market_result="no",
            realized_pnl=-0.55,
        )
    )
    db.commit()
    from src.shadow_buys import resolve_known_results

    assert resolve_known_results(db) == 1
    db.commit()
    db.refresh(miss)
    assert miss.resolved_result == "no"
    assert float(miss.hypothetical_pnl) < 0
