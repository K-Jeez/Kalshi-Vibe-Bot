"""Autonomous buy guardrails (baked from closed-trade analysis; not exposed in Settings UI)."""

from __future__ import annotations

import math
from typing import Optional

from src.clients.kalshi_client import executable_buy_best_ask_dollars
from src.decision_engine.strategy_math import ai_win_prob_pct_on_buy_side, max_whole_contracts_for_cash

# Upper/lower bounds applied after user min edge / min AI (Settings + .env).
MAX_EDGE_TO_BUY_PCT = 22
# Favorites (e.g. 85% NO at 73¢) are allowed; extreme tail confidence is still blocked.
MAX_AI_WIN_PROB_BUY_SIDE_PCT = 90
MIN_ENTRY_PRICE_CENTS = 26
# Approved 2026-11-09 bundle. Quarter Kelly is applied in strategy_math.
# Per-entry cap is 2.5% of available cash including the estimated entry fee.
# 5-point edge, 60% win probability, and 80% stop-loss are not part of this cap.
KELLY_FRACTION = 0.25
MAX_KELLY_BANKROLL_PCT = 0.025
EVENT_EXPOSURE_CAP_FRACTION = 0.05
QUADRATIC_FEE_COEFFICIENT = 0.07

# Sports: stricter scan (volume) and exit grace only — min edge uses your Settings value as-is.
_SPORTS_MIN_VOLUME_FLOOR = 2000.0
_SPORTS_EXIT_GRACE_EXTRA_MINUTES = 5.0

# High AI % on mid-priced contracts historically lost large despite decent win rate.
_OVERCONF_AI_MIN_PCT = 75
_OVERCONF_ENTRY_CENTS_LO = 41
_OVERCONF_ENTRY_CENTS_HI = 65


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


def exit_grace_minutes_for_market(settings_grace_minutes: float, title: str, event_ticker: str = "") -> float:
    g = max(0.0, float(settings_grace_minutes))
    if is_sports_market_title(title, event_ticker):
        return g + _SPORTS_EXIT_GRACE_EXTRA_MINUTES
    return g


# GET /series/{series_ticker} documents these fee_type values.
_QUADRATIC_FEE_TYPES = frozenset({
    "quadratic",
    "quadratic_with_maker_fees",
    "quadratic_with_combo_maker_fees",
})

FEE_ESTIMATE_NOTE = (
    "When GET /series returns a quadratic fee_type and a fee_multiplier, the "
    "cap and expected-value check use 0.07 times that multiplier, rounded up "
    "to the next cent. That is a conservative estimate, not the billed fee. "
    "The exchange fill fee is stored separately. A missing or non-quadratic "
    "schedule does not count as a positive expected value, and it does not "
    "prove the 2.5% all-in cap."
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


def kelly_contract_cap_for_bankroll(
    bankroll: float,
    per_contract_premium: float,
    fee_multiplier: Optional[float] = None,
    fee_type: Optional[str] = None,
) -> int:
    """Largest whole-contract count whose estimated all-in cost fits in 2.5% of cash.

    Returns 0 when the series fee schedule is unknown. That is not a claim that
    one contract fits the cap.
    """
    br = float(bankroll)
    px = float(per_contract_premium)
    if not math.isfinite(br) or not math.isfinite(px) or br <= 0 or px <= 1e-12:
        return 0
    if all_in_entry_dollars(1, px, fee_multiplier=fee_multiplier, fee_type=fee_type) is None:
        return 0
    cash_cap = max_whole_contracts_for_cash(br, px)
    if cash_cap < 1:
        return 0
    budget = br * float(MAX_KELLY_BANKROLL_PCT)
    if all_in_entry_dollars(1, px, fee_multiplier=fee_multiplier, fee_type=fee_type) > budget + 1e-9:
        return 0
    lo, hi = 1, cash_cap
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        cost = all_in_entry_dollars(mid, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
        if cost is not None and cost <= budget + 1e-9:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def event_exposure_room(equity: float, open_cost_basis: float, pending_reserve: float, added_all_in: float) -> bool:
    """True when this buy keeps the event's cost basis within 5% of equity."""
    eq = float(equity)
    if not math.isfinite(eq) or eq <= 0:
        return False
    total = float(open_cost_basis) + float(pending_reserve) + float(added_all_in)
    return total <= eq * float(EVENT_EXPOSURE_CAP_FRACTION) + 1e-9


def either_side_can_buy_one_capped_contract(market: dict, bankroll: float) -> bool:
    """True when YES or NO can buy one contract under the estimated 2.5% all-in cap.

    Uses the native ask only. A missing ask is not replaced with the mid price.
    A missing series fee schedule returns False. That is not a claim that the
    cap was met. Model probability is not used.
    """
    schedule = fee_schedule_from_mapping(market)
    if not schedule["known"]:
        return False
    for side in ("YES", "NO"):
        ask = executable_buy_best_ask_dollars(market, side)
        if ask is None:
            continue
        if kelly_contract_cap_for_bankroll(
            float(bankroll),
            float(ask),
            fee_multiplier=schedule["fee_multiplier"],
            fee_type=schedule["fee_type"],
        ) >= 1:
            return True
    return False


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

    if edge > MAX_EDGE_TO_BUY_PCT + 1e-9:
        return (
            f"Skipped — edge {edge:.1f} pts exceeds autonomous cap ({MAX_EDGE_TO_BUY_PCT} pts; "
            "often model overconfidence)"
        )
    if ai_buy > MAX_AI_WIN_PROB_BUY_SIDE_PCT:
        return (
            f"Skipped — AI win prob on buy side {ai_buy}% exceeds autonomous cap "
            f"({MAX_AI_WIN_PROB_BUY_SIDE_PCT}%)"
        )
    if entry_c < MIN_ENTRY_PRICE_CENTS:
        return f"Skipped — entry {entry_c}¢ below autonomous floor ({MIN_ENTRY_PRICE_CENTS}¢)"
    if (
        ai_buy >= _OVERCONF_AI_MIN_PCT
        and _OVERCONF_ENTRY_CENTS_LO <= entry_c <= _OVERCONF_ENTRY_CENTS_HI
    ):
        return (
            f"Skipped — calibration guard: AI {ai_buy}% on {entry_c}¢ entry "
            "(mid-priced high-confidence bucket underperformed historically)"
        )
    return None
