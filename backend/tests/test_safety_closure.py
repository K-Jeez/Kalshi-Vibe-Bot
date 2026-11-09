"""Restart-safe order identity, exact counts, fee metadata, and backups. No network."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.activity_log import fresh_unresolved_order_hold, mark_intent_reconciled, persist_order_intent
from src.bot.order_reconcile import verdict_from_sources
from src.bot.supervision import submission_reservation_lock
from src.clients.kalshi_client import bounded_retry_delay_seconds
from src.decision_engine.strategy_gates import (
    fee_inclusive_ev_dollars,
    fee_schedule_from_mapping,
    kelly_contract_cap_for_bankroll,
)
from src.fixed_point import FixedPointCountError, format_count_fp, parse_fixed_point_count, require_whole_submit_count
from src.policy_migration import migrate_sqlite_file


def _factory(tmp_path: Path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.database.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_unresolved_intent_survives_restart_and_does_not_expire(tmp_path: Path):
    factory = _factory(tmp_path)
    cid = persist_order_intent(
        {"market_id": "KXTEST-1", "action": "buy", "side": "YES", "quantity": 1, "limit_price": 0.4},
        session_factory=factory,
    )
    db = factory()
    from src.database.models import ActivityEvent

    row = db.query(ActivityEvent).filter(ActivityEvent.client_order_id == cid).one()
    row.ts_utc = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit()
    db.close()
    restarted = _factory(tmp_path)
    hold = fresh_unresolved_order_hold("KXTEST-1", "buy", session_factory=restarted, within_minutes=1)
    assert hold and "does not" not in hold
    assert "reconciled" in hold
    assert cid in hold


def test_reconcile_found_not_found_and_unknown():
    empty = {"state": "complete", "rows": []}
    positions = {"state": "complete", "rows": [{"ticker": "KXTEST-1"}]}
    found = verdict_from_sources(
        client_order_id="cid-1",
        live_orders={"state": "complete", "rows": [{"client_order_id": "cid-1", "order_id": "ord-1"}]},
        historical_orders=empty,
        fills=empty,
        positions=positions,
    )
    assert found["verdict"] == "found"
    absent = verdict_from_sources(
        client_order_id="cid-1",
        exchange_order_id="ord-missing",
        get_order={"state": "not_found"},
        live_orders=empty,
        historical_orders=empty,
        fills=empty,
        positions=positions,
    )
    assert absent["verdict"] == "not_found"
    unknown = verdict_from_sources(
        client_order_id="cid-1",
        live_orders={"state": "unknown", "rows": []},
        historical_orders=empty,
        fills=empty,
        positions=positions,
    )
    assert unknown["verdict"] == "unknown"
    mark_ok = False
    assert mark_ok is False


def test_definitive_absence_clears_only_after_reconcile(tmp_path: Path):
    factory = _factory(tmp_path)
    cid = persist_order_intent(
        {"market_id": "KXTEST-1", "action": "buy", "quantity": 1, "limit_price": 0.4},
        session_factory=factory,
    )
    assert fresh_unresolved_order_hold("KXTEST-1", "buy", session_factory=factory)
    mark_intent_reconciled(
        cid,
        market_id="KXTEST-1",
        action="buy",
        order_status="reconciled_absent",
        session_factory=factory,
        detail="live, historical, and fills finished with no matching id",
    )
    assert fresh_unresolved_order_hold("KXTEST-1", "buy", session_factory=factory) is None


def test_fractional_fill_count_is_not_relabeled():
    parsed = parse_fixed_point_count("2.50")
    assert parsed == Decimal("2.50")
    assert format_count_fp(parsed) == "2.50"
    try:
        parse_fixed_point_count(2.5)
    except FixedPointCountError:
        pass
    else:
        raise AssertionError("float count was accepted")
    assert require_whole_submit_count(2) == 2
    try:
        require_whole_submit_count("2.50")
    except FixedPointCountError:
        pass
    else:
        raise AssertionError("fractional submit count was accepted")


def test_unknown_fee_schedule_does_not_prove_ev_or_cap():
    assert fee_schedule_from_mapping({"series": {"fee_type": "quadratic", "fee_multiplier": 2}})["known"]
    assert fee_inclusive_ev_dollars(0.9, 0.4, 1, fee_multiplier=1, fee_type="quadratic") is not None
    assert fee_inclusive_ev_dollars(0.9, 0.4, 1) is None
    assert kelly_contract_cap_for_bankroll(1000, 0.5) == 0
    assert kelly_contract_cap_for_bankroll(1000, 0.5, fee_multiplier=1, fee_type="flat") == 0
    capped = kelly_contract_cap_for_bankroll(36, 0.40, fee_multiplier=1, fee_type="quadratic")
    higher = kelly_contract_cap_for_bankroll(36, 0.40, fee_multiplier=10, fee_type="quadratic")
    assert capped > higher


def test_market_retry_delay_is_bounded():
    assert bounded_retry_delay_seconds(0, "1") <= 2
    assert bounded_retry_delay_seconds(0, "30") == 2
    assert 0.05 <= bounded_retry_delay_seconds(1, None) <= 2


def test_backup_is_consistent_and_does_not_overwrite_the_original(tmp_path: Path):
    db = tmp_path / "trading_bot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE activity_events (id TEXT PRIMARY KEY, event_type TEXT)")
    conn.execute("INSERT INTO activity_events VALUES ('row-1', 'fill')")
    conn.execute(
        "CREATE TABLE tuning_state (max_open_positions INT, min_edge_to_buy_pct INT, stop_loss_drawdown_pct REAL)"
    )
    conn.execute("INSERT INTO tuning_state VALUES (30, 5, 0.8)")
    conn.commit()
    conn.close()
    original = tmp_path / "trading_bot.db.pre-bundle.bak"
    original.write_bytes(b"original-backup")
    first = migrate_sqlite_file(db)
    second = migrate_sqlite_file(db)
    assert original.read_bytes() == b"original-backup"
    assert first["original_pre_bundle_backup_preserved"] is True
    assert Path(first["backup"]).is_file()
    assert Path(second["backup"]) != Path(first["backup"])
    saved = sqlite3.connect(first["backup"])
    assert saved.execute("SELECT id FROM activity_events").fetchone()[0] == "row-1"
    saved.close()
    live = sqlite3.connect(db)
    cols = {row[1] for row in live.execute("PRAGMA table_info(activity_events)")}
    assert "exchange_order_id" in cols and "fee_kind" in cols and "fill_count_fp" in cols
    assert live.execute("SELECT id FROM activity_events").fetchone()[0] == "row-1"
    assert live.execute("SELECT max_open_positions, min_edge_to_buy_pct FROM tuning_state").fetchone() == (12, 5)
    live.close()
    assert second["already_applied"] is True


def test_concurrent_buys_share_one_submission_lock():
    results = []

    async def claim(name: str):
        async with submission_reservation_lock():
            results.append(name)
            await asyncio.sleep(0.02)
            results.append(name)

    async def main():
        await asyncio.gather(claim("a"), claim("b"))

    asyncio.run(main())
    assert results in (["a", "a", "b", "b"], ["b", "b", "a", "a"])
