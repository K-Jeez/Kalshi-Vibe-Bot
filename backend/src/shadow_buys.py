"""Remember BUY directions the bot did not fill, then score them at resolution.

One unresolved row per market and side. A later yes/no result writes the
hypothetical profit of one contract at the recorded ask, including the
estimated entry fee. Nothing here places an order.
"""

from __future__ import annotations

import uuid
from typing import Optional

from src.decision_engine.strategy_gates import estimated_entry_fee_dollars
from src.util.datetimes import utc_now


def hypothetical_pnl(side: str, ask: float, result: str, *, fee_multiplier=None, fee_type=None) -> Optional[float]:
    outcome = str(result or "").strip().lower()
    held = str(side or "").strip().upper()
    if outcome not in ("yes", "no") or held not in ("YES", "NO"):
        return None
    px = float(ask)
    if px <= 0 or px >= 1:
        return None
    fee = estimated_entry_fee_dollars(
        1,
        px,
        fee_multiplier=1 if fee_multiplier is None else fee_multiplier,
        fee_type="quadratic" if fee_type is None else fee_type,
    )
    if fee is None:
        fee = 0.0
    won = (held == "YES" and outcome == "yes") or (held == "NO" and outcome == "no")
    if won:
        return 1.0 - px - float(fee)
    return -(px + float(fee))


def record_shadow_buy(
    db,
    *,
    market_id: str,
    side: str,
    reason: str,
    edge_pct: float,
    ai_yes_pct: int,
    ask: float,
    blended_side_pct: Optional[float] = None,
) -> bool:
    """Insert one open shadow row. A duplicate unresolved row is left as it is."""
    from src.database.models import ShadowBuy

    ticker = str(market_id or "").strip().upper()
    held = str(side or "").strip().upper()
    if not ticker or held not in ("YES", "NO"):
        return False
    existing = (
        db.query(ShadowBuy)
        .filter(
            ShadowBuy.market_id == ticker,
            ShadowBuy.side == held,
            ShadowBuy.resolved_result.is_(None),
        )
        .first()
    )
    if existing is not None:
        return False
    db.add(
        ShadowBuy(
            id=str(uuid.uuid4()),
            market_id=ticker,
            side=held,
            reason=str(reason or "")[:240],
            edge_pct=float(edge_pct or 0.0),
            ai_yes_pct=int(ai_yes_pct or 0),
            ask=float(ask or 0.0),
            blended_side_pct=None if blended_side_pct is None else float(blended_side_pct),
            created_at=utc_now(),
        )
    )
    return True


def resolve_shadow_row(row, result: str) -> bool:
    pnl = hypothetical_pnl(row.side, float(row.ask or 0.0), result)
    if pnl is None:
        return False
    row.resolved_result = str(result).strip().lower()
    row.hypothetical_pnl = float(pnl)
    row.resolved_at = utc_now()
    return True


def resolve_known_results(db) -> int:
    """Score shadows whose ticker already has a yes/no result on a position."""
    from src.database.models import Position, ShadowBuy

    pending = db.query(ShadowBuy).filter(ShadowBuy.resolved_result.is_(None)).all()
    if not pending:
        return 0
    n = 0
    for row in pending:
        found = (
            db.query(Position)
            .filter(Position.market_id == row.market_id)
            .order_by(Position.closed_at.desc())
            .first()
        )
        if found is None:
            continue
        text = str(getattr(found, "kalshi_market_result", "") or "").strip().lower()
        if resolve_shadow_row(row, text):
            n += 1
    return n


async def resolve_shadow_markets(db, kalshi_client, *, limit: int = 10) -> int:
    """Fetch a few unresolved tickers. Failures are skipped."""
    from src.database.models import ShadowBuy

    resolve_known_results(db)
    pending = (
        db.query(ShadowBuy.market_id)
        .filter(ShadowBuy.resolved_result.is_(None))
        .distinct()
        .limit(max(1, int(limit)))
        .all()
    )
    n = 0
    for (ticker,) in pending:
        try:
            market = await kalshi_client.get_market(ticker)
        except Exception:
            continue
        if not isinstance(market, dict):
            continue
        result = str(market.get("result") or market.get("market_result") or "").strip().lower()
        status = str(market.get("status") or "").strip().lower()
        if result not in ("yes", "no"):
            continue
        if status and status not in ("determined", "finalized", "settled", "closed"):
            continue
        for row in (
            db.query(ShadowBuy)
            .filter(ShadowBuy.market_id == ticker, ShadowBuy.resolved_result.is_(None))
            .all()
        ):
            if resolve_shadow_row(row, result):
                n += 1
    return n
