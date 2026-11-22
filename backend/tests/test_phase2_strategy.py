"""Skipped buys are scored once when the market result is already known."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, Position, ShadowBuy
from src.shadow_buys import hypothetical_pnl, record_shadow_buy, resolve_shadow_row
from src.util.datetimes import utc_now


def test_shadow_buy_is_scored_once_when_the_market_resolves():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    assert record_shadow_buy(
        db,
        market_id="KXTEST-YES",
        side="YES",
        reason="Skipped — the model’s edge is 24 points, above the 22-point safety cap",
        edge_pct=24,
        ai_yes_pct=90,
        ask=0.40,
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
