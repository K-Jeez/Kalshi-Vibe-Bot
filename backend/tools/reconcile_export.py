#!/usr/bin/env python3
"""Read a local SQLite file and print closed-position rows.

This does not connect to Kalshi, place orders, or change the database.
It does not explain a cash difference. Compare the printed realized P&L
and fees with the exchange statement yourself.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def export_closed(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT market_id, side, quantity, entry_price, entry_cost, fees_paid,
                   realized_pnl, exit_reason, status, closed_at
            FROM positions
            WHERE status = 'closed'
            ORDER BY closed_at
            """
        ).fetchall()
    finally:
        conn.close()
    positions = [dict(row) for row in rows]
    realized = round(sum(float(p.get("realized_pnl") or 0) for p in positions), 4)
    fees = round(sum(float(p.get("fees_paid") or 0) for p in positions), 4)
    return {
        "closed_count": len(positions),
        "sum_realized_pnl": realized,
        "sum_fees_paid": fees,
        "note": "Cash change is not computed. This is the local closed-position ledger only.",
        "positions": positions,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Export closed positions from a local SQLite file.")
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    if not args.database.is_file():
        print("database file not found", file=sys.stderr)
        return 2
    print(json.dumps(export_closed(args.database), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
