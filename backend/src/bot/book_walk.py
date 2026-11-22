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


def quote_blockers(
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
    original_ask: Optional[float] = None,
) -> list[str]:
    """Names of the checks this size fails at this fresh limit.

    Names stay stable so a skip can be counted from the activity log:
    raw edge, AI win, Kelly, model cost, bucket cap, event cap, price moved.
    """
    from src.bot.buy_retry import gates_still_pass_at_ask
    from src.decision_engine.history_sizing import order_covers_model_cost
    from src.decision_engine.strategy_gates import (
        all_in_entry_dollars,
        autonomous_buy_gate_failure,
        event_exposure_room,
        fee_inclusive_ev_dollars,
    )
    from src.decision_engine.strategy_math import (
        ai_win_prob_pct_on_buy_side,
        edge_pct_for_side,
    )

    reasons: list[str] = []
    qty = int(quantity)
    px = float(limit)
    if qty < 1 or not (0.0 < px < 1.0):
        reasons.append("empty/thin book")
        return reasons
    edge = edge_pct_for_side(side, int(ai_yes_pct), float(yes_ask), float(no_ask), float(yes_ask), float(no_ask))
    if edge + 1e-9 < float(min_edge):
        reasons.append("raw edge")
    if ai_win_prob_pct_on_buy_side(side, int(ai_yes_pct)) < int(min_ai):
        reasons.append("AI win")
    if float(min_gross or 0) > 1e-12 and (1.0 - px) + 1e-12 < float(min_gross):
        reasons.append("raw edge")
    gate = autonomous_buy_gate_failure(
        side=side,
        ai_yes_pct=int(ai_yes_pct),
        edge_pct=float(edge),
        entry_price_dollars=px,
    )
    if gate and "calibration" in gate.lower() and "calibration" not in reasons:
        reasons.append("calibration")
    elif gate and "raw edge" not in reasons and "AI win" not in reasons:
        reasons.append("raw edge")
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
    ) and not reasons:
        reasons.append("raw edge")
    from src.decision_engine.strategy_math import stake_dollars_for_edge, whole_contracts_for_stake

    allowed = whole_contracts_for_stake(
        stake_dollars_for_edge(float(balance), float(edge)), px, float(balance)
    )
    if allowed < 1 or qty > allowed:
        reasons.append("stake")
    all_in = all_in_entry_dollars(qty, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
    cost = float(all_in) if all_in is not None else qty * px
    if cost > float(balance) + 1e-9:
        reasons.append("stake")
    elif all_in is not None and not event_exposure_room(
        float(equity), float(open_event_cost), float(event_reserve), float(all_in)
    ):
        reasons.append("event cap")
    del win_probability, model_cost, fee_inclusive_ev_dollars, order_covers_model_cost
    if (
        original_ask is not None
        and abs(px - float(original_ask)) >= 0.01
        and reasons
        and "price moved" not in reasons
    ):
        reasons.append("price moved")
    unique: list[str] = []
    for name in reasons:
        if name not in unique:
            unique.append(name)
    return unique


def fresh_quote_skip_reasons(
    levels: list[tuple[float, float]],
    *,
    original_ask: Optional[float] = None,
    **kwargs,
) -> list[str]:
    """Why even one contract cannot be sent after a re-quote."""
    limit = limit_for_quantity(levels, 1)
    if limit is None:
        return ["empty/thin book"]
    side = str(kwargs.get("side") or "").upper()
    if side == "YES":
        kwargs["yes_ask"] = float(limit)
    elif side == "NO":
        kwargs["no_ask"] = float(limit)
    reasons = quote_blockers(quantity=1, limit=float(limit), original_ask=original_ask, **kwargs)
    if not reasons:
        return ["empty/thin book"]
    return reasons


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
    check are all recomputed at ``limit``. A higher ask sizes down; it does
    not by itself cancel a buy the raw gates still allow.
    """
    return not quote_blockers(
        quantity=quantity,
        limit=limit,
        side=side,
        ai_yes_pct=ai_yes_pct,
        yes_ask=yes_ask,
        no_ask=no_ask,
        min_edge=min_edge,
        min_ai=min_ai,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
        blend_ev_gate=blend_ev_gate,
        balance=balance,
        cap_fraction=cap_fraction,
        win_probability=win_probability,
        model_cost=model_cost,
        equity=equity,
        open_event_cost=open_event_cost,
        event_reserve=event_reserve,
        min_gross=min_gross,
    )
