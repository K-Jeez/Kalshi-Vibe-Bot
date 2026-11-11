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

from src.bot.fill_bookkeeping import (
    apply_cumulative_fill,
    cumulative_exchange_fees,
    normalize_order_action,
    normalize_order_side,
)


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


VISIBILITY_LAG_SEC = 45


def _matched_order(verdict_inputs: dict, client_order_id: str, exchange_order_id: str) -> Optional[dict]:
    looked = verdict_inputs.get("get_order") or {}
    if str(looked.get("state")) == "found" and _matches_order(looked.get("order") or {}, client_order_id, exchange_order_id):
        return looked.get("order") or {}
    for key in ("live_orders", "historical_orders"):
        source = verdict_inputs.get(key) or {}
        for row in source.get("rows") or []:
            if _matches_order(row, client_order_id, exchange_order_id):
                return row
    return None


async def reconcile_open_intents(
    client,
    session_factory,
    *,
    market_id: Optional[str] = None,
    action: Optional[str] = None,
    now=None,
    visibility_lag_sec: float = VISIBILITY_LAG_SEC,
) -> list[dict]:
    """Production reconcile pass. Loads unresolved intents and reads the exchange.

    ``client.collect_reconcile_sources`` performs the documented paginated reads.
    This function does not send an order. A ``not_found`` result is applied only
    after the visibility lag and only when all four reads finished. ``found``
    updates ledger facts and clears the hold only when the order is terminal
    and any whole-contract fill has been recorded once.
    """
    from datetime import datetime, timezone

    from src.activity_log import list_unresolved_intents, mark_intent_reconciled, record_activity
    from src.ops_status import note_unresolved

    current = now or datetime.now(timezone.utc)
    intents = list_unresolved_intents(
        session_factory=session_factory,
        market_id=market_id,
        action=action,
    )
    if intents is None:
        note_unresolved("Order intent log could not be read. Unconfirmed orders stay reserved.")
        return [{"verdict": "unknown", "reason": "intent log unreadable"}]

    results = []
    for intent in intents:
        cid = str(intent.get("client_order_id") or "")
        oid = str(intent.get("exchange_order_id") or "")
        ticker = str(intent.get("market_id") or "")
        try:
            sources = await client.collect_reconcile_sources(ticker=ticker, exchange_order_id=oid or None)
        except Exception:
            sources = None
        if not isinstance(sources, dict):
            note_unresolved(f"Reconcile read failed for {ticker or cid}. The order stays reserved.")
            results.append({"client_order_id": cid, "verdict": "unknown", "reason": "reconcile read failed"})
            continue
        verdict = verdict_from_sources(
            client_order_id=cid,
            exchange_order_id=oid,
            get_order=sources.get("get_order"),
            live_orders=sources.get("live_orders"),
            historical_orders=sources.get("historical_orders"),
            fills=sources.get("fills"),
            positions=sources.get("positions"),
        )
        if verdict["verdict"] == "not_found":
            started = intent.get("ts_utc")
            if started is None or not hasattr(started, "timestamp"):
                verdict = {"verdict": "unknown", "reason": "intent has no timestamp, so absence is not definitive"}
            else:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                age = (current - started).total_seconds()
                if age < float(visibility_lag_sec):
                    verdict = {
                        "verdict": "unknown",
                        "reason": "order is inside the visibility lag, so absence is not definitive",
                    }
        if verdict["verdict"] == "unknown":
            note_unresolved(
                f"Unconfirmed {intent.get('action') or 'order'} {ticker} client_order_id={cid} stays reserved. "
                f"{verdict.get('reason')}"
            )
            results.append({"client_order_id": cid, "verdict": "unknown", "reason": verdict.get("reason")})
            continue
        if verdict["verdict"] == "not_found":
            mark_intent_reconciled(
                cid,
                market_id=ticker,
                action=str(intent.get("action") or ""),
                order_status="reconciled_absent",
                session_factory=session_factory,
                detail=verdict["reason"],
            )
            results.append({"client_order_id": cid, "verdict": "not_found", "reason": verdict["reason"]})
            continue
        applied = _apply_found_order(
            intent,
            sources,
            session_factory=session_factory,
            record_activity=record_activity,
            mark_intent_reconciled=mark_intent_reconciled,
        )
        if not applied.get("ok"):
            note_unresolved(
                f"Matched order {ticker} client_order_id={cid} was not booked. It stays reserved. "
                f"{applied.get('reason')}"
            )
            results.append({"client_order_id": cid, "verdict": "unknown", "reason": applied.get("reason")})
            continue
        results.append({"client_order_id": cid, "verdict": "found", **applied})
    return results


def _apply_found_order(intent, sources, *, session_factory, record_activity, mark_intent_reconciled) -> dict:
    """Record a matched order once. An open remainder stays reserved."""
    from src.fixed_point import FixedPointCountError, format_count_fp, is_whole_count, parse_fixed_point_count

    cid = str(intent.get("client_order_id") or "")
    oid = str(intent.get("exchange_order_id") or "")
    order = _matched_order(sources, cid, oid) or {}
    exchange_id = str(order.get("order_id") or order.get("id") or oid or "")
    status = str(order.get("status") or "").strip().lower()
    try:
        filled = parse_fixed_point_count(order.get("fill_count_fp")) if order.get("fill_count_fp") not in (None, "") else parse_fixed_point_count("0")
        remaining_raw = order.get("remaining_count_fp")
        remaining = parse_fixed_point_count(remaining_raw) if remaining_raw not in (None, "") else parse_fixed_point_count("0")
    except FixedPointCountError as exc:
        return {"ok": False, "reason": f"matched order count is not fixed-point ({exc})"}
    if not is_whole_count(filled) or not is_whole_count(remaining):
        return {
            "ok": False,
            "reason": (
                f"matched fill_count_fp={format_count_fp(filled)} "
                f"remaining_count_fp={format_count_fp(remaining)} was not relabeled"
            ),
        }
    ticker = str(intent.get("market_id") or "")
    act = str(intent.get("action") or "")
    try:
        act = normalize_order_action(act)
        side = normalize_order_side(intent.get("side"), order)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    if filled > 0:
        booked = _book_fill_delta(
            intent,
            exchange_id=exchange_id,
            filled=int(filled),
            order=order,
            side=side,
            action=act,
            session_factory=session_factory,
        )
        if not booked.get("ok"):
            return booked
    terminal = status in ("canceled", "executed") and remaining == 0
    if terminal:
        mark_intent_reconciled(
            cid,
            market_id=ticker,
            action=act,
            order_status="reconciled_filled" if filled > 0 else "reconciled_canceled",
            session_factory=session_factory,
            detail=f"exchange order {exchange_id or 'unknown'} status {status or 'unknown'}",
        )
        return {"ok": True, "terminal": True, "filled": format_count_fp(filled), "exchange_order_id": exchange_id}
    if status not in ("resting", "executed"):
        return {"ok": False, "reason": f"matched order status {status or 'missing'} is not booked"}
    record_activity(
        {
            "event_type": "order_ack",
            "order_status": "open_at_exchange",
            "market_id": ticker,
            "action": act,
            "side": side or None,
            "client_order_id": cid,
            "exchange_order_id": exchange_id or None,
            "quantity": int(remaining) if remaining > 0 else int(intent.get("quantity") or 0),
            "limit_price": intent.get("limit_price"),
            "fill_count_fp": format_count_fp(filled),
            "detail": "matched open order; remainder stays reserved",
        },
        session_factory=session_factory,
    )
    return {
        "ok": True,
        "terminal": False,
        "filled": format_count_fp(filled),
        "remaining": format_count_fp(remaining),
        "exchange_order_id": exchange_id,
    }


def _fill_price(intent: dict, order: dict, side: str) -> float:
    if side == "NO":
        raw = order.get("no_price_dollars") or order.get("average_fill_price")
    else:
        raw = order.get("yes_price_dollars") or order.get("average_fill_price")
    if raw in (None, ""):
        raw = intent.get("limit_price")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _book_fill_delta(intent, *, exchange_id, filled, order, side, action, session_factory) -> dict:
    from src.database.models import get_session_local

    fees, fee_kind = cumulative_exchange_fees(order)
    factory = session_factory or get_session_local()
    db = factory()
    try:
        result = apply_cumulative_fill(
            db,
            action=action,
            side=side,
            market_id=str(intent.get("market_id") or ""),
            cumulative_qty=int(filled),
            cumulative_fees=fees,
            price=_fill_price(intent, order, side),
            client_order_id=str(intent.get("client_order_id") or ""),
            exchange_order_id=exchange_id,
            event_ticker=intent.get("event_ticker"),
            market_title=str(intent.get("market_id") or ""),
            order=order,
            fee_kind=fee_kind,
        )
        if result.get("ok"):
            db.commit()
        else:
            db.rollback()
        return result
    except Exception as exc:
        db.rollback()
        return {"ok": False, "reason": f"fill bookkeeping failed ({type(exc).__name__})"}
    finally:
        db.close()
