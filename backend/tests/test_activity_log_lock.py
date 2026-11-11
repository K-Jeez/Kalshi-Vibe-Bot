"""A locked SQLite file should not drop an activity row after one attempt."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.activity_log import record_activity
from src.database.models import ActivityEvent, Base


def _factory(path):
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_locked_database_is_retried_and_the_message_is_logged(tmp_path, monkeypatch, caplog):
    path = tmp_path / "locked.db"
    factory = _factory(path)
    monkeypatch.setattr("src.activity_log._LOCK_WAITS", (0.02, 0.02, 0.02))
    monkeypatch.setattr("src.activity_log.ACTIVITY_BUSY_TIMEOUT_MS", 40)

    def hold_lock():
        holder = sqlite3.connect(path)
        holder.execute("BEGIN EXCLUSIVE")
        time.sleep(0.15)
        holder.commit()
        holder.close()

    threading.Thread(target=hold_lock, daemon=True).start()
    time.sleep(0.03)
    with caplog.at_level(logging.DEBUG, logger="activity_log"):
        saved = record_activity(
            {"event_type": "note", "market_id": "KXLOCK", "detail": "after the lock"},
            session_factory=factory,
        )
    assert saved is True
    db = factory()
    row = db.query(ActivityEvent).filter(ActivityEvent.market_id == "KXLOCK").one()
    db.close()
    assert row.detail == "after the lock"
    assert "database is locked" in caplog.text


def test_a_lock_that_outlasts_the_retries_logs_the_message(tmp_path, monkeypatch, caplog):
    path = tmp_path / "stuck.db"
    factory = _factory(path)
    monkeypatch.setattr("src.activity_log._LOCK_WAITS", (0.0, 0.0, 0.0))
    monkeypatch.setattr("src.activity_log.ACTIVITY_BUSY_TIMEOUT_MS", 30)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with caplog.at_level(logging.DEBUG, logger="activity_log"):
            saved = record_activity(
                {"event_type": "note", "market_id": "KXSTUCK", "detail": "not saved"},
                session_factory=factory,
            )
    finally:
        holder.commit()
        holder.close()
    assert saved is False
    assert "activity log write failed (OperationalError):" in caplog.text
    assert "database is locked" in caplog.text
    assert "Traceback" in caplog.text
