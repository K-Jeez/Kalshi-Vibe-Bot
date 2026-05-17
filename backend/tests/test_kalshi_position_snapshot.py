"""Kalshi portfolio snapshot → open position sync."""

from types import SimpleNamespace

from src.reconcile.kalshi_positions import (
    KalshiPositionSnapshot,
    apply_kalshi_snapshot_to_open_position,
)


def test_apply_snapshot_updates_qty_and_fees_not_entry():
    pos = SimpleNamespace(
        quantity=7,
        entry_cost=1.95,
        entry_price=0.2643,
        fees_paid=0.10,
    )
    snap = KalshiPositionSnapshot(
        ticker="KXPGAWIN-PGC26LIV-1",
        side="NO",
        qty_whole=7,
        qty_raw_fp=7.0,
        cost_usd=5.35,
        avg_price=0.75,
        fees_paid_dollars=0.12,
        realized_locked_dollars=0.0,
        realized_pnl_usd=None,
    )
    assert apply_kalshi_snapshot_to_open_position(pos, snap) is True
    assert pos.quantity == 7
    assert abs(pos.fees_paid - 0.12) < 1e-9
    assert abs(pos.entry_cost - 1.95) < 1e-9
    assert abs(pos.entry_price - 0.2643) < 1e-9
