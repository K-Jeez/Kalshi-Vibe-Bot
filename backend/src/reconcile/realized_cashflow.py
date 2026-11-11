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


def settlement_revenue_dollars(settlement: Optional[dict]) -> Optional[float]:
    """Kalshi ``revenue`` is integer cents. ``revenue_dollars`` is already dollars."""
    if not settlement:
        return None
    if settlement.get("revenue_dollars") not in (None, ""):
        return float(settlement["revenue_dollars"])
    if settlement.get("revenue") not in (None, ""):
        return float(int(float(settlement["revenue"]))) / 100.0
    return None


def _stated_count(settlement: dict, *keys: str) -> Optional[float]:
    for key in keys:
        if key in settlement and settlement.get(key) not in (None, ""):
            return fill_count({"count_fp": settlement.get(key)})
    return None


def sale_split(
    *,
    position_qty: int,
    sold_qty: int,
    settlement: Optional[dict] = None,
    side: str = "YES",
) -> dict:
    """Split a close using Kalshi's bought-versus-sold settlement counts.

    ``yes_count`` is every contract bought and ``no_count`` is every contract
    sold on the NO side of the book. A YES position's remainder is
    ``yes_count - no_count``. NO positions mirror that. Selling 2 of 4 is
    reported as 4/2, so the net is 2 and the close is partial. Net 0, including
    a 0/0 row after a full sell, is a full exit. A sold-side count near 0 is a
    hold to settlement even when the local close wrote a full-qty sell.
    """
    sold = max(0, int(sold_qty))
    stored = max(0, int(position_qty))
    held = str(side or "YES").upper()
    yes_c = no_c = None
    if settlement:
        yes_c = _stated_count(settlement, "yes_count_fp", "yes_count")
        no_c = _stated_count(settlement, "no_count_fp", "no_count")
    if yes_c is not None or no_c is not None:
        yes_n = float(yes_c or 0.0)
        no_n = float(no_c or 0.0)
        net = yes_n - no_n if held == "YES" else no_n - yes_n
        remaining = max(0, int(round(net)))
        bought = int(round(yes_n if held == "YES" else no_n))
        book_sold = no_n if held == "YES" else yes_n
        if remaining <= 0:
            kind = "full"
        elif book_sold <= 0.01:
            kind = "held"
        else:
            kind = "partial"
        return {
            "kind": kind,
            "remaining": remaining,
            "sold": sold,
            "original": bought,
        }
    if sold <= 0:
        kind = "held" if stored else "none"
        remaining = stored
    elif stored <= 0:
        kind = "full"
        remaining = 0
    else:
        kind = "partial"
        remaining = stored
    return {
        "kind": kind,
        "remaining": remaining,
        "sold": sold,
        "original": remaining + sold,
    }


def combine_partial_realized(position_realized: float, sale_realized: float, remainder_pnl: float) -> float:
    """Sale P&L lives on the sell trade when the position row was not updated."""
    pos = float(position_realized or 0.0)
    sale = float(sale_realized or 0.0)
    base = sale if abs(pos) <= 1e-9 else pos
    return base + float(remainder_pnl)


def remainder_settlement_pnl(
    *,
    remaining_qty: int,
    entry_cost: float,
    fees_paid: float,
    sold_qty: int,
    won: bool,
) -> float:
    """Payout on unsold contracts, minus their cost and their share of fees.

    ``remaining_qty`` is the position quantity after a partial sell. It is not
    the original size.
    """
    remaining = max(0, int(remaining_qty))
    sold = max(0, int(sold_qty))
    original = remaining + sold
    fee_remaining = float(fees_paid or 0.0) * (remaining / original) if original else 0.0
    payout = float(remaining) if won else 0.0
    return payout - float(entry_cost or 0.0) - fee_remaining


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
    fees = buy_fee + sell_fee
    # Sale proceeds and any settlement on the unsold contracts are both cash.
    # A full exit has no settlement payout. A hold-to-settlement has no sale.
    realized = proceeds + payout - notional - fees
    return {"realized_pnl": float(realized), "fees_paid": float(fees), "closed_by_sell": bool(closed_by_sell)}


def _first_number(row: dict, *keys: str) -> Optional[float]:
    for key in keys:
        raw = row.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def fill_count(fill: dict) -> float:
    return _first_number(fill, "count_fp", "count") or 0.0


def fill_fee(fill: dict) -> float:
    """Prefer ``fee_cost`` / ``fee``. Otherwise add taker and maker fee fields."""
    direct = _first_number(fill, "fee_cost", "fee", "fees")
    if direct is not None:
        return direct
    taker = _first_number(fill, "taker_fees_dollars") or 0.0
    maker = _first_number(fill, "maker_fees_dollars") or 0.0
    return taker + maker


def fill_yes_price(fill: dict) -> float:
    return _first_number(fill, "yes_price_dollars", "yes_price", "price") or 0.0


def fill_no_price(fill: dict) -> float:
    explicit = _first_number(fill, "no_price_dollars", "no_price")
    if explicit is not None and explicit > 0:
        return explicit
    yes = fill_yes_price(fill)
    if 0.0 < yes < 1.0:
        return 1.0 - yes
    return 0.0


def fill_role(fill: dict, held_side: str) -> str:
    """``entry`` or ``exit`` for the position side.

    Buying NO is reported as ``action=sell``, ``side=no``. That is an entry
    when the position is NO, and a stop-loss exit when the position is YES.
    """
    action = str(fill.get("action") or "").strip().lower()
    side = str(fill.get("side") or "").strip().lower()
    held = str(held_side or "YES").strip().upper()
    if held == "NO":
        if action == "sell" and side == "no":
            return "entry"
        if action == "buy" and side == "no":
            return "entry"
        if action in ("buy", "sell"):
            return "exit"
        return "ignore"
    if action == "buy" and side in ("yes", ""):
        return "entry"
    if action == "sell" or (action == "buy" and side == "no"):
        return "exit"
    return "ignore"


def fills_to_cashflows(fills: list, settlement: Optional[dict] = None, held_side: str = "") -> dict:
    """Sum Kalshi fills for one position side.

    Accepts the shortened names used in local exports and the API names
    ``yes_price_dollars``, ``no_price_dollars``, ``fee_cost``, and ``count_fp``.
    A YES stop-loss exit is ``sell`` / ``no`` and its YES price is the exit price.
    A NO entry is that same shape and is not an exit.
    """
    held = str(held_side or (settlement or {}).get("side") or "").strip().upper()
    if held not in ("YES", "NO"):
        actions = {str(row.get("action") or "").strip().lower() for row in fills or []}
        sides = {str(row.get("side") or "").strip().lower() for row in fills or []}
        held = "NO" if actions == {"sell"} and sides <= {"no"} else "YES"
    buy_notional = 0.0
    buy_fees = 0.0
    buy_count = 0.0
    sell_proceeds = 0.0
    sell_fees = 0.0
    sell_count = 0.0
    for fill in fills or []:
        count = fill_count(fill)
        fee = fill_fee(fill)
        if count <= 0:
            continue
        role = fill_role(fill, held)
        if role == "entry":
            price = fill_no_price(fill) if held == "NO" else fill_yes_price(fill)
            buy_count += count
            buy_notional += count * price
            buy_fees += fee
        elif role == "exit":
            price = fill_yes_price(fill) if held == "YES" else fill_no_price(fill)
            sell_count += count
            sell_proceeds += count * price
            sell_fees += fee
    result = str((settlement or {}).get("market_result") or (settlement or {}).get("result") or "").strip().lower()
    has_settlement = result in ("yes", "no", "scalar", "void")
    won = (held == "YES" and result == "yes") or (held == "NO" and result == "no")
    remaining = max(0.0, buy_count - sell_count)
    fully_exited = sell_count > 1e-9 and remaining <= 1e-6
    payout = 0.0
    if has_settlement and result in ("void", "scalar"):
        payout = settlement_revenue_dollars(settlement) or 0.0
    elif has_settlement and not fully_exited and won:
        payout = remaining if sell_count > 1e-9 else buy_count
        if settlement and settlement.get("payout") not in (None, "") and sell_count <= 1e-9:
            payout = float(settlement.get("payout") or payout)
    out = realized_from_cashflows(
        buy_notional=buy_notional,
        buy_fees=buy_fees,
        sell_proceeds=sell_proceeds,
        sell_fees=sell_fees,
        settlement_payout=payout,
        closed_by_sell=sell_count > 1e-9,
    )
    out["closed_by_sell"] = sell_count > 1e-9 and fully_exited
    out["partial_exit"] = sell_count > 1e-9 and not fully_exited
    out["has_settlement"] = has_settlement
    out["bookable"] = bool(fully_exited or has_settlement)
    out["side"] = held
    out["remaining_qty"] = remaining
    return out
