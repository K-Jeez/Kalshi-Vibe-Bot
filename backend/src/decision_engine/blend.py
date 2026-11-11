"""Shrink the model toward the market for size and expected value only.

The market number is the fee-free mid: the average of the YES bid and YES ask.
A missing bid uses the executable ask (ask-implied). The NO probability is one
minus that YES mid, so both sides share one number.

``p_blend = w * p_model + (1 - w) * p_market`` with ``w`` from
``VIBE_MODEL_BLEND_WEIGHT`` (default 0.25). Entry gates keep the raw model.
"""

from __future__ import annotations

from typing import Optional

from src.config import settings


def model_blend_weight() -> float:
    try:
        weight = float(getattr(settings, "vibe_model_blend_weight", 0.25))
    except (TypeError, ValueError):
        weight = 0.25
    if weight != weight:
        return 0.25
    return max(0.0, min(1.0, weight))


def fee_free_yes_mid(
    *,
    yes_bid: Optional[float],
    yes_ask: Optional[float],
    yes_price: Optional[float] = None,
) -> tuple[float, str]:
    """YES probability in 0–1, and which quote produced it.

    ``fee-free mid`` when bid and ask are both present. ``ask-implied`` when
    the bid is missing and the ask (or the stored YES price) is used.
    """
    bid = float(yes_bid) if yes_bid is not None else 0.0
    ask = float(yes_ask) if yes_ask is not None else 0.0
    if 0.0 < bid < 1.0 and 0.0 < ask < 1.0:
        return max(0.0, min(1.0, (bid + ask) / 2.0)), "fee-free mid"
    if 0.0 < ask < 1.0:
        return ask, "ask-implied"
    price = float(yes_price or 0.0)
    if 0.0 < price < 1.0:
        return price, "ask-implied"
    return 0.5, "ask-implied"


def blend_yes_probability(model_yes_pct: int, market_yes: float, weight: Optional[float] = None) -> float:
    w = model_blend_weight() if weight is None else max(0.0, min(1.0, float(weight)))
    model = max(0.0, min(1.0, float(int(model_yes_pct)) / 100.0))
    market = max(0.0, min(1.0, float(market_yes)))
    return w * model + (1.0 - w) * market


def blend_snapshot(
    *,
    side: str,
    model_yes_pct: int,
    yes_bid: Optional[float],
    yes_ask: Optional[float],
    yes_price: Optional[float] = None,
    weight: Optional[float] = None,
) -> dict:
    """Raw model, market mid, and the blended win probability for ``side``."""
    market_yes, source = fee_free_yes_mid(yes_bid=yes_bid, yes_ask=yes_ask, yes_price=yes_price)
    w = model_blend_weight() if weight is None else max(0.0, min(1.0, float(weight)))
    blended_yes = blend_yes_probability(model_yes_pct, market_yes, w)
    held = str(side or "").upper()
    win = blended_yes if held == "YES" else (1.0 - blended_yes if held == "NO" else blended_yes)
    return {
        "weight": w,
        "market_source": source,
        "market_yes": market_yes,
        "blended_yes": blended_yes,
        "win_probability": max(0.0, min(1.0, win)),
        "blended_yes_pct": round(blended_yes * 100.0, 1),
        "market_yes_pct": round(market_yes * 100.0, 1),
        "blended_side_pct": round(max(0.0, min(1.0, win)) * 100.0, 1),
    }


def stamp_blend(decision: dict, snap: dict) -> None:
    """Keep the raw model fields and add the blend beside them."""
    decision["blend_weight"] = snap["weight"]
    decision["market_mid_yes_pct"] = snap["market_yes_pct"]
    decision["blended_probability_yes_pct"] = snap["blended_yes_pct"]
    decision["blended_win_probability"] = snap["win_probability"]
    blob = decision.get("xai_analysis")
    if isinstance(blob, dict):
        blob["blend_weight"] = snap["weight"]
        blob["blend_market"] = snap["market_source"]
        blob["market_mid_yes_pct"] = snap["market_yes_pct"]
        blob["blended_probability_yes_pct"] = snap["blended_yes_pct"]
        blob["blended_side_pct"] = snap["blended_side_pct"]
