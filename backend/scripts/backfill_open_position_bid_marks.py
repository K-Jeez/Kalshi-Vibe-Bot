"""Backfill ``bid_price``, ``current_price``, and ``unrealized_pnl`` for open positions.

Uses strict observable bids (REST ``yes_bid``/``no_bid`` + optional ``GET …/orderbook`` when the
snapshot shows no bid). Fixes stale rows that still show ~1¢ from older parity/composite logic.

Usage from ``backend/``::

    python scripts/backfill_open_position_bid_marks.py

Requires ``KALSHI_*`` credentials in ``.env`` and network access.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
os.chdir(_BACKEND_ROOT)
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from src.clients.kalshi_client import KalshiClient
from src.config import TRADE_MODE, settings
from src.database.models import Position, get_session_local
from src.reconcile.kalshi_positions import recompute_open_live_position_unrealized_pnl


async def _run() -> None:
    client = KalshiClient(
        settings.kalshi_api_key,
        settings.kalshi_private_key_path,
        settings.kalshi_base_url,
    )
    if not client._has_credentials():
        print("Kalshi client missing credentials — check KALSHI_API_KEY and private key path in .env")
        return

    SessionLocal = get_session_local()
    db = SessionLocal()
    try:
        rows = (
            db.query(Position)
            .filter(Position.status == "open", Position.trade_mode == TRADE_MODE)
            .order_by(Position.opened_at.asc())
            .all()
        )
        n_rows = len(rows)
        n_touch = 0
        for pos in rows:
            if await recompute_open_live_position_unrealized_pnl(client, pos):
                n_touch += 1
        db.commit()
        print(f"Open live positions scanned={n_rows} rows_updated={n_touch} (strict bid marks).")
    finally:
        db.close()


def main() -> None:
    argparse.ArgumentParser(description="Backfill bid marks for open live positions from Kalshi.").parse_args()
    asyncio.run(_run())


if __name__ == "__main__":
    main()
