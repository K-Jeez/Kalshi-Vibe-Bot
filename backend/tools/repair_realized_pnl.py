#!/usr/bin/env python3
"""Recompute closed-position realized P&L from Kalshi fills and settlements.

Backs up the SQLite file, writes an audit CSV of old versus new values, and
does not place orders. ``--dry-run`` writes the CSV and does not change the DB.

    python tools/repair_realized_pnl.py trading_bot.db --fills fills.json --settlements settlements.json
    python tools/repair_realized_pnl.py trading_bot.db --fills fills.json --dry-run --audit /tmp/audit.csv

Fill objects use ticker, action, side, count_fp, yes_price, and fee.
Settlement objects use ticker, market_result, and optional payout.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.reconcile.realized_cashflow import fills_to_cashflows


def _load_json(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("fills") or data.get("settlements") or data.get("rows") or []
    return data


def _group_fills(fills: list) -> dict[str, list]:
    grouped: dict[str, list] = defaultdict(list)
    for fill in fills or []:
        ticker = str(fill.get("ticker") or fill.get("market_ticker") or "").strip().upper()
        if ticker:
            grouped[ticker].append(fill)
    return grouped


def _settlement_by_ticker(rows: list) -> dict[str, dict]:
    out = {}
    for row in rows or []:
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker:
            out[ticker] = row
    return out


def plan_repairs(positions: list[dict], fills: list, settlements: list) -> list[dict]:
    grouped = _group_fills(fills)
    settled = _settlement_by_ticker(settlements)
    plans = []
    for pos in positions:
        if str(pos.get("status") or "") != "closed":
            continue
        ticker = str(pos.get("market_id") or "").strip().upper()
        legs = grouped.get(ticker) or []
        if not legs:
            continue
        settlement = settled.get(ticker)
        if settlement is not None and pos.get("side") and "side" not in settlement:
            settlement = {**settlement, "side": pos.get("side")}
        cash = fills_to_cashflows(legs, settlement)
        old_realized = float(pos.get("realized_pnl") or 0.0)
        old_fees = float(pos.get("fees_paid") or 0.0)
        old_reason = str(pos.get("exit_reason") or "")
        new_reason = old_reason
        if cash["closed_by_sell"] and old_reason == "settlement":
            new_reason = "stop_loss"
        plans.append(
            {
                "id": pos.get("id"),
                "ticker": ticker,
                "side": pos.get("side") or cash.get("side") or "",
                "old_realized_pnl": old_realized,
                "new_realized_pnl": float(cash["realized_pnl"]),
                "old_fees_paid": old_fees,
                "new_fees_paid": float(cash["fees_paid"]),
                "old_exit_reason": old_reason,
                "new_exit_reason": new_reason,
            }
        )
    return plans


def write_audit(path: Path, plans: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ticker",
        "side",
        "old_realized_pnl",
        "new_realized_pnl",
        "old_fees_paid",
        "new_fees_paid",
        "old_exit_reason",
        "new_exit_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(plans)


def backup_database(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.pre-realized-repair-{stamp}.bak")
    shutil.copy2(path, dest)
    return dest


def apply_plans(database: Path, plans: list[dict]) -> int:
    conn = sqlite3.connect(database)
    try:
        changed = 0
        for plan in plans:
            changed += conn.execute(
                """
                UPDATE positions
                   SET realized_pnl = ?, fees_paid = ?, exit_reason = ?
                 WHERE id = ?
                """,
                (
                    plan["new_realized_pnl"],
                    plan["new_fees_paid"],
                    plan["new_exit_reason"],
                    plan["id"],
                ),
            ).rowcount
        conn.commit()
        return int(changed or 0)
    finally:
        conn.close()


def load_closed_positions(database: Path) -> list[dict]:
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, market_id, side, status, realized_pnl, fees_paid, exit_reason
              FROM positions
             WHERE status = 'closed'
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute closed realized P&L from fills")
    parser.add_argument("database")
    parser.add_argument("--fills", required=True)
    parser.add_argument("--settlements", default="")
    parser.add_argument("--audit", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    database = Path(args.database)
    fills = _load_json(Path(args.fills))
    settlements = _load_json(Path(args.settlements)) if args.settlements else []
    plans = plan_repairs(load_closed_positions(database), fills, settlements)
    audit = Path(args.audit) if args.audit else database.with_name("realized_pnl_audit.csv")
    write_audit(audit, plans)
    new_total = sum(float(row["new_realized_pnl"]) for row in plans)
    print(f"positions={len(plans)} new_realized_total={new_total:.4f} audit={audit}")
    if args.dry_run:
        print("dry-run: database not modified")
        return 0
    backup = backup_database(database)
    changed = apply_plans(database, plans)
    print(f"backup={backup} rows_updated={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
