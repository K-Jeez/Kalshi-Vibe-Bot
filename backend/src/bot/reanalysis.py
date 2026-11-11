"""Skip a model call when the quote has not moved since the last analysis."""

from __future__ import annotations

from typing import Optional

UNCHANGED_PRICE_TOLERANCE = 0.005


def should_skip_unchanged_quote(
    *,
    analyzed_recently: bool,
    previous_yes: Optional[float],
    previous_no: Optional[float],
    yes_price: float,
    no_price: float,
) -> bool:
    """True when this market was analyzed inside the TTL and the price is the same.

    A moved price is analyzed again. A recent analysis with no stored quote is
    skipped, which is the previous cooldown.
    """
    if not analyzed_recently:
        return False
    if previous_yes is None or previous_no is None:
        return True
    return (
        abs(float(previous_yes) - float(yes_price)) <= UNCHANGED_PRICE_TOLERANCE
        and abs(float(previous_no) - float(no_price)) <= UNCHANGED_PRICE_TOLERANCE
    )
