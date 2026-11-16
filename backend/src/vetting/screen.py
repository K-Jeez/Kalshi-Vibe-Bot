"""Reject a market before the model when no side can become a buy.

The window is the same one the order uses: ask from 50¢ to 85¢, and gross
upside at least the configured floor. A thin book on both sides is already
rejected by the scan.
"""

from __future__ import annotations

from src.decision_engine.strategy_gates import MAX_ENTRY_PRICE_CENTS, MIN_ENTRY_PRICE_CENTS


def _liquid(bid: float, ask: float, spread: float, ask_size: float, max_spread: float, min_top_size: float) -> bool:
    if bid <= 0.0 and ask > 0.0 and ask < 0.95:
        return False
    if bid > 0.0 and ask > 0.0 and spread > max_spread:
        return False
    if ask > 0.0 and ask_size < min_top_size:
        return False
    return ask > 0.0


def buyable_leg(
    market: dict,
    *,
    min_upside: float,
    max_spread: float,
    min_top_size: float,
) -> str:
    """``YES`` or ``NO`` when that side is inside the buy window and liquid. Else ``""``."""
    from src.config import settings as app_settings

    floor = int(getattr(app_settings, "min_entry_price_cents", MIN_ENTRY_PRICE_CENTS)) / 100.0
    upside = max(0.0, float(min_upside or 0.0))
    ceiling = int(getattr(app_settings, "max_entry_price_cents", MAX_ENTRY_PRICE_CENTS)) / 100.0
    if upside > 1e-12:
        ceiling = min(ceiling, 1.0 - upside)
    for side, bid_key, ask_key, spread_key, size_key in (
        ("YES", "yes_bid", "yes_ask", "yes_spread", "yes_ask_size"),
        ("NO", "no_bid", "no_ask", "no_spread", "no_ask_size"),
    ):
        ask = float(market.get(ask_key) or 0.0)
        if not _liquid(
            float(market.get(bid_key) or 0.0),
            ask,
            float(market.get(spread_key) or 1.0),
            float(market.get(size_key) or 0.0),
            float(max_spread),
            float(min_top_size),
        ):
            continue
        if ask + 1e-12 < floor or ask > ceiling + 1e-12:
            continue
        return side
    return ""
