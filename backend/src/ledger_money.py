"""Fixed-point money for contracts. Prices are dollars per contract, not floats we invent.

A YES buy pays ``price`` per contract and receives $1 if YES wins.
A NO buy pays ``price`` per contract and receives $1 if NO wins.
Fees are added only when the caller supplies them. This module does not
guess a missing NO-side fee.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Optional


_CENTS = Decimal("0.01")
_QTY = Decimal("0.01")


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def contracts(value) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(_QTY, rounding=ROUND_HALF_UP)


def whole_contracts(value) -> int:
    """Whole contracts, floored.

    A value within 1e-9 below the next integer counts as that integer.
    This is the same rule as ``floor(filled + 1e-9)`` and is the only
    whole-contract conversion the order path should use.
    """
    raw = Decimal(str(value if value is not None else 0))
    if raw <= 0:
        return 0
    bumped = raw + Decimal("1E-9")
    return int(bumped.to_integral_value(rounding=ROUND_FLOOR))


def entry_cost(quantity, price) -> Decimal:
    return money(contracts(quantity) * Decimal(str(price)))


def settlement_payout(quantity, won: bool) -> Decimal:
    """$1 per contract when the held side wins, otherwise zero."""
    if not won:
        return Decimal("0.00")
    return money(contracts(quantity) * Decimal("1"))


def realized_pnl(*, quantity, entry_price, won: bool, fees: Optional[Decimal] = None) -> Decimal:
    """Proceeds minus entry cost minus supplied fees.

    ``fees`` is optional. When it is omitted the result is marked by the
    caller as fee-incomplete. This function does not insert a default fee.
    """
    cost = entry_cost(quantity, entry_price)
    payout = settlement_payout(quantity, won)
    fee = money(fees) if fees is not None else Decimal("0.00")
    return money(payout - cost - fee)
