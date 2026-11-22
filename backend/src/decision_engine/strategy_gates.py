"""Autonomous buy guardrails (baked from closed-trade analysis; not exposed in Settings UI)."""

from __future__ import annotations

import math
from typing import Optional

from src.clients.kalshi_client import executable_buy_best_ask_dollars
from src.decision_engine.strategy_math import ai_win_prob_pct_on_buy_side

# Upper/lower bounds applied after user min edge / min AI (Settings + .env).
MAX_EDGE_TO_BUY_PCT = 22
# Off. A cap belongs on the market price (the 50–85¢ window), not on the model's probability.
MAX_AI_WIN_PROB_BUY_SIDE_PCT = 100
MIN_ENTRY_PRICE_CENTS = 50
MAX_ENTRY_PRICE_CENTS = 85
QUADRATIC_FEE_COEFFICIENT = 0.07

# Sports: stricter scan volume only. Min edge uses the Settings value as-is.
_SPORTS_MIN_VOLUME_FLOOR = 2000.0

def is_sports_market_title(title: str, event_ticker: str = "") -> bool:
    t = f"{title or ''} {event_ticker or ''}".lower()
    return any(
        k in t
        for k in (
            " vs ",
            "total goals",
            "total maps",
            "btts",
            "both teams to score",
            "spread",
            "moneyline",
        )
    )


def effective_scan_min_volume(base_min_volume: float, title: str, event_ticker: str = "") -> float:
    base = max(0.0, float(base_min_volume))
    if is_sports_market_title(title, event_ticker):
        return max(base, _SPORTS_MIN_VOLUME_FLOOR)
    return base


def effective_min_edge_for_market(min_edge_base: float, title: str, event_ticker: str = "") -> float:
    """Return the user-configured min edge (Settings / ``MIN_EDGE_TO_BUY_PCT``); unchanged by market type."""
    return max(0.0, float(min_edge_base))


# GET /series/{series_ticker} documents these fee_type values.
_QUADRATIC_FEE_TYPES = frozenset({
    "quadratic",
    "quadratic_with_maker_fees",
    "quadratic_with_combo_maker_fees",
})

FEE_ESTIMATE_NOTE = (
    "When GET /series returns a quadratic fee_type and a fee_multiplier, the "
    "order check uses 0.07 times that multiplier, rounded up to the next cent. "
    "That is a conservative estimate, not the billed fee. The exchange fill "
    "fee is stored separately. A missing or non-quadratic schedule does not "
    "count as a positive expected value."
)


def fee_schedule_from_mapping(source: object) -> dict:
    """Read ``fee_type`` and ``fee_multiplier`` from a series or market mapping.

    The series payload shape is ``{"series": {"fee_type", "fee_multiplier"}}``.
    A flat schedule is not given a guessed dollar amount.
    """
    if not isinstance(source, dict):
        return {"known": False, "reason": "no fee metadata"}
    body = source.get("series") if isinstance(source.get("series"), dict) else source
    fee_type = str(body.get("fee_type") or "").strip().lower()
    if fee_type not in _QUADRATIC_FEE_TYPES:
        return {
            "known": False,
            "fee_type": fee_type or None,
            "reason": "fee schedule is not a documented quadratic type",
        }
    raw_mult = body.get("fee_multiplier")
    try:
        mult = float(raw_mult)
    except (TypeError, ValueError):
        return {"known": False, "fee_type": fee_type, "reason": "fee_multiplier missing"}
    if not math.isfinite(mult) or mult < 0:
        return {"known": False, "fee_type": fee_type, "reason": "fee_multiplier invalid"}
    return {
        "known": True,
        "fee_type": fee_type,
        "fee_multiplier": mult,
        "estimate": True,
    }


def estimated_entry_fee_dollars(
    quantity: int,
    price: float,
    fee_multiplier: Optional[float] = None,
    fee_type: Optional[str] = None,
) -> Optional[float]:
    """Conservative entry-fee estimate, or None when the series schedule is unknown."""
    schedule = fee_schedule_from_mapping({"fee_type": fee_type, "fee_multiplier": fee_multiplier})
    if not schedule["known"]:
        return None
    qty = int(quantity)
    px = float(price)
    mult = float(schedule["fee_multiplier"])
    if qty < 1 or not math.isfinite(px) or px <= 0 or px >= 1:
        return None
    raw = QUADRATIC_FEE_COEFFICIENT * mult * qty * px * (1.0 - px)
    cents = math.ceil(raw * 100.0 - 1e-9)
    return max(0.0, cents / 100.0)


def all_in_entry_dollars(
    quantity: int,
    price: float,
    fee_multiplier: Optional[float] = None,
    fee_type: Optional[str] = None,
) -> Optional[float]:
    """Premium plus one estimated entry fee. None means the cap cannot be checked."""
    qty = int(quantity)
    px = float(price)
    if qty < 1 or px <= 0:
        return None
    fee = estimated_entry_fee_dollars(qty, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
    if fee is None:
        return None
    return qty * px + fee


def fee_inclusive_ev_dollars(
    win_probability: float,
    ask: float,
    quantity: int = 1,
    fee_multiplier: Optional[float] = None,
    fee_type: Optional[str] = None,
) -> Optional[float]:
    """Estimated expected dollars, or None when the fee schedule is not known.

    None is not a positive result. The 5-point edge rule still applies separately.
    """
    qty = int(quantity)
    p = float(win_probability)
    px = float(ask)
    if qty < 1 or not math.isfinite(p) or not math.isfinite(px):
        return None
    cost = all_in_entry_dollars(qty, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
    if cost is None:
        return None
    return p * qty - cost


def _side_entry_quote(market: dict, side: str, schedule: dict) -> dict:
    ask = executable_buy_best_ask_dollars(market, side)
    if ask is None:
        return {"side": side, "ask": None, "fee": None, "all_in": None}
    fee = estimated_entry_fee_dollars(
        1,
        float(ask),
        fee_multiplier=schedule["fee_multiplier"],
        fee_type=schedule["fee_type"],
    )
    cost = all_in_entry_dollars(
        1,
        float(ask),
        fee_multiplier=schedule["fee_multiplier"],
        fee_type=schedule["fee_type"],
    )
    return {"side": side, "ask": float(ask), "fee": fee, "all_in": cost}


def premodel_entry_block(market: dict, bankroll: float) -> Optional[dict]:
    """Why one contract cannot be bought with the cash on hand, or None when it can.

    An unknown series fee is ``fee_unknown``. Model probability is not used.
    """
    schedule = fee_schedule_from_mapping(market)
    ticker = str(market.get("id") or "")
    series = str(market.get("series_ticker") or "").strip() or ticker or "unknown"
    if not schedule["known"]:
        return {
            "kind": "fee_unknown",
            "series": series,
            "ticker": ticker,
            "message": f"fee schedule unknown for series {series} — skipping",
        }
    sides = [_side_entry_quote(market, side, schedule) for side in ("YES", "NO")]
    priced = [row for row in sides if row["all_in"] is not None]
    if priced and all(float(row["all_in"]) > float(bankroll) + 1e-9 for row in priced):
        return {
            "kind": "cash_short",
            "series": series,
            "ticker": ticker,
            "sides": sides,
            "message": (
                f"Skip model {ticker} — available cash ${float(bankroll):.2f} "
                "cannot buy one contract at the ask including the estimated fee"
            ),
        }
    if any(row["all_in"] is not None and float(row["all_in"]) <= float(bankroll) + 1e-9 for row in sides):
        return None
    return {
        "kind": "cash_short",
        "series": series,
        "ticker": ticker,
        "sides": sides,
        "message": (
            f"Skip model {ticker} — available cash ${float(bankroll):.2f} "
            "cannot buy one contract at the ask including the estimated fee"
        ),
    }


def either_side_can_buy_one_contract(market: dict, bankroll: float) -> bool:
    """True when YES or NO can buy one whole contract with the cash on hand."""
    return premodel_entry_block(market, bankroll) is None


def autonomous_buy_gate_failure(
    *,
    side: str,
    ai_yes_pct: int,
    edge_pct: float,
    entry_price_dollars: float,
) -> Optional[str]:
    """Return a skip summary when autonomous guardrails reject the buy; else ``None``."""
    ai_buy = int(ai_win_prob_pct_on_buy_side(side, ai_yes_pct))
    edge = float(edge_pct)
    entry_c = int(max(0, min(100, round(float(entry_price_dollars) * 100.0))))

    from src.config import settings as app_settings

    max_edge = float(getattr(app_settings, "max_edge_to_buy_pct", MAX_EDGE_TO_BUY_PCT))
    if max_edge > 0 and edge > max_edge + 1e-9:
        return (
            f"Skipped — the model’s edge is {edge:.0f} points, above the "
            f"{max_edge:.0f}-point safety cap"
        )
    low = int(getattr(app_settings, "min_entry_price_cents", MIN_ENTRY_PRICE_CENTS))
    high = int(getattr(app_settings, "max_entry_price_cents", MAX_ENTRY_PRICE_CENTS))
    if entry_c < low or entry_c > high:
        return (
            f"Skipped — price is {entry_c}¢. The bot only buys between {low}¢ and {high}¢."
        )
    del ai_buy
    return None
