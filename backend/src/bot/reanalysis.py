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
    """Skip during the cooldown, and after it when the quote moved by half a cent or less.

    ``analyzed_recently`` is the cooldown window. A missing stored quote still
    skips inside that window. After the window, a missing quote is analyzed.
    """
    if analyzed_recently:
        return True
    if previous_yes is None or previous_no is None:
        return False
    return (
        abs(float(previous_yes) - float(yes_price)) <= UNCHANGED_PRICE_TOLERANCE
        and abs(float(previous_no) - float(no_price)) <= UNCHANGED_PRICE_TOLERANCE
    )
