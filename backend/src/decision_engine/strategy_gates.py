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


def estimated_entry_fee_dollars(quantity: int, price: float) -> float:
    """Quadratic entry-fee estimate, rounded up to the next cent.

    Coefficient 0.07 is the general schedule. Series-specific multipliers are
    not fetched here. Rounding up keeps the cap and EV gate from understating
    the fee. The exchange fill fee, once known, is the accounting figure.
    """
    qty = int(quantity)
    px = float(price)
    if qty < 1 or not math.isfinite(px) or px <= 0 or px >= 1:
        return 0.0
    raw = QUADRATIC_FEE_COEFFICIENT * qty * px * (1.0 - px)
    cents = math.ceil(raw * 100.0 - 1e-9)
    return max(0.0, cents / 100.0)


def all_in_entry_dollars(quantity: int, price: float) -> float:
    """Premium plus one entry-fee estimate. The ask is not reduced by the spread again."""
    qty = int(quantity)
    px = float(price)
    if qty < 1 or px <= 0:
        return 0.0
    return qty * px + estimated_entry_fee_dollars(qty, px)


def fee_inclusive_ev_dollars(win_probability: float, ask: float, quantity: int = 1) -> float:
    """Expected dollars for ``quantity`` contracts after one entry fee.

    ``win_probability`` is the chance the bought side wins. Payout is $1 per
    winning contract. A non-positive result blocks the buy. The 5-point edge
    rule still applies separately.
    """
    qty = int(quantity)
    p = float(win_probability)
    px = float(ask)
    if qty < 1 or not math.isfinite(p) or not math.isfinite(px):
        return -1.0
    return p * qty - all_in_entry_dollars(qty, px)


def kelly_contract_cap_for_bankroll(bankroll: float, per_contract_premium: float) -> int:
    """Largest whole-contract count whose all-in entry cost fits in 2.5% of cash.

    When that budget cannot pay for one contract, return 0. The order is not
    enlarged to the rest of the cash balance.
    """
    br = float(bankroll)
    px = float(per_contract_premium)
    if not math.isfinite(br) or not math.isfinite(px) or br <= 0 or px <= 1e-12:
        return 0
    cash_cap = max_whole_contracts_for_cash(br, px)
    if cash_cap < 1:
        return 0
    budget = br * float(MAX_KELLY_BANKROLL_PCT)
    if all_in_entry_dollars(1, px) > budget + 1e-9:
        return 0
    lo, hi = 1, cash_cap
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if all_in_entry_dollars(mid, px) <= budget + 1e-9:
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
    """True when YES or NO can buy one contract under the existing 5% cash cap.

    Uses the native ask only. A missing ask is not replaced with the mid price.
    Model probability is not used. The 5% figure is the existing cap, not a new one.
    """
    for side in ("YES", "NO"):
        ask = executable_buy_best_ask_dollars(market, side)
        if ask is None:
            continue
        if kelly_contract_cap_for_bankroll(float(bankroll), float(ask)) >= 1:
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
