"""One ledger update for a cumulative exchange fill.

The live buy path and the unconfirmed-order reconcile path both call
``apply_cumulative_fill``. A later report for the same client order id books
only the increase in contracts and fees. A sell never creates a position.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from src.clients.kalshi_client import kalshi_order_fees_dollars
from src.config import TRADE_MODE
from src.database.models import ActivityEvent, Position, Trade
from src.fixed_point import format_count_fp
from src.reconcile.open_positions import (
    closed_leg_realized_pnl_kalshi_dollars,
    get_open_position,
    normalize_market_id,
    normalize_side,
)
from src.util.datetimes import utc_now


def normalize_order_action(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in ("buy", "sell"):
        raise ValueError("action must be buy or sell")
    return text


def normalize_order_side(intent_side: object, order: dict | None = None) -> str:
    """Return YES or NO. A bid or ask quote is not a contract side.

    The intent side wins when it is YES or NO. An order ``side`` or
    ``outcome_side`` may confirm it. A conflict, or a missing YES/NO, fails
    closed instead of inventing the side from the book.
    """
    found = ""
    candidates = [intent_side]
    if isinstance(order, dict):
        candidates.extend((order.get("outcome_side"), order.get("side")))
    for raw in candidates:
        text = str(raw or "").strip().upper()
        if text in ("", "BID", "ASK"):
            continue
        if text not in ("YES", "NO"):
            raise ValueError("side must be yes or no")
        if found and found != text:
            raise ValueError("yes/no side conflict")
        found = text
    if not found:
        raise ValueError("side must be yes or no")
    return found


def fill_trade_id(exchange_order_id: str, client_order_id: str, action: str, cumulative_qty: int) -> str:
    """One id for one exchange fill total. A replay of the same total hits the same row."""
    stem = str(exchange_order_id or client_order_id or "").strip()
    return f"{stem}:{normalize_order_action(action)}:{int(cumulative_qty)}"


def ensure_fill_trade(
    db,
    *,
    action: str,
    market_id: str,
    market_title: str,
    side: str,
    cumulative_qty: int,
    delta_qty: int,
    price: float,
    notional: float,
    client_order_id: str,
    exchange_order_id: str,
    realized_pnl: float | None = None,
) -> str:
    trade_id = fill_trade_id(exchange_order_id, client_order_id, action, cumulative_qty)
    if db.query(Trade).filter(Trade.id == trade_id).first() is None:
        db.add(
            Trade(
                id=trade_id,
                market_id=market_id,
                market_title=market_title,
                action=normalize_order_action(action),
                side=normalize_order_side(side),
                quantity=int(delta_qty),
                price=float(price),
                total_cost=float(notional),
                realized_pnl=realized_pnl,
                trade_mode=TRADE_MODE,
            )
        )
    return trade_id


def cumulative_exchange_fees(order: dict | None) -> tuple[float, str]:
    """Fees on the order object. Missing fee fields are not treated as zero billed fees."""
    if not isinstance(order, dict):
        return 0.0, "unavailable"
    present = any(key in order and order.get(key) not in (None, "") for key in ("taker_fees_dollars", "maker_fees_dollars"))
    if not present:
        return 0.0, "unavailable"
    return float(kalshi_order_fees_dollars(order)), "exchange_fill"


def _booked_totals(db, *, client_order_id: str, exchange_order_id: str, action: str) -> tuple[int, float]:
    rows = (
        db.query(ActivityEvent)
        .filter(
            ActivityEvent.client_order_id == client_order_id,
            ActivityEvent.event_type == "fill",
            ActivityEvent.action == action,
        )
        .all()
    )
    qty = 0
    fees = 0.0
    for row in rows:
        other = str(row.exchange_order_id or "").strip()
        if exchange_order_id and other and other != exchange_order_id:
            continue
        qty += int(row.quantity or 0)
        fees += float(row.fees_dollars or 0.0)
    return qty, fees


def apply_cumulative_fill(
    db,
    *,
    action: object,
    side: object,
    market_id: str,
    cumulative_qty: int,
    cumulative_fees: float,
    price: float,
    client_order_id: str,
    exchange_order_id: str = "",
    event_ticker: str | None = None,
    market_title: str | None = None,
    order: dict | None = None,
    exit_reason: str | None = None,
    fee_kind: str = "exchange_fill",
    entry_decision_log_id: str | None = None,
    stop_loss_drawdown_pct_at_entry: float | None = None,
    close_time: str | None = None,
    expected_expiration_time: str | None = None,
) -> dict:
    """Apply the new contracts and fees since the last persisted fill for this id.

    The caller commits. This function does not create a position for a sell.
    """
    act = normalize_order_action(action)
    held = normalize_order_side(side, order)
    cid = str(client_order_id or "").strip()
    xid = str(exchange_order_id or "").strip()
    if not cid:
        return {"ok": False, "reason": "fill has no client order id"}
    target = int(cumulative_qty)
    if target < 0:
        return {"ok": False, "reason": "cumulative fill is negative"}
    booked_qty, booked_fees = _booked_totals(db, client_order_id=cid, exchange_order_id=xid, action=act)
    delta_qty = target - booked_qty
    delta_fees = round(float(cumulative_fees) - float(booked_fees), 6)
    if delta_qty < 0 or delta_fees < -1e-6:
        return {"ok": False, "reason": "exchange fill shrank versus the ledger"}
    if delta_qty == 0 and abs(delta_fees) <= 1e-6:
        return {"ok": True, "delta_qty": 0, "delta_fees": 0.0, "replay": True}
    px = float(price)
    if delta_qty > 0 and px <= 0:
        return {"ok": False, "reason": "fill has no price"}
    mid = normalize_market_id(market_id)
    existing = get_open_position(db, trade_mode=TRADE_MODE, market_id=mid, side=held)
    if act == "sell":
        if existing is None:
            closed = (
                db.query(Position)
                .filter(
                    Position.trade_mode == TRADE_MODE,
                    Position.status == "closed",
                    Position.market_id == mid,
                    Position.side == held,
                )
                .first()
            )
            if closed is None:
                # Case-insensitive closed row.
                for row in db.query(Position).filter(Position.trade_mode == TRADE_MODE, Position.status == "closed").all():
                    if normalize_market_id(row.market_id) == mid and normalize_side(row.side) == held:
                        closed = row
                        break
            reason = "sell matches a closed position" if closed is not None else "sell has no open position"
            return {"ok": False, "reason": reason}
        have = int(existing.quantity or 0)
        if delta_qty > have:
            return {"ok": False, "reason": "sell fill is larger than the open position"}
        return _apply_sell_delta(
            db,
            existing,
            delta_qty=delta_qty,
            delta_fees=max(0.0, delta_fees),
            price=px,
            client_order_id=cid,
            exchange_order_id=xid,
            event_ticker=event_ticker,
            exit_reason=exit_reason,
            fee_kind=fee_kind,
            cumulative_qty=target,
        )
    return _apply_buy_delta(
        db,
        existing,
        market_id=mid,
        side=held,
        delta_qty=delta_qty,
        delta_fees=max(0.0, delta_fees),
        price=px,
        client_order_id=cid,
        exchange_order_id=xid,
        event_ticker=event_ticker,
        market_title=market_title or mid,
        fee_kind=fee_kind,
        cumulative_qty=target,
        entry_decision_log_id=entry_decision_log_id,
        stop_loss_drawdown_pct_at_entry=stop_loss_drawdown_pct_at_entry,
        close_time=close_time,
        expected_expiration_time=expected_expiration_time,
    )


def _write_fill(
    db,
    *,
    action: str,
    market_id: str,
    side: str,
    event_ticker,
    client_order_id: str,
    exchange_order_id: str,
    delta_qty: int,
    delta_fees: float,
    price: float,
    fee_kind: str,
    cumulative_qty: int,
    detail: str,
) -> None:
    from src.activity_log import _RUN_ID, _apply, _append_durable_event

    row = ActivityEvent(id=str(uuid.uuid4()), seq=0)
    _apply(
        row,
        {
            "event_type": "fill",
            "order_status": "filled",
            "market_id": market_id,
            "event_ticker": event_ticker,
            "action": action,
            "side": side,
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_order_id or None,
            "quantity": int(delta_qty),
            "fill_qty": float(delta_qty),
            "fill_count_fp": format_count_fp(Decimal(cumulative_qty)),
            "fill_price": float(price),
            "fees_dollars": float(delta_fees),
            "fee_kind": fee_kind,
            "detail": detail,
        },
    )
    row.run_id = _RUN_ID
    db.add(row)
    db.flush()
    _append_durable_event(row)


def _apply_buy_delta(
    db,
    existing: Position | None,
    *,
    market_id: str,
    side: str,
    delta_qty: int,
    delta_fees: float,
    price: float,
    client_order_id: str,
    exchange_order_id: str,
    event_ticker,
    market_title: str,
    fee_kind: str,
    cumulative_qty: int,
    entry_decision_log_id: str | None = None,
    stop_loss_drawdown_pct_at_entry: float | None = None,
    close_time: str | None = None,
    expected_expiration_time: str | None = None,
) -> dict:
    notional = float(delta_qty) * float(price)
    if existing is None:
        db.add(
            Position(
                id=str(uuid.uuid4()),
                market_id=market_id,
                market_title=market_title,
                event_ticker=event_ticker,
                side=side,
                quantity=int(delta_qty),
                entry_price=float(price),
                entry_cost=notional,
                current_price=float(price),
                fees_paid=float(delta_fees),
                status="open",
                trade_mode=TRADE_MODE,
                entry_decision_log_id=entry_decision_log_id,
                stop_loss_drawdown_pct_at_entry=stop_loss_drawdown_pct_at_entry,
                close_time=close_time,
                expected_expiration_time=expected_expiration_time,
            )
        )
    else:
        existing.quantity = int(existing.quantity or 0) + int(delta_qty)
        existing.entry_cost = float(existing.entry_cost or 0.0) + notional
        existing.fees_paid = float(getattr(existing, "fees_paid", 0.0) or 0.0) + float(delta_fees)
        if int(existing.quantity or 0) > 0:
            existing.entry_price = float(existing.entry_cost) / float(existing.quantity)
        existing.side = side
    _write_fill(
        db,
        action="buy",
        market_id=market_id,
        side=side,
        event_ticker=event_ticker,
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        delta_qty=delta_qty,
        delta_fees=delta_fees,
        price=price,
        fee_kind=fee_kind,
        cumulative_qty=cumulative_qty,
        detail="buy fill delta",
    )
    ensure_fill_trade(
        db,
        action="buy",
        market_id=market_id,
        market_title=market_title,
        side=side,
        cumulative_qty=cumulative_qty,
        delta_qty=delta_qty,
        price=price,
        notional=notional,
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
    )
    return {"ok": True, "delta_qty": delta_qty, "delta_fees": delta_fees, "action": "buy"}


def _apply_sell_delta(
    db,
    existing: Position,
    *,
    delta_qty: int,
    delta_fees: float,
    price: float,
    client_order_id: str,
    exchange_order_id: str,
    event_ticker,
    exit_reason: str | None,
    fee_kind: str,
    cumulative_qty: int,
) -> dict:
    pre_qty = int(existing.quantity or 0)
    pre_cost = float(existing.entry_cost or 0.0)
    pre_price = float(existing.entry_price or 0.0)
    existing.fees_paid = float(getattr(existing, "fees_paid", 0.0) or 0.0) + float(delta_fees)
    fees_now = float(existing.fees_paid or 0.0)
    realized = closed_leg_realized_pnl_kalshi_dollars(
        quantity_sold=int(delta_qty),
        exit_price_per_contract_gross=float(price),
        entry_cost_at_open=pre_cost,
        entry_price_at_open=pre_price,
        quantity_at_open=pre_qty,
        fees_paid_roundtrip=fees_now,
    )
    removed = pre_cost * (float(delta_qty) / float(pre_qty)) if pre_qty else 0.0
    remaining = pre_qty - int(delta_qty)
    kept_reason = str(existing.exit_reason or "").strip()
    if remaining <= 0:
        existing.quantity = int(delta_qty)
        existing.status = "closed"
        existing.closed_at = utc_now()
        existing.exit_reason = kept_reason or (exit_reason or "reconcile")
        existing.realized_pnl = float(realized)
        existing.current_price = float(price)
    else:
        existing.quantity = remaining
        existing.entry_cost = max(0.0, pre_cost - removed)
        if int(existing.quantity or 0) > 0:
            existing.entry_price = float(existing.entry_cost) / float(existing.quantity)
        existing.realized_pnl = float(getattr(existing, "realized_pnl", 0.0) or 0.0) + float(realized)
        if kept_reason:
            existing.exit_reason = kept_reason
    _write_fill(
        db,
        action="sell",
        market_id=normalize_market_id(existing.market_id),
        side=normalize_side(existing.side),
        event_ticker=event_ticker or existing.event_ticker,
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        delta_qty=delta_qty,
        delta_fees=delta_fees,
        price=price,
        fee_kind=fee_kind,
        cumulative_qty=cumulative_qty,
        detail="sell fill delta",
    )
    ensure_fill_trade(
        db,
        action="sell",
        market_id=existing.market_id,
        market_title=existing.market_title or existing.market_id,
        side=normalize_side(existing.side),
        cumulative_qty=cumulative_qty,
        delta_qty=delta_qty,
        price=price,
        notional=float(delta_qty) * float(price),
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        realized_pnl=float(realized),
    )
    return {
        "ok": True,
        "delta_qty": delta_qty,
        "delta_fees": delta_fees,
        "action": "sell",
        "remaining": max(0, remaining),
        "realized_pnl": float(realized),
        "closed": remaining <= 0,
    }
