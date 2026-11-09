"""Unconfirmed buy exposure for the event cap and the open-position cap.

A missing price or a failed read blocks the new buy. It does not count as zero.
The current intent is excluded because its cost is passed in separately.
"""

from __future__ import annotations

from typing import Any, Optional

from src.clients.kalshi_client import (
    kalshi_order_remaining_contracts,
    resting_buy_collateral_estimate_usd,
)
from src.decision_engine.strategy_gates import all_in_entry_dollars


def _value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _same_text(left: object, right: object) -> bool:
    return str(left or "").strip().upper() == str(right or "").strip().upper() and bool(str(right or "").strip())


def _matches_event(row: Any, event_ticker: str, market_id: str) -> bool:
    if _same_text(_value(row, "event_ticker"), event_ticker):
        return True
    ticker = _value(row, "market_id") or _value(row, "ticker") or _value(row, "market_ticker")
    if _same_text(ticker, market_id):
        return True
    et = str(event_ticker or "").strip().upper()
    tk = str(ticker or "").strip().upper()
    return bool(et and tk and (tk == et or tk.startswith(et + "-")))


def _resting_buy_all_in(order: dict, *, fee_type: Optional[str], fee_multiplier: Optional[float]) -> Optional[float]:
    if str(order.get("status") or "").lower() != "resting":
        return 0.0
    if str(order.get("action") or "").lower() != "buy":
        return 0.0
    remaining = float(kalshi_order_remaining_contracts(order))
    if remaining <= 0:
        return 0.0
    if abs(remaining - round(remaining)) > 1e-6:
        return None
    premium = float(resting_buy_collateral_estimate_usd([order]))
    if premium <= 0:
        return None
    quantity = int(round(remaining))
    return all_in_entry_dollars(
        quantity,
        premium / quantity,
        fee_multiplier=fee_multiplier,
        fee_type=fee_type,
    )


def unconfirmed_buy_exposure(
    *,
    local_rows: Optional[list],
    resting_orders: Optional[list],
    resting_known: bool,
    event_ticker: str,
    market_id: str,
    exclude_client_order_id: Optional[str],
    fee_type: Optional[str] = None,
    fee_multiplier: Optional[float] = None,
) -> dict:
    """Return reserve dollars for this event and how many other buys are still open orders."""
    if local_rows is None:
        return {"reserve_dollars": None, "extra_orders": None, "reason": "local order log could not be read"}
    if not resting_known or resting_orders is None:
        return {"reserve_dollars": None, "extra_orders": None, "reason": "resting orders could not be read"}

    reserve = 0.0
    extra = 0
    skip_id = str(exclude_client_order_id or "").strip()
    seen_clients: set[str] = set()
    for row in local_rows:
        status = str(_value(row, "order_status") or "").strip().lower()
        action = str(_value(row, "action") or "").strip().lower()
        event_type = str(_value(row, "event_type") or "order_intent").strip().lower()
        if event_type not in ("order_intent", "order_ack") or action != "buy":
            continue
        cid = str(_value(row, "client_order_id") or "").strip()
        if cid in seen_clients:
            continue
        seen_clients.add(cid)
        if status not in ("pending", "ambiguous", "open_at_exchange"):
            continue
        if skip_id and cid == skip_id:
            continue
        quantity = _value(row, "quantity")
        price = _value(row, "limit_price")
        try:
            qty = int(quantity)
            px = float(price)
        except (TypeError, ValueError):
            qty, px = 0, 0.0
        if qty < 1 or px <= 0:
            return {
                "reserve_dollars": None,
                "extra_orders": None,
                "reason": "pending buy is missing a price or size",
            }
        extra += 1
        if _matches_event(row, event_ticker, market_id):
            cost = all_in_entry_dollars(qty, px, fee_multiplier=fee_multiplier, fee_type=fee_type)
            if cost is None:
                return {
                    "reserve_dollars": None,
                    "extra_orders": None,
                    "reason": "fee schedule unknown, so reserved capital is not a proven cap",
                }
            reserve += cost

    for order in resting_orders:
        if not isinstance(order, dict):
            return {"reserve_dollars": None, "extra_orders": None, "reason": "resting order record is unreadable"}
        if str(order.get("status") or "").lower() != "resting":
            continue
        if str(order.get("action") or "").lower() != "buy":
            continue
        cost = _resting_buy_all_in(order, fee_type=fee_type, fee_multiplier=fee_multiplier)
        if cost is None:
            return {
                "reserve_dollars": None,
                "extra_orders": None,
                "reason": "resting buy cannot be reserved without a known quadratic fee schedule",
            }
        if cost <= 0:
            continue
        extra += 1
        if _matches_event(order, event_ticker, market_id):
            reserve += cost

    return {"reserve_dollars": reserve, "extra_orders": extra, "reason": None}
