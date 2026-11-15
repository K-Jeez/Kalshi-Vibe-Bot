"""Fresh executable asks from the live book.

Kalshi's order book is bids only. A YES ask is one minus a NO bid, and a NO
ask is one minus a YES bid. The limit is the price where the whole-contract
size is actually resting.
"""

from __future__ import annotations

from typing import Optional

STALE_QUOTE_SEC = 10.0
FAST_CLOSE_QUOTE_SEC = 4.0
FAST_CLOSE_WINDOW_SEC = 3600.0


def quote_is_stale(age_seconds: Optional[float], seconds_to_close: Optional[float]) -> bool:
    """True when the captured quote is too old to send, including an unknown age."""
    window = FAST_CLOSE_QUOTE_SEC
    if seconds_to_close is None or float(seconds_to_close) > FAST_CLOSE_WINDOW_SEC:
        window = STALE_QUOTE_SEC
    if age_seconds is None:
        return True
    try:
        age = float(age_seconds)
    except (TypeError, ValueError):
        return True
    return age > window + 1e-9


def is_insufficient_resting_volume(error: object, status_code: object = None) -> bool:
    """FOK HTTP 409 for a book that could not fill the whole order."""
    text = str(error or "").lower()
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    if code != 409 and "409" not in text:
        return False
    return "insufficient resting" in text


def _levels(orderbook_json: object, key: str) -> list:
    if not isinstance(orderbook_json, dict):
        return []
    ob = orderbook_json.get("orderbook_fp") or orderbook_json.get("orderbook") or orderbook_json
    if not isinstance(ob, dict):
        return []
    rows = ob.get(key) or []
    return rows if isinstance(rows, list) else []


def ask_levels_for_side(orderbook_json: object, side: str) -> list[tuple[float, float]]:
    """Ask price and size, cheapest first, from the opposite bid ladder."""
    held = str(side or "").upper()
    key = "no_dollars" if held == "YES" else "yes_dollars" if held == "NO" else ""
    if not key:
        return []
    out: list[tuple[float, float]] = []
    for row in _levels(orderbook_json, key):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            bid = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError):
            continue
        ask = 1.0 - bid
        if size > 0 and 0.0 < ask < 1.0:
            out.append((ask, size))
    out.sort(key=lambda item: item[0])
    return out


def limit_for_quantity(levels: list[tuple[float, float]], quantity: int) -> Optional[float]:
    """Worst ask that still fills ``quantity`` whole contracts, or None."""
    need = float(max(0, int(quantity)))
    if need < 1:
        return None
    worst = None
    for ask, size in levels or []:
        take = min(need, float(size))
        if take <= 0:
            continue
        need -= take
        worst = float(ask)
        if need <= 1e-6:
            return min(0.99, worst)
    return None


def largest_qualifying_buy(
    levels: list[tuple[float, float]],
    wanted: int,
    qualifies,
) -> tuple[int, Optional[float]]:
    """Largest whole size from ``wanted`` down whose book price still qualifies."""
    for qty in range(max(0, int(wanted)), 0, -1):
        limit = limit_for_quantity(levels, qty)
        if limit is None:
            continue
        if qualifies(qty, float(limit)):
            return qty, float(limit)
    return 0, None


def size_still_fits(
    *,
    quantity: int,
    limit: float,
    side: str,
    ai_yes_pct: int,
    yes_ask: float,
    no_ask: float,
    min_edge: float,
    min_ai: int,
    fee_multiplier,
    fee_type,
    blend_ev_gate: bool,
    balance: float,
    cap_fraction: float,
    win_probability: float,
    model_cost: float,
    equity: float,
    open_event_cost: float,
    event_reserve: float,
    min_gross: float,
) -> bool:
    """True when this whole size at this fresh limit still clears the live gates.

    Quarter Kelly, the bucket cash cap, the event cap, and the model-cost
    check are all recomputed at ``limit``. A higher ask must size down.
    """
    from src.bot.buy_retry import gates_still_pass_at_ask
    from src.decision_engine.history_sizing import order_covers_model_cost
    from src.decision_engine.strategy_gates import (
        all_in_entry_dollars,
        event_exposure_room,
        fee_inclusive_ev_dollars,
        kelly_contract_cap_for_bankroll,
    )
    from src.decision_engine.strategy_math import kelly_contracts_for_order

    qty = int(quantity)
    px = float(limit)
    if qty < 1 or px <= 0 or px >= 1:
        return False
    if float(min_gross or 0) > 1e-12 and (1.0 - px) + 1e-12 < float(min_gross):
        return False
    if not gates_still_pass_at_ask(
        side=side,
        ai_yes_pct=int(ai_yes_pct),
        ask=px,
        yes_ask=float(yes_ask),
        no_ask=float(no_ask),
        min_edge=float(min_edge),
        min_ai=int(min_ai),
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
        blend_ev_gate=blend_ev_gate,
    ):
        return False
    cap_contracts = kelly_contract_cap_for_bankroll(
        float(balance),
        px,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
        bankroll_fraction=float(cap_fraction),
    )
    kelly_qty, _tag = kelly_contracts_for_order(
        float(balance),
        side,
        int(ai_yes_pct),
        float(yes_ask),
        float(no_ask),
        float(yes_ask),
        float(no_ask),
        per_contract_premium=px,
        max_kelly_contracts=cap_contracts,
        win_probability=float(win_probability),
    )
    if qty > int(kelly_qty):
        return False
    all_in = all_in_entry_dollars(qty, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
    if all_in is None or all_in > float(balance) + 1e-9:
        return False
    if not event_exposure_room(float(equity), float(open_event_cost), float(event_reserve), float(all_in)):
        return False
    ev = fee_inclusive_ev_dollars(
        float(win_probability),
        px,
        qty,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
    )
    return order_covers_model_cost(ev, float(model_cost or 0.0))
