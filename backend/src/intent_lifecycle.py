"""Latest order status per client order id.

Health, the live hold, and the read-only export all use this so a historical
pending row is not treated as a current order. Nothing here deletes rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from src.util.datetimes import ensure_utc

UNRESOLVED_ORDER_STATUSES = frozenset({"pending", "ambiguous", "open_at_exchange"})
_ORDER_EVENTS = frozenset({"order_intent", "order_ack"})


def _value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _stamp(row: Any) -> float:
    raw = _value(row, "ts_utc")
    if isinstance(raw, datetime):
        parsed = ensure_utc(raw)
        return parsed.timestamp() if parsed is not None else float("-inf")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return float("-inf")
        aware = ensure_utc(parsed)
        return aware.timestamp() if aware is not None else float("-inf")
    return float("-inf")


def _seq(row: Any) -> int:
    try:
        return int(_value(row, "seq") or 0)
    except (TypeError, ValueError):
        return 0


def select_intent_lifecycle(rows) -> dict:
    """Group order intents by client order id and keep the newest status.

    ``current`` is still pending, ambiguous, or open at the exchange.
    A later fill or reconcile for the same id removes that id from ``current``
    without deleting the older pending row. Rows with no client order id stay
    in ``legacy_without_client_order_id`` and are not current orders.
    """
    ordered = sorted(rows or [], key=lambda row: (_stamp(row), _seq(row)))
    latest: dict[tuple[str, str, str], Any] = {}
    legacy = []
    historical_pending = 0
    for row in ordered:
        if str(_value(row, "event_type") or "") not in _ORDER_EVENTS:
            continue
        status = str(_value(row, "order_status") or "").strip().lower()
        if status == "pending":
            historical_pending += 1
        cid = str(_value(row, "client_order_id") or "").strip()
        if not cid:
            if status in UNRESOLVED_ORDER_STATUSES:
                legacy.append(row)
            continue
        key = (
            cid,
            str(_value(row, "market_id") or ""),
            str(_value(row, "action") or "").strip().lower(),
        )
        latest[key] = row
    current = [
        row
        for row in latest.values()
        if str(_value(row, "order_status") or "").strip().lower() in UNRESOLVED_ORDER_STATUSES
    ]
    return {
        "current": current,
        "historical_pending_rows": historical_pending,
        "legacy_without_client_order_id": legacy,
    }
