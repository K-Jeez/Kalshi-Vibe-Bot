"""Edge and whole-contract sizing for binary Kalshi contracts (prices as 0–1 dollars)."""

from __future__ import annotations

import math
from typing import Optional, Tuple

def _clamp_unit(x: float) -> float:
    if not math.isfinite(x):
        return 0.0
    return max(0.0, min(1.0, float(x)))


def executable_asks(
    yes_ask: Optional[float],
    no_ask: Optional[float],
    yes_mid: float,
    no_mid: float,
) -> Tuple[float, float]:
    """Best executable buy prices; fall back to mids when ask missing."""
    ym = _clamp_unit(float(yes_mid or 0.5))
    nm = _clamp_unit(float(no_mid or 0.5))
    ya = float(yes_ask) if yes_ask is not None and float(yes_ask) > 0 else ym
    na = float(no_ask) if no_ask is not None and float(no_ask) > 0 else nm
    return _clamp_unit(ya), _clamp_unit(na)


def ai_yes_probability(ai_yes_pct: int) -> float:
    return max(0.0, min(1.0, float(int(ai_yes_pct)) / 100.0))


def market_implied_pct_for_side(side: str, yes_ask: Optional[float], no_ask: Optional[float], yes_mid: float, no_mid: float) -> int:
    ya, na = executable_asks(yes_ask, no_ask, yes_mid, no_mid)
    v = (ya * 100.0) if str(side).upper() == "YES" else (na * 100.0)
    return int(max(0, min(100, round(v))))


def edge_pct_for_side(
    side: str,
    ai_yes_pct: int,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    yes_mid: float,
    no_mid: float,
) -> float:
    """Edge in percentage points: AI win prob for that side minus market-implied (ask) for that side."""
    p_yes = ai_yes_probability(ai_yes_pct)
    ya, na = executable_asks(yes_ask, no_ask, yes_mid, no_mid)
    if str(side).upper() == "YES":
        return p_yes * 100.0 - ya * 100.0
    p_no = 1.0 - p_yes
    return p_no * 100.0 - na * 100.0


def stake_dollars_for_edge(cash: float, edge_pct: float) -> float:
    """Stake is the edge, in points, as a percent of available cash.

    A positive FLAT_STAKE_DOLLARS replaces that with a fixed dollar amount.
    There is no extra percent cap. The order is still limited to available cash
    and to whole contracts.
    """
    from src.config import settings as app_settings

    flat = float(getattr(app_settings, "flat_stake_dollars", 0) or 0)
    if flat > 0:
        return flat
    return max(0.0, float(cash)) * max(0.0, float(edge_pct)) / 100.0


def whole_contracts_for_stake(stake_dollars: float, price: float, cash: float) -> int:
    """How many whole contracts a flat dollar stake buys. Never rounds up to one."""
    px = float(price)
    if not math.isfinite(px) or px <= 1e-12:
        return 0
    wanted = int(math.floor(float(stake_dollars) / px + 1e-12))
    affordable = int(math.floor(max(0.0, float(cash)) / px + 1e-12))
    return max(0, min(wanted, affordable))


def max_whole_contracts_for_cash(bankroll: float, per_contract_premium: float) -> int:
    """Largest whole-contract count affordable at ``per_contract_premium`` (premium $/contract, 0–1)."""
    br = float(bankroll)
    px = float(per_contract_premium)
    if not math.isfinite(br) or not math.isfinite(px) or br <= 0 or px <= 1e-12:
        return 0
    return int(math.floor(br / px + 1e-12))


def derive_buy_side(
    ai_yes_pct: int,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    yes_mid: float,
    no_mid: float,
) -> str:
    """YES or NO from P(YES) versus the executable asks.

    The larger edge wins. A model SKIP is not an input. The server gates
    still decide whether that side is a buy.
    """
    edge_yes = edge_pct_for_side("YES", ai_yes_pct, yes_ask, no_ask, yes_mid, no_mid)
    edge_no = edge_pct_for_side("NO", ai_yes_pct, yes_ask, no_ask, yes_mid, no_mid)
    return "YES" if edge_yes >= edge_no else "NO"


def ai_win_prob_pct_on_buy_side(side: str, ai_yes_pct: int) -> int:
    """AI P(win) for the purchased contract side (YES leg or NO leg), 0–100 integer."""
    y = int(max(0, min(100, int(ai_yes_pct))))
    return y if str(side).upper() == "YES" else 100 - y


def effective_buy_gate_thresholds(
    *,
    side: str,
    ai_yes_pct: int,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    yes_mid: float,
    no_mid: float,
    min_edge_base: float,
    min_ai_win_prob_base: int,
) -> Tuple[float, int, bool]:
    """Return the Settings floors unchanged. The third value stays false."""
    del side, ai_yes_pct, yes_ask, no_ask, yes_mid, no_mid
    return float(min_edge_base), int(min_ai_win_prob_base), False
