"""Offline checks for the live-run repairs. No Kalshi and no model calls."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.bot.order_outcome import buy_fill_status, classify_routing_error, ioc_zero_fill_is_not_resting
from src.bot.routing_cooldown import clear_untradeable, note_untradeable, untradeable_reason
from src.bot.supervision import run_parallel_cycles
from src.decision_engine.strategy_gates import (
    MAX_KELLY_BANKROLL_PCT,
    either_side_can_buy_one_capped_contract,
    kelly_contract_cap_for_bankroll,
)
from src.strategy_proposal import PROPOSAL_ACTIVE, PROPOSED_ENTRY_CAP_OF_CASH
from src.api.trades import denver_close_date


def test_monitor_runs_while_scan_is_blocked():
    started = asyncio.Event()
    release = asyncio.Event()
    monitors = []

    async def scan_once():
        started.set()
        await release.wait()

    async def monitor_once():
        monitors.append("ran")

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(run_parallel_cycles(monitor_once, scan_once, stop, 0.01))
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.sleep(0.05)
        assert monitors
        release.set()
        stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())


def test_rejected_buy_is_not_a_model_skip():
    assert classify_routing_error("Exchange user not found. Exchange Sharding", 404) == "exchange_shard_user"
    assert classify_routing_error("market closed", 404) == "not_found"
    assert buy_fill_status(filled=0, requested=9, time_in_force="immediate_or_cancel") == "zero_fill"
    assert buy_fill_status(filled=0.4, requested=9, time_in_force="immediate_or_cancel") == "fractional_fill"
    assert buy_fill_status(filled=3, requested=9, time_in_force="immediate_or_cancel") == "partial"
    assert ioc_zero_fill_is_not_resting("immediate_or_cancel", 0)


def test_routing_cooldown_expires_and_is_not_permanent():
    clear_untradeable()
    note_untradeable("KXTEST", "exchange_shard_user", minutes=30, now=1000)
    assert untradeable_reason("KXTEST", now=1000 + 10) == "exchange_shard_user"
    assert untradeable_reason("KXTEST", now=1000 + 31 * 60) is None
    clear_untradeable()


def test_prefilter_requires_both_sides_unaffordable():
    # 5% of $10 is $0.50. Ask 0.51 cannot buy one. Ask 0.50 can.
    rich = {"yes_ask": 0.40, "no_ask": 0.51}
    assert either_side_can_buy_one_capped_contract(rich, 10)
    neither = {"yes_ask": 0.51, "no_ask": 0.80}
    assert not either_side_can_buy_one_capped_contract(neither, 10)
    only_no = {"yes_ask": 0.90, "no_ask": 0.20}
    assert either_side_can_buy_one_capped_contract(only_no, 10)
    missing_yes_cheap_no = {"no_ask": 0.20}
    assert either_side_can_buy_one_capped_contract(missing_yes_cheap_no, 10)
    missing_yes_dear_no = {"no_ask": 0.90}
    assert not either_side_can_buy_one_capped_contract(missing_yes_dear_no, 10)
    assert kelly_contract_cap_for_bankroll(10, 0.50) == 1
    assert kelly_contract_cap_for_bankroll(10, 0.51) == 0
    assert MAX_KELLY_BANKROLL_PCT == 0.05
    assert PROPOSAL_ACTIVE is False
    assert PROPOSED_ENTRY_CAP_OF_CASH == 0.025


def test_denver_evening_stays_on_that_denver_date():
    # 5:33pm MST on Nov 8 2026 is 00:33 UTC on Nov 9.
    instant = datetime(2026, 11, 9, 0, 33, tzinfo=timezone.utc)
    assert denver_close_date(instant).isoformat() == "2026-11-08"


def test_export_reads_closed_rows_without_writing(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE positions (
            market_id TEXT, side TEXT, quantity INT, entry_price REAL, entry_cost REAL,
            fees_paid REAL, realized_pnl REAL, exit_reason TEXT, status TEXT, closed_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("M1", "YES", 1, 0.4, 0.4, 0.02, 1.95, "stop_loss", "closed", "2026-11-09T00:33:00Z"),
    )
    conn.commit()
    conn.close()
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "reconcile_export",
        Path(__file__).resolve().parents[1] / "tools" / "reconcile_export.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    report = mod.export_closed(db)
    assert report["closed_count"] == 1
    assert report["sum_realized_pnl"] == 1.95
    assert "not computed" in report["note"]
    assert json.loads(json.dumps(report))["positions"][0]["exit_reason"] == "stop_loss"
