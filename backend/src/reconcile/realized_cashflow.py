"""Realized P&L from fills and settlement, counted once.

Buy notional and sell proceeds are the contract cash. Fees are the fill fees
only. A settlement ``fee_cost`` is the same cumulative trade fee, not a second
charge. A position closed by a sell does not also collect the settlement payout.
"""

from __future__ import annotations

from typing import Optional

_SOLD_REASONS = frozenset({"stop_loss", "manual", "take_profit", "exit"})


def closed_by_our_sell(exit_reason: object, has_sell_trade: bool) -> bool:
    if has_sell_trade:
        return True
    return str(exit_reason or "").strip().lower() in _SOLD_REASONS


def fees_without_double_count(current_fees: float, reported_cumulative: float) -> float:
    """Keep the larger cumulative fee. Never add the report on top of itself."""
    current = max(0.0, float(current_fees or 0.0))
    reported = max(0.0, float(reported_cumulative or 0.0))
    return max(current, reported)


def realized_from_cashflows(
    *,
    buy_notional: float,
    buy_fees: float,
    sell_proceeds: float = 0.0,
    sell_fees: float = 0.0,
    settlement_payout: float = 0.0,
    closed_by_sell: bool,
) -> dict:
    """Idempotent realized dollars and the fee total those dollars already include."""
    notional = float(buy_notional or 0.0)
    buy_fee = max(0.0, float(buy_fees or 0.0))
    proceeds = float(sell_proceeds or 0.0)
    sell_fee = max(0.0, float(sell_fees or 0.0))
    payout = float(settlement_payout or 0.0)
    if closed_by_sell:
        fees = buy_fee + sell_fee
        realized = proceeds - notional - fees
    else:
        fees = buy_fee
        realized = payout - notional - fees
    return {"realized_pnl": float(realized), "fees_paid": float(fees)}


def fills_to_cashflows(fills: list, settlement: Optional[dict] = None) -> dict:
    """Sum Kalshi fills for one ticker.

    A stop-loss on this account is ``action=sell`` with ``side=no``. Its
    ``yes_price`` is the YES exit price, so proceeds are count times that price.
    """
    buy_notional = 0.0
    buy_fees = 0.0
    buy_count = 0.0
    sell_proceeds = 0.0
    sell_fees = 0.0
    sell_count = 0.0
    side = ""
    for fill in fills or []:
        count = float(fill.get("count_fp") or fill.get("count") or 0.0)
        price = float(fill.get("yes_price") or fill.get("price") or 0.0)
        fee = float(fill.get("fee") or fill.get("fees") or 0.0)
        action = str(fill.get("action") or "").strip().lower()
        if not side and action == "buy":
            side = str(fill.get("side") or "").strip().upper()
        if action == "sell":
            sell_count += count
            sell_proceeds += count * price
            sell_fees += fee
        elif action == "buy":
            buy_count += count
            buy_notional += count * price
            buy_fees += fee
    closed = sell_count > 1e-9
    payout = 0.0
    if not closed and settlement:
        result = str(settlement.get("market_result") or "").strip().lower()
        held = side or str(settlement.get("side") or "").strip().upper()
        won = (held == "YES" and result == "yes") or (held == "NO" and result == "no")
        if won:
            payout = float(settlement.get("payout") or buy_count or 0.0)
    out = realized_from_cashflows(
        buy_notional=buy_notional,
        buy_fees=buy_fees,
        sell_proceeds=sell_proceeds,
        sell_fees=sell_fees,
        settlement_payout=payout,
        closed_by_sell=closed,
    )
    out["closed_by_sell"] = closed
    out["side"] = side
    return out
