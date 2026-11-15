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
