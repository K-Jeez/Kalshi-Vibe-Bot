"""One reprice after a missed buy. No sleep. The watch list is in memory only."""

from __future__ import annotations

import time
from typing import Optional

MAX_RETRY_SLIPPAGE = 0.02
WATCH_TTL_SEC = 15 * 60

_watch_until: dict[str, float] = {}


def retry_limit(original_ask: float, fresh_ask: float) -> Optional[float]:
    """Fresh ask when it is within 2 cents of the first limit. Otherwise no retry."""
    original = float(original_ask)
    fresh = float(fresh_ask)
    if fresh <= 0 or fresh >= 1:
        return None
    if fresh > original + MAX_RETRY_SLIPPAGE + 1e-9:
        return None
    return fresh


def price_gap(original_ask: float, fresh_ask: float) -> float:
    return round(float(fresh_ask) - float(original_ask), 4)


def remember_miss(ticker: str, *, now: Optional[float] = None) -> None:
    stamp = time.monotonic() if now is None else float(now)
    _watch_until[str(ticker or "").strip().upper()] = stamp + WATCH_TTL_SEC


def miss_is_watched(ticker: str, *, now: Optional[float] = None) -> bool:
    key = str(ticker or "").strip().upper()
    until = _watch_until.get(key)
    if until is None:
        return False
    stamp = time.monotonic() if now is None else float(now)
    if stamp > until:
        _watch_until.pop(key, None)
        return False
    return True


def reset_for_tests() -> None:
    _watch_until.clear()


def gates_still_pass_at_ask(
    *,
    side: str,
    ai_yes_pct: int,
    ask: float,
    yes_ask: float,
    no_ask: float,
    min_edge: float,
    min_ai: int,
    fee_multiplier,
    fee_type,
) -> bool:
    """Fee-inclusive edge and expected value at the fresh ask."""
    from src.decision_engine.strategy_gates import autonomous_buy_gate_failure, fee_inclusive_ev_dollars
    from src.decision_engine.strategy_math import ai_win_prob_pct_on_buy_side, edge_pct_for_side

    edge = edge_pct_for_side(side, int(ai_yes_pct), yes_ask, no_ask, yes_ask, no_ask)
    if edge + 1e-9 < float(min_edge):
        return False
    win = ai_win_prob_pct_on_buy_side(side, int(ai_yes_pct))
    if win < int(min_ai):
        return False
    ev = fee_inclusive_ev_dollars(
        win / 100.0,
        float(ask),
        1,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
    )
    if ev is None or ev <= 0:
        return False
    if autonomous_buy_gate_failure(
        side=side,
        ai_yes_pct=int(ai_yes_pct),
        edge_pct=edge,
        entry_price_dollars=float(ask),
    ):
        return False
    return True


async def retry_buy_once(
    client,
    *,
    market_id: str,
    side: str,
    quantity: int,
    original_ask: float,
    ai_yes_pct: int,
    min_edge: float,
    min_ai: int,
    fee_multiplier,
    fee_type,
    client_order_id: Optional[str] = None,
    exchange_index: Optional[int] = None,
    balance: Optional[float] = None,
    persist_intent=None,
    fund=None,
    acknowledge=None,
) -> tuple[Optional[dict], float]:
    """Re-read the book and send one fill-or-kill if the gates still pass.

    Returns ``(order_or_none, price_gap)``. A miss is remembered on the watch list.
    The size is walked down the live depth and rechecked against the stake.
    """
    from src.bot.book_walk import ask_levels_for_side, largest_qualifying_buy, size_still_fits
    from src.clients.kalshi_client import executable_buy_best_ask_dollars
    from src.decision_engine.strategy_gates import all_in_entry_dollars

    fresh = await client.get_market(market_id)
    book = fresh if isinstance(fresh, dict) else {}
    ask = executable_buy_best_ask_dollars(book, side)
    if ask is None:
        remember_miss(market_id)
        return None, 0.0
    gap = price_gap(original_ask, float(ask))
    depth = None
    getter = getattr(client, "get_market_orderbook_fp", None)
    if getter is not None:
        depth = await getter(market_id)
    levels = ask_levels_for_side(depth, side)
    if not levels:
        levels = [(float(ask), float(max(1, int(quantity))))]
    held = str(side or "").upper()

    def _fits(qty, limit):
        if float(limit) > float(original_ask) + MAX_RETRY_SLIPPAGE + 1e-9:
            return False
        yes_ask = float(limit) if held == "YES" else float(book.get("yes_ask") or book.get("yes_price") or 0)
        no_ask = float(limit) if held == "NO" else float(book.get("no_ask") or book.get("no_price") or 0)
        if balance is None:
            return gates_still_pass_at_ask(
                side=side,
                ai_yes_pct=ai_yes_pct,
                ask=float(limit),
                yes_ask=yes_ask,
                no_ask=no_ask,
                min_edge=min_edge,
                min_ai=min_ai,
                fee_multiplier=fee_multiplier,
                fee_type=fee_type,
            )
        return size_still_fits(
            quantity=int(qty),
            limit=float(limit),
            side=side,
            ai_yes_pct=int(ai_yes_pct),
            yes_ask=yes_ask,
            no_ask=no_ask,
            min_edge=float(min_edge),
            min_ai=int(min_ai),
            fee_multiplier=fee_multiplier,
            fee_type=fee_type,
            balance=float(balance),
            min_gross=0.0,
        )

    send_qty, limit = largest_qualifying_buy(levels, int(quantity), _fits)
    if send_qty < 1 or limit is None:
        remember_miss(market_id)
        return None, gap
    if fund is not None:
        funded = await fund(
            float(
                all_in_entry_dollars(
                    send_qty,
                    float(limit),
                    fee_multiplier=fee_multiplier,
                    fee_type=fee_type,
                )
                or 0.0
            )
        )
        if not funded or not funded.get("ok"):
            remember_miss(market_id)
            return None, gap
    order_id = client_order_id
    if persist_intent is not None:
        order_id = persist_intent(float(limit), send_qty)
        if not order_id:
            remember_miss(market_id)
            return None, gap
    result = await client.place_buy_ioc_limit(
        market_id,
        side,
        send_qty,
        float(limit),
        client_order_id=order_id,
        exchange_index=exchange_index,
    )
    if isinstance(result, dict) and order_id and not result.get("client_order_id"):
        result = {**result, "client_order_id": order_id}
    if acknowledge is not None:
        acknowledge(result, order_id)
    return result, gap
