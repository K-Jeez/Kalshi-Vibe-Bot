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


def test_monitor_failure_does_not_stop_the_scan():
    import asyncio as aio

    scans = []
    passes = {"m": 0}

    async def monitor_once():
        passes["m"] += 1
        if passes["m"] == 1:
            raise RuntimeError("monitor broke")

    async def scan_once():
        scans.append(1)

    async def main():
        stop = aio.Event()
        task = aio.create_task(run_parallel_cycles(monitor_once, scan_once, stop, 0.01))
        await aio.sleep(0.08)
        stop.set()
        await task

    aio.run(main())
    assert len(scans) >= 2
    assert passes["m"] >= 2


def test_order_status_and_exit_reason_and_whole_contracts():
    from src.bot.order_outcome import order_status_after_submit
    from src.ledger_money import realized_pnl, whole_contracts
    from src.reconcile.kalshi_settlement import preserve_exit_reason

    assert order_status_after_submit("immediate_or_cancel", 0) == "canceled"
    assert order_status_after_submit("immediate_or_cancel", 2) == "executed"
    assert preserve_exit_reason("stop_loss") == "stop_loss"
    assert preserve_exit_reason("") == "settlement"
    assert whole_contracts(0.9999999995) == 1
    assert whole_contracts(0.4) == 0
    assert realized_pnl(quantity=1, entry_price="0.40", won=True, fees="0.02") == realized_pnl(
        quantity="1.00", entry_price="0.40", won=True, fees="0.02"
    )


def test_event_cap_and_migration(tmp_path: Path):
    from src.decision_engine.strategy_gates import event_exposure_room
    from src.policy_migration import migrate_sqlite_file

    assert event_exposure_room(100, 4, 0, 0.5)
    assert not event_exposure_room(100, 4, 0, 2)
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE tuning_state (max_open_positions INT)")
    conn.execute("INSERT INTO tuning_state VALUES (30)")
    conn.commit()
    conn.close()
    first = migrate_sqlite_file(db)
    second = migrate_sqlite_file(db)
    conn = sqlite3.connect(db)
    value = conn.execute("SELECT max_open_positions FROM tuning_state").fetchone()[0]
    conn.close()
    assert first["rows_updated"] == 1
    assert second["already_applied"] is True
    assert value == 12


def test_shadow_eval_drops_future_leaks():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shadow_eval",
        Path(__file__).resolve().parents[1] / "tools" / "shadow_eval.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    report = mod.score_rows(
        [
            {
                "id": "leak",
                "decided_at": "2026-11-09T00:00:00Z",
                "resolved_at": "2026-11-09T00:00:00Z",
                "outcome": "yes",
                "model_p_yes": 0.7,
                "market_p_yes": 0.4,
            },
            {
                "id": "ok",
                "decided_at": "2026-11-08T00:00:00Z",
                "resolved_at": "2026-11-09T00:00:00Z",
                "outcome": "yes",
                "model_p_yes": 0.7,
                "market_p_yes": 0.4,
                "prompt_version": "decision-2026-11-09",
            },
        ]
    )
    assert report["n"] == 1
    assert report["excluded"][0]["reason"] == "outcome_not_after_decision"


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
    assert either_side_can_buy_one_capped_contract(rich, 40)
    neither = {"yes_ask": 0.51, "no_ask": 0.80}
    assert not either_side_can_buy_one_capped_contract(neither, 10)
    only_no = {"yes_ask": 0.90, "no_ask": 0.20}
    assert either_side_can_buy_one_capped_contract(only_no, 10)
    missing_yes_cheap_no = {"no_ask": 0.20}
    assert either_side_can_buy_one_capped_contract(missing_yes_cheap_no, 10)
    missing_yes_dear_no = {"no_ask": 0.90}
    assert not either_side_can_buy_one_capped_contract(missing_yes_dear_no, 10)
    assert kelly_contract_cap_for_bankroll(10, 0.50) == 0
    assert kelly_contract_cap_for_bankroll(40, 0.50) >= 1
    assert MAX_KELLY_BANKROLL_PCT == 0.025
    assert PROPOSAL_ACTIVE is True
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
