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


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())
        if record.exc_info:
            self.lines.append("Traceback")


def _capture_activity_log():
    target = logging.getLogger("activity_log")
    handler = _Capture()
    previous = target.level
    target.setLevel(logging.DEBUG)
    target.addHandler(handler)
    return target, handler, previous


def test_locked_database_is_retried_and_the_message_is_logged(tmp_path, monkeypatch):
    path = tmp_path / "locked.db"
    factory = _factory(path)
    monkeypatch.setattr("src.activity_log.ACTIVITY_BUSY_TIMEOUT_MS", 400)

    def hold_lock():
        holder = sqlite3.connect(path)
        holder.execute("BEGIN EXCLUSIVE")
        time.sleep(0.05)
        holder.commit()
        holder.close()

    threading.Thread(target=hold_lock, daemon=True).start()
    time.sleep(0.02)
    target, handler, previous = _capture_activity_log()
    try:
        started = time.monotonic()
        saved = record_activity(
            {"event_type": "note", "market_id": "KXLOCK", "detail": "after the lock"},
            session_factory=factory,
        )
        elapsed = time.monotonic() - started
    finally:
        target.removeHandler(handler)
        target.setLevel(previous)
    assert saved is True
    assert elapsed < 2.0
    db = factory()
    row = db.query(ActivityEvent).filter(ActivityEvent.market_id == "KXLOCK").one()
    db.close()
    assert row.detail == "after the lock"


def test_a_lock_that_outlasts_the_retries_logs_the_message(tmp_path, monkeypatch):
    path = tmp_path / "stuck.db"
    factory = _factory(path)
    monkeypatch.setattr("src.activity_log.ACTIVITY_BUSY_TIMEOUT_MS", 30)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN EXCLUSIVE")
    target, handler, previous = _capture_activity_log()
    try:
        started = time.monotonic()
        saved = record_activity(
            {"event_type": "note", "market_id": "KXSTUCK", "detail": "not saved"},
            session_factory=factory,
        )
        elapsed = time.monotonic() - started
    finally:
        holder.commit()
        holder.close()
        target.removeHandler(handler)
        target.setLevel(previous)
    assert saved is False
    assert elapsed < 1.0
    text = "\n".join(handler.lines)
    assert "activity log write failed (OperationalError):" in text
    assert "database is locked" in text
    assert "Traceback" in text
