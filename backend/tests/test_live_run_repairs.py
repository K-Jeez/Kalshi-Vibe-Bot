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
                "decided_at": "2024-11-09T00:00:00Z",
                "resolved_at": "2024-11-09T00:00:00Z",
                "outcome": "yes",
                "model_p_yes": 0.7,
                "market_p_yes": 0.4,
            },
            {
                "id": "ok",
                "decided_at": "2024-11-08T00:00:00Z",
                "resolved_at": "2024-11-09T00:00:00Z",
                "outcome": "yes",
                "model_p_yes": 0.7,
                "market_p_yes": 0.4,
                "prompt_version": "decision-2026-11-09",
            },
        ]
    )
    assert report["n"] == 1
    assert report["excluded"][0]["reason"] == "outcome_not_after_decision"
    missing = mod.score_rows([{"id": "no-time", "outcome": "yes", "model_p_yes": 0.5, "market_p_yes": 0.5}])
    assert missing["n"] == 0
    assert missing["excluded"][0]["reason"] == "missing_time"


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
    # 2.5% of $40 is $1.00. Ask 0.40 can buy one. Ask 0.51 on $10 cannot.
    fee = {"fee_type": "quadratic", "fee_multiplier": 1}
    rich = {"yes_ask": 0.40, "no_ask": 0.51, **fee}
    assert either_side_can_buy_one_capped_contract(rich, 40)
    neither = {"yes_ask": 0.51, "no_ask": 0.80, **fee}
    assert not either_side_can_buy_one_capped_contract(neither, 10)
    only_no = {"yes_ask": 0.90, "no_ask": 0.20, **fee}
    assert either_side_can_buy_one_capped_contract(only_no, 10)
    missing_yes_cheap_no = {"no_ask": 0.20, **fee}
    assert either_side_can_buy_one_capped_contract(missing_yes_cheap_no, 10)
    missing_yes_dear_no = {"no_ask": 0.90, **fee}
    assert not either_side_can_buy_one_capped_contract(missing_yes_dear_no, 10)
    assert kelly_contract_cap_for_bankroll(10, 0.50, fee_multiplier=1, fee_type="quadratic") == 0
    assert kelly_contract_cap_for_bankroll(40, 0.50, fee_multiplier=1, fee_type="quadratic") >= 1
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
            id TEXT, market_id TEXT, event_ticker TEXT, side TEXT, quantity INT, entry_price REAL,
            entry_cost REAL, fees_paid REAL, realized_pnl REAL, exit_reason TEXT, status TEXT, closed_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p1", "M1", "EV", "YES", 1, 0.4, 0.4, 0.02, 1.95, "stop_loss", "closed", "2026-11-09T00:33:00Z"),
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
    report = mod.audit_database(db, cash_start=100, cash_end=98.83)
    assert report["components"]["closed_count"] == 1
    assert report["components"]["sum_closed_realized_pnl"] == 1.95
    assert report["cash_comparison"]["resolved"] is False
    assert any(item.get("amount") for item in report["unresolved"])
    assert report["cash_comparison"]["unexplained_gap"] != 0


def test_migration_lowers_any_cap_above_12_and_leaves_the_stop(tmp_path: Path):
    from src.policy_migration import migrate_sqlite_file

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE tuning_state (
            max_open_positions INT,
            min_edge_to_buy_pct INT,
            min_ai_win_prob_buy_side_pct INT,
            stop_loss_drawdown_pct REAL
        )
        """
    )
    conn.execute("INSERT INTO tuning_state VALUES (20, 5, 60, 0.8)")
    conn.execute("INSERT INTO tuning_state VALUES (8, 5, 60, 0.8)")
    conn.commit()
    conn.close()
    report = migrate_sqlite_file(db)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT max_open_positions, min_edge_to_buy_pct, min_ai_win_prob_buy_side_pct, stop_loss_drawdown_pct FROM tuning_state ORDER BY max_open_positions"
    ).fetchall()
    conn.close()
    assert report["rows_updated"] == 1
    assert rows == [(12, 4, 55, 0.8), (12, 4, 55, 0.8)]


def test_installed_db_gates_are_corrected_once_after_the_cap_patch(tmp_path: Path):
    from src.policy_migration import GATES_POLICY_ID, POLICY_ID, migrate_sqlite_file

    db = tmp_path / "installed.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE tuning_state (
            max_open_positions INT,
            min_edge_to_buy_pct INT,
            min_ai_win_prob_buy_side_pct INT,
            stop_loss_drawdown_pct REAL,
            stop_loss_selling_enabled INT
        )
        """
    )
    conn.execute("INSERT INTO tuning_state VALUES (12, 4, 55, 0.8, 1)")
    conn.execute("CREATE TABLE _bot_patches (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO _bot_patches VALUES (?)", (POLICY_ID,))
    conn.commit()
    conn.close()
    first = migrate_sqlite_file(db)
    conn = sqlite3.connect(db)
    row = conn.execute(
        """
        SELECT max_open_positions, min_edge_to_buy_pct, min_ai_win_prob_buy_side_pct,
               stop_loss_drawdown_pct, stop_loss_selling_enabled
        FROM tuning_state
        """
    ).fetchone()
    conn.execute("UPDATE tuning_state SET min_edge_to_buy_pct = 7")
    conn.commit()
    conn.close()
    second = migrate_sqlite_file(db)
    conn = sqlite3.connect(db)
    edited = conn.execute("SELECT min_edge_to_buy_pct FROM tuning_state").fetchone()[0]
    conn.close()
    assert first["gates_already_applied"] is False
    assert first["gates_policy_id"] == GATES_POLICY_ID
    assert first["previous_gates"]["min_edge_to_buy_pct"] == [4]
    assert first["previous_gates"]["min_ai_win_prob_buy_side_pct"] == [55]
    assert row == (12, 4, 55, 0.8, 1)
    assert "stop_loss_selling_enabled" in first["gates_left_untouched"]
    assert second["gates_already_applied"] is True
    assert edited == 7


def test_export_counts_real_trades_and_only_the_latest_unresolved_intent(tmp_path: Path):
    import importlib.util

    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE trades (
            id TEXT PRIMARY KEY, market_id TEXT, market_title TEXT, action TEXT, side TEXT,
            quantity INT, price REAL, total_cost REAL, realized_pnl REAL, timestamp TEXT, trade_mode TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("t1", "M1", "One", "buy", "YES", 1, 0.4, 0.4, 0, "2024-01-01", "live"),
            ("t2", "M2", "Two", "sell", "NO", 1, 0.3, 0.3, 0.1, "2024-01-02", "live"),
            ("t3", "M3", "Three", "buy", "YES", 2, 0.5, 1.0, 0, "2024-01-03", "live"),
        ],
    )
    conn.execute(
        """
        CREATE TABLE positions (
            id TEXT, market_id TEXT, event_ticker TEXT, side TEXT, quantity INT, entry_price REAL,
            entry_cost REAL, fees_paid REAL, realized_pnl REAL, exit_reason TEXT, status TEXT, closed_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p1", "M1", "EV", "YES", 1, 0.4, 59.46, 3.0384, 1.95, "stop_loss", "closed", "2024-11-08"),
    )
    conn.execute(
        """
        CREATE TABLE activity_events (
            id TEXT, run_id TEXT, ts_utc TEXT, event_type TEXT, market_id TEXT, action TEXT,
            client_order_id TEXT, order_status TEXT, fees_dollars REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO activity_events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("a1", "run-old", "2024-01-01T00:00:00", "order_intent", "M1", "buy", "cid-1", "pending", None),
            ("a2", "run-old", "2024-01-01T00:01:00", "order_ack", "M1", "buy", "cid-1", "filled", 0.02),
            ("a3", "run-old", "2024-01-02T00:00:00", "order_intent", "M2", "sell", "cid-2", "pending", None),
            ("a4", "run-new", "2024-01-02T00:02:00", "order_ack", "M2", "sell", "cid-2", "reconciled_filled", None),
            ("a5", "run-new", "2024-01-03T00:00:00", "order_intent", "M3", "buy", "cid-3", "pending", None),
            ("a6", "legacy", "2024-01-04T00:00:00", "order_intent", "M4", "buy", "", "pending", None),
        ],
    )
    conn.commit()
    conn.close()
    spec = importlib.util.spec_from_file_location(
        "reconcile_export_schema",
        Path(__file__).resolve().parents[1] / "tools" / "reconcile_export.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    report = mod.audit_database(db, cash_start=100.0, cash_end=101.77)
    assert report["components"]["trade_row_count"] == 3
    assert report["components"]["trade_scope"] == "all_trade_rows_in_this_database"
    assert any("no fees column" in gap for gap in report["schema_gaps"])
    assert report["components"]["historical_pending_row_count"] == 4
    assert report["components"]["current_unresolved_intent_count"] == 1
    assert report["components"]["legacy_unresolved_without_client_order_id"] == 1
    assert report["components"]["sum_closed_realized_pnl"] == 1.95
    assert report["components"]["sum_closed_entry_cost"] == 59.46
    assert report["components"]["sum_closed_fees_paid"] == 3.0384
    assert report["cash_comparison"]["resolved"] is False
    assert report["cash_comparison"]["unexplained_gap"] == round(1.77 - 1.95, 4)
    assert sorted(report["components"]["run_ids"]) == ["legacy", "run-new", "run-old"]


def test_restore_source_cannot_load_an_old_open_cap_of_30(monkeypatch):
    monkeypatch.setenv("BOT_MAX_OPEN_POSITIONS", "30")
    from src.config import Settings

    fresh = Settings()
    assert "bot_max_open_positions" not in Settings.model_fields
    assert not hasattr(fresh, "bot_max_open_positions")


def test_stale_database_cap_does_not_raise_the_runtime_limit():
    from types import SimpleNamespace

    from src.api.tuning import _sync_runtime_from_row
    from src.config import settings as app_settings

    previous = (
        app_settings.min_edge_to_buy_pct,
        app_settings.min_ai_win_prob_buy_side_pct,
        app_settings.stop_loss_drawdown_pct,
        app_settings.stop_loss_selling_enabled,
    )
    row = SimpleNamespace(
        stop_loss_drawdown_pct=0.80,
        min_edge_to_buy_pct=5,
        stop_loss_selling_enabled=False,
        min_ai_win_prob_buy_side_pct=60,
        max_open_positions=30,
    )
    try:
        _sync_runtime_from_row(row)
        assert not hasattr(app_settings, "bot_max_open_positions")
        assert app_settings.min_edge_to_buy_pct == 5
        assert app_settings.min_ai_win_prob_buy_side_pct == 60
        assert abs(app_settings.stop_loss_drawdown_pct - 0.80) < 1e-9
    finally:
        (
            app_settings.min_edge_to_buy_pct,
            app_settings.min_ai_win_prob_buy_side_pct,
            app_settings.stop_loss_drawdown_pct,
            app_settings.stop_loss_selling_enabled,
        ) = previous


def test_unconfirmed_buys_reserve_cash_and_block_when_unknown():
    from src.bot.exposure_reserve import unconfirmed_buy_exposure

    current = {
        "event_type": "order_intent",
        "action": "buy",
        "order_status": "pending",
        "client_order_id": "this-order",
        "event_ticker": "EVT",
        "market_id": "EVT-A",
        "quantity": 2,
        "limit_price": 0.40,
    }
    other = dict(current, client_order_id="other-order", market_id="EVT-B")
    resting = {
        "status": "resting",
        "action": "buy",
        "event_ticker": "EVT",
        "ticker": "EVT-C",
        "side": "yes",
        "yes_price_dollars": "0.50",
        "remaining_count_fp": "1.00",
    }
    known = unconfirmed_buy_exposure(
        local_rows=[current, other],
        resting_orders=[resting],
        resting_known=True,
        event_ticker="EVT",
        market_id="EVT-A",
        exclude_client_order_id="this-order",
        fee_type="quadratic",
        fee_multiplier=1,
    )
    assert known["reason"] is None
    assert known["extra_orders"] == 2
    assert known["reserve_dollars"] > 0
    missing = unconfirmed_buy_exposure(
        local_rows=[{**other, "limit_price": None}],
        resting_orders=[],
        resting_known=True,
        event_ticker="EVT",
        market_id="EVT-A",
        exclude_client_order_id="this-order",
        fee_type="quadratic",
        fee_multiplier=1,
    )
    assert missing["reason"] == "pending buy is missing a price or size"
    unread = unconfirmed_buy_exposure(
        local_rows=[],
        resting_orders=None,
        resting_known=False,
        event_ticker="EVT",
        market_id="EVT-A",
        exclude_client_order_id="this-order",
        fee_type="quadratic",
        fee_multiplier=1,
    )
    assert unread["reserve_dollars"] is None


def test_uncertainty_is_structured_and_does_not_invent_a_probability():
    from src.clients.xai_client import _parse_json, attach_model_usage
    import time

    old = _parse_json(json.dumps({
        "direction": "YES",
        "ai_probability_yes_pct": 70,
        "reasoning": "old shape",
    }))
    assert old["uncertainty"]["status"] == "missing"
    assert old["direction"] == "YES"
    assert old["ai_probability_yes_pct"] == 70
    abstain = _parse_json(json.dumps({
        "direction": "YES",
        "ai_probability_yes_pct": 70,
        "uncertainty": {"level": "abstain", "missing": ["score"], "as_of": "2024-11-08T12:00:00Z"},
    }))
    assert abstain["direction"] == "SKIP"
    assert abstain["ai_probability_yes_pct"] == 70
    assert abstain["uncertainty"]["missing"] == ["score"]
    invalid = _parse_json(json.dumps({
        "direction": "NO",
        "ai_probability_yes_pct": 40,
        "uncertainty": "high",
    }))
    assert invalid["uncertainty"]["status"] == "invalid"
    assert invalid["direction"] == "NO"
    assert invalid["ai_probability_yes_pct"] == 40
    out = {}
    attach_model_usage(
        out,
        {"usage": {"prompt_tokens": 1000, "completion_tokens": 10, "total_cost_usd": 0.01}, "choices": []},
        started=time.perf_counter() - 0.2,
    )
    assert out["usage"]["actual_usd"] == 0.01
    assert out["usage"]["cost_label"] == "provider_reported"
    assert out["usage"]["input_tokens"] == 1000
    assert out["usage"]["duration_sec"] >= 0
    assert out["prompt_version"] == "decision-2026-11-11"
    assert "choices" not in out["usage"]
    estimate = {}
    attach_model_usage(
        estimate,
        {"usage": {"prompt_tokens": 1000, "completion_tokens": 0}},
        started=time.perf_counter(),
    )
    assert estimate["usage"]["actual_usd"] is None
    assert estimate["usage"]["cost_label"] == "list_price_estimate"
    assert estimate["usage"]["estimated_usd"] is not None


def test_buy_window_default_is_four_hours_and_is_not_in_the_database(monkeypatch):
    from src.config import Settings
    from src.database.models import TuningState

    monkeypatch.delenv("BOT_MAX_HOURS", raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.bot_max_hours == 4
    assert "bot_max_hours" not in TuningState.__table__.columns
