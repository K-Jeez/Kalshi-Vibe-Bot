"""Decide whether an unconfirmed client order id is safe to retry.

This does not call Kalshi. Callers pass the results of the documented reads:

- ``GET /portfolio/orders/{order_id}``
- ``GET /portfolio/orders?ticker=``
- ``GET /historical/orders?ticker=``
- ``GET /portfolio/fills`` filtered by ticker or order id
- ``GET /portfolio/positions``

A source that timed out, returned 5xx, or stopped before its cursor was empty
is ``unknown``. Time since the intent was written is not an input.
"""

from __future__ import annotations

from typing import Optional


def _matches_order(row: dict, client_order_id: str, exchange_order_id: str) -> bool:
    if not isinstance(row, dict):
        return False
    cid = str(row.get("client_order_id") or "").strip()
    oid = str(row.get("order_id") or row.get("id") or "").strip()
    if client_order_id and cid == client_order_id:
        return True
    return bool(exchange_order_id and oid == exchange_order_id)


def _source_rows(source: Optional[dict]) -> tuple[str, list]:
    if not isinstance(source, dict):
        return "unknown", []
    state = str(source.get("state") or "unknown")
    rows = source.get("rows")
    if state != "complete" or not isinstance(rows, list):
        return "unknown", []
    return "complete", rows


def verdict_from_sources(
    *,
    client_order_id: str,
    exchange_order_id: str = "",
    get_order: Optional[dict] = None,
    live_orders: Optional[dict] = None,
    historical_orders: Optional[dict] = None,
    fills: Optional[dict] = None,
    positions: Optional[dict] = None,
) -> dict:
    """Return ``found``, ``not_found``, or ``unknown``.

    ``not_found`` is only when every identity source finished and none of them
    contain this client order id or exchange order id. A position row has no
    client order id, so it cannot by itself prove the attempt.
    """
    cid = (client_order_id or "").strip()
    oid = (exchange_order_id or "").strip()
    if not cid and not oid:
        return {"verdict": "unknown", "reason": "no client or exchange order id to reconcile"}

    if not isinstance(positions, dict) or positions.get("state") != "complete":
        return {"verdict": "unknown", "reason": "positions read did not finish"}

    looked = get_order if isinstance(get_order, dict) else {"state": "not_found" if not oid else "unknown"}
    looked_state = str(looked.get("state") or "unknown")
    if oid and looked_state == "unknown":
        return {"verdict": "unknown", "reason": "get order did not finish"}
    if looked_state == "found" and _matches_order(looked.get("order") or {}, cid, oid):
        return {"verdict": "found", "reason": "GET /portfolio/orders/{order_id} returned this order"}

    for label, source in (("live orders", live_orders), ("historical orders", historical_orders), ("fills", fills)):
        state, rows = _source_rows(source)
        if state != "complete":
            return {"verdict": "unknown", "reason": f"{label} read did not finish"}
        for row in rows:
            if label == "fills":
                fill_oid = str((row or {}).get("order_id") or "").strip()
                if oid and fill_oid == oid:
                    return {"verdict": "found", "reason": "a fill references this exchange order id"}
                if not oid and rows:
                    return {
                        "verdict": "unknown",
                        "reason": "fills exist for this ticker but none can be tied to the client order id",
                    }
            elif _matches_order(row, cid, oid):
                return {"verdict": "found", "reason": f"{label} include this order"}

    if oid and looked_state != "not_found":
        return {"verdict": "unknown", "reason": "exchange order id was not confirmed absent"}
    return {
        "verdict": "not_found",
        "reason": "live orders, historical orders, and fills finished with no matching id",
    }
