#!/usr/bin/env python3
"""Recompute closed-position realized P&L from Kalshi fills and settlements.

Read-only by default. ``--apply`` writes, and only when the app is not running.
The backup uses the SQLite backup API so a WAL is included.

    python tools/repair_realized_pnl.py trading_bot.db --fills fills.json
    python tools/repair_realized_pnl.py trading_bot.db --fetch --apply

Fill objects may use the API names (``yes_price_dollars``, ``no_price_dollars``,
``fee_cost``, ``count_fp``) or the short export names (``yes_price``, ``fee``).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import socket
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.policy_migration import consistent_sqlite_backup
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
        ticker = str(row.get("ticker") or row.get("market_ticker") or "").strip().upper()
        if ticker:
            out[ticker] = row
    return out


def plan_repairs(positions: list[dict], fills: list, settlements: list) -> list[dict]:
    """One plan per closed position. Fills for a ticker are shared, not copied onto each row."""
    grouped = _group_fills(fills)
    settled = _settlement_by_ticker(settlements)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pos in positions:
        if str(pos.get("status") or "") != "closed":
            continue
        ticker = str(pos.get("market_id") or "").strip().upper()
        side = str(pos.get("side") or "").strip().upper()
        if not ticker or side not in ("YES", "NO"):
            continue
        buckets[(ticker, side)].append(pos)
    plans = []
    for (ticker, side), group in buckets.items():
        legs = grouped.get(ticker) or []
        if not legs:
            continue
        settlement = dict(settled.get(ticker) or {})
        settlement.setdefault("side", side)
        cash = fills_to_cashflows(legs, settlement, held_side=side)
        weights = []
        for pos in group:
            try:
                qty = float(pos.get("quantity") or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
            weights.append(qty if qty > 0 else 0.0)
        if sum(weights) <= 0:
            weights = [1.0 for _ in group]
        total = sum(weights)
        for pos, weight in zip(group, weights):
            share = weight / total
            old_reason = str(pos.get("exit_reason") or "")
            new_reason = old_reason
            if cash["closed_by_sell"] and old_reason == "settlement":
                new_reason = "stop_loss"
            plans.append(
                {
                    "id": pos.get("id"),
                    "ticker": ticker,
                    "side": side,
                    "old_realized_pnl": float(pos.get("realized_pnl") or 0.0),
                    "new_realized_pnl": float(cash["realized_pnl"]) * share,
                    "old_fees_paid": float(pos.get("fees_paid") or 0.0),
                    "new_fees_paid": float(cash["fees_paid"]) * share,
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


def running_block_reason(database: Path, port: int = 8000) -> str | None:
    """Why a write must be refused, or None when the file is free."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", int(port))) == 0:
            return (
                f"Refusing to modify the database because something is listening on 127.0.0.1:{port}. "
                "Stop the app so it releases the database, then run this again."
            )
    finally:
        sock.close()
    conn = sqlite3.connect(str(database), timeout=0.2)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return (
                "Refusing to modify the database because it is locked. "
                "Stop the app so it releases the file, then run this again."
            )
        raise
    finally:
        conn.close()
    return None


def apply_plans(database: Path, plans: list[dict]) -> int:
    conn = sqlite3.connect(database)
    try:
        changed = 0
        by_side: dict[tuple[str, str], float] = defaultdict(float)
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
            by_side[(plan["ticker"], plan["side"])] += float(plan["new_realized_pnl"])
        for (ticker, side), total in by_side.items():
            trades = conn.execute(
                """
                SELECT id FROM trades
                 WHERE upper(market_id) = ? AND upper(side) = ? AND lower(action) = 'sell'
                 ORDER BY rowid
                """,
                (ticker, side),
            ).fetchall()
            if not trades:
                continue
            first = trades[0][0]
            conn.execute("UPDATE trades SET realized_pnl = ? WHERE id = ?", (total, first))
            for extra in trades[1:]:
                conn.execute("UPDATE trades SET realized_pnl = 0 WHERE id = ?", (extra[0],))
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
            SELECT id, market_id, side, status, quantity, realized_pnl, fees_paid, exit_reason
              FROM positions
             WHERE status = 'closed'
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


async def fetch_fills_and_settlements(client) -> tuple[list, list]:
    """Read-only portfolio fills and settlements. Does not place an order."""
    fills_page = await client._reconcile_pages("/trade-api/v2/portfolio/fills", "fills", {})
    if not isinstance(fills_page, dict) or fills_page.get("state") != "complete":
        raise RuntimeError(f"fills read was not complete: {fills_page!r}"[:300])
    settlements = await client.get_settlements_paginated()
    return list(fills_page.get("rows") or []), list(settlements or [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recompute closed realized P&L from fills")
    parser.add_argument("database")
    parser.add_argument("--fills", default="")
    parser.add_argument("--settlements", default="")
    parser.add_argument("--fetch", action="store_true", help="Read-only GET of fills and settlements")
    parser.add_argument("--audit", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Write the database. Refused while the app is running.")
    args = parser.parse_args(argv)
    database = Path(args.database)
    if args.fetch:
        from src.clients.kalshi_client import KalshiClient
        from src.config import settings

        client = KalshiClient(settings.kalshi_api_key, settings.kalshi_private_key_path, settings.kalshi_base_url)

        async def _pull():
            try:
                return await fetch_fills_and_settlements(client)
            finally:
                await client.aclose()

        fills, settlements = asyncio.run(_pull())
    else:
        if not args.fills:
            print("Pass --fills or --fetch. Nothing was written.")
            return 2
        fills = _load_json(Path(args.fills))
        settlements = _load_json(Path(args.settlements)) if args.settlements else []
    plans = plan_repairs(load_closed_positions(database), fills, settlements)
    audit = Path(args.audit) if args.audit else database.with_name("realized_pnl_audit.csv")
    write_audit(audit, plans)
    new_total = sum(float(row["new_realized_pnl"]) for row in plans)
    print(f"positions={len(plans)} new_realized_total={new_total:.4f} audit={audit}")
    if args.dry_run or not args.apply:
        print("dry-run: database not modified")
        return 0
    blocked = running_block_reason(database)
    if blocked:
        print(blocked)
        return 3
    backup = consistent_sqlite_backup(database)
    changed = apply_plans(database, plans)
    print(f"backup={backup} rows_updated={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
