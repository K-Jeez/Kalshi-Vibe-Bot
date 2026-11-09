"""Classify an order result without calling Kalshi.

Model SKIP stays separate from a buy that was blocked, rejected, or unfilled.
"""

from __future__ import annotations

from typing import Optional

from src.ledger_money import whole_contracts


def classify_routing_error(detail: str, status_code: Optional[int] = None) -> str:
    """Label a Kalshi error string. Does not decide why the account was rejected."""
    text = (detail or "").lower()
    code = int(status_code or 0)
    if code == 404 and "exchange user not found" in text:
        return "exchange_shard_user"
    if "exchange sharding" in text or "exchange user not found" in text:
        return "exchange_shard_user"
    if code == 404:
        return "not_found"
    if code == 429 or "429" in text:
        return "rate_limited"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "order_error"


def buy_fill_status(*, filled: float, requested: int, time_in_force: str) -> str:
    """Whole-contract outcome. IOC with a zero fill is not a resting order."""
    qty = int(requested or 0)
    whole = whole_contracts(filled)
    if whole < 1:
        if filled > 0:
            return "fractional_fill"
        return "zero_fill"
    if qty > 0 and whole < qty:
        return "partial"
    return "executed"


def ioc_zero_fill_is_not_resting(time_in_force: str, filled: float) -> bool:
    tif = (time_in_force or "").strip().lower()
    return tif == "immediate_or_cancel" and float(filled) <= 0


def order_status_after_submit(time_in_force: str, filled: float) -> str:
    """IOC with no fill is canceled. A positive fill is executed."""
    if float(filled) > 0:
        return "executed"
    if ioc_zero_fill_is_not_resting(time_in_force, filled):
        return "canceled"
    return "open"
