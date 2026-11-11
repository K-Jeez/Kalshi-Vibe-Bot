"""Pytest must not append to the shared kalshi_bot.log."""

from __future__ import annotations

from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.bot.loop import logger as loop_logger
from src.logger import setup_logging


def test_pytest_does_not_write_the_live_log():
    live = Path(__file__).resolve().parents[1] / "logs" / "kalshi_bot.log"
    before = live.read_bytes() if live.exists() else None
    fresh = setup_logging("test-log-isolation")
    assert not any(isinstance(handler, RotatingFileHandler) for handler in fresh.handlers)
    assert not any(isinstance(handler, RotatingFileHandler) for handler in loop_logger.handlers)
    loop_logger.exception("series down")
    after = live.read_bytes() if live.exists() else None
    assert after == before
