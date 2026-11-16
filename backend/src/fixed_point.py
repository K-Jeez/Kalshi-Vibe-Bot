"""Kalshi fixed-point contract counts.

Responses are strings with up to two decimal places, such as ``"10.00"`` or
``"2.50"``. A float is not accepted: converting it would invent precision.
An integer is a whole-contract request count, which the create-order schema
allows. A fractional reported count is kept exact. It is not floored into a
whole contract.
"""

from __future__ import annotations

import re
from decimal import Decimal

_COUNT = re.compile(r"[+-]?\d+(\.\d{1,2})?")


class FixedPointCountError(ValueError):
    """The value is not a documented fixed-point contract count."""


def parse_fixed_point_count(raw: object) -> Decimal:
    if isinstance(raw, bool) or isinstance(raw, float) or raw is None:
        raise FixedPointCountError("fixed-point count must be a string or a whole integer")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or _COUNT.fullmatch(text) is None:
            raise FixedPointCountError("fixed-point count is not a 0-2 decimal contract string")
        value = Decimal(text)
    else:
        raise FixedPointCountError("fixed-point count must be a string or a whole integer")
    return value


def is_whole_count(value: Decimal) -> bool:
    return value == value.to_integral_value()


def require_whole_submit_count(raw: object) -> int:
    """Count the bot may send. Fractional and float values are refused."""
    value = parse_fixed_point_count(raw)
    if value < 1 or not is_whole_count(value):
        raise FixedPointCountError("order count must be a positive whole contract")
    return int(value)


def format_count_fp(value: Decimal) -> str:
    """Exact fixed-point text. Does not round a fractional count to a whole contract."""
    quantized = value.quantize(Decimal("0.01"))
    if quantized != value:
        return format(value, "f")
    return f"{quantized:.2f}"
