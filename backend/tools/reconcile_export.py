#!/usr/bin/env python3
"""Read-only audit of a local SQLite file.

Prints the pieces that can be compared with a Kalshi statement, and flags
what this file does not contain. It does not invent a cash correction and
it does not call Kalshi.

Optional cash figures are inputs from you, not values read from the exchange:

python tools/reconcile_export.py trading_bot.db --cash-start 100 --cash-end 98.83
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _rows(conn: sqlite3.Connection, sql: str) -> list[dict]:
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.OperationalError:
        return []


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return []


def _select(conn: sqlite3.Connection, table: str, wanted: list[str], suffix: str) -> list[dict]:
    have = _columns(conn, table)
    cols = [name for name in wanted if name in have]
    if not cols:
        return []
    return _rows(conn, f"SELECT {', '.join(cols)} FROM {table} {suffix}")


def _sum(rows: list[dict], key: str) -> float:
    total = 0.0
    for row in rows:
        try:
            total += float(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 4)


def audit_database(db_path: Path, *, cash_start: float | None = None, cash_end: float | None = None) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        closed = _rows(
            conn,
            """
            SELECT id, market_id, event_ticker, side, quantity, entry_price, entry_cost,
                   fees_paid, realized_pnl, exit_reason, status, closed_at
            FROM positions WHERE status = 'closed' ORDER BY closed_at
            """,
        )
        opens = _rows(
            conn,
            """
            SELECT id, market_id, event_ticker, side, quantity, entry_price, entry_cost, fees_paid, status
            FROM positions WHERE status = 'open'
            """,
        )
        trades = _rows(
            conn,
            """
            SELECT id, market_id, side, action, quantity, price, total_cost, fees, timestamp
            FROM trades ORDER BY timestamp
            """,
        )
        event_columns = _columns(conn, "activity_events")
        events = _select(
            conn,
            "activity_events",
            [
                "id", "run_id", "ts_utc", "event_type", "market_id", "event_ticker", "side", "action",
                "quantity", "limit_price", "fill_qty", "fill_price", "fees_dollars", "fee_kind",
                "client_order_id", "exchange_order_id", "order_status", "detail",
            ],
            "ORDER BY ts_utc",
        )
    finally:
        conn.close()

    unresolved = []
    for row in closed:
        if not str(row.get("exit_reason") or "").strip():
            unresolved.append({"market_id": row.get("market_id"), "gap": "closed row has no exit_reason"})
        if row.get("fees_paid") is None:
            unresolved.append({"market_id": row.get("market_id"), "gap": "fees_paid is null"})
        if row.get("realized_pnl") is None:
            unresolved.append({"market_id": row.get("market_id"), "gap": "realized_pnl is null"})
    fills = [row for row in events if row.get("event_type") == "fill"]
    if events and "exchange_order_id" not in event_columns:
        unresolved.append({"gap": "activity table has no exchange_order_id column"})
    for row in fills:
        if not str(row.get("client_order_id") or "").strip():
            unresolved.append({"market_id": row.get("market_id"), "gap": "fill has no client_order_id"})
        if "exchange_order_id" in event_columns and not str(row.get("exchange_order_id") or "").strip():
            unresolved.append({"market_id": row.get("market_id"), "gap": "fill has no exchange order id"})
        if row.get("fee_kind") == "unavailable" or row.get("fees_dollars") is None:
            unresolved.append({"market_id": row.get("market_id"), "gap": "fill fee was not on the exchange record"})
    pending = [
        row for row in events
        if row.get("event_type") == "order_intent" and row.get("order_status") == "pending"
    ]
    if pending:
        unresolved.append({"gap": f"{len(pending)} order intent(s) still pending", "count": len(pending)})

    realized = _sum(closed, "realized_pnl")
    components = {
        "closed_count": len(closed),
        "open_count": len(opens),
        "sum_closed_entry_cost": _sum(closed, "entry_cost"),
        "sum_closed_fees_paid": _sum(closed, "fees_paid"),
        "sum_closed_realized_pnl": realized,
        "sum_open_entry_cost": _sum(opens, "entry_cost"),
        "sum_open_fees_paid": _sum(opens, "fees_paid"),
        "trade_row_count": len(trades),
        "activity_event_count": len(events),
        "fill_event_count": len(fills),
        "pending_intent_count": len(pending),
        "run_ids": sorted({str(row.get("run_id")) for row in events if row.get("run_id")}),
    }
    cash = None
    if cash_start is not None and cash_end is not None:
        gap = round(float(cash_end) - float(cash_start) - realized, 4)
        cash = {
            "cash_start": cash_start,
            "cash_end": cash_end,
            "cash_change": round(float(cash_end) - float(cash_start), 4),
            "ledger_realized_pnl": realized,
            "unexplained_gap": gap,
            "resolved": False,
            "note": "A nonzero gap is unresolved. This tool does not assign it to a fee or a missing fill.",
        }
        if abs(gap) > 0.001:
            unresolved.append({"gap": "cash change minus ledger realized P&L is unexplained", "amount": gap})
    return {
        "components": components,
        "unresolved": unresolved,
        "cash_comparison": cash,
        "closed_positions": closed,
        "open_positions": opens,
        "note": (
            "Fees on closed rows are whatever was stored. NO-side historical fees are not reconstructed. "
            "The estimated 0.07 schedule is not applied here."
        ),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only local ledger audit.")
    parser.add_argument("database", type=Path)
    parser.add_argument("--cash-start", type=float, default=None)
    parser.add_argument("--cash-end", type=float, default=None)
    args = parser.parse_args(argv)
    if not args.database.is_file():
        print("database file not found", file=sys.stderr)
        return 2
    print(json.dumps(audit_database(args.database, cash_start=args.cash_start, cash_end=args.cash_end), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
