"""Run position monitoring beside the scan, not behind it.

One slow model call must not delay stop-loss checks. Stop still means no
automatic exits. Pause still allows them. The scan task does not place a new
order after the operator leaves Play.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict

_market_locks: Dict[str, asyncio.Lock] = {}
_monitor_gate = asyncio.Lock()
_submission_lock = asyncio.Lock()


def submission_reservation_lock() -> asyncio.Lock:
    """One short lock around the buy reservation and submit.

    It is not held during a model call. Sell checks do not take it, so a buy
    in one event does not stop the exit monitor from running.
    """
    return _submission_lock


def market_execution_lock(market_id: str) -> asyncio.Lock:
    """Serialize buy and sell HTTP for one market. Does not lock unrelated markets."""
    key = (market_id or "").strip().upper() or "_"
    _prune_idle_locks(keep=key)
    lock = _market_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _market_locks[key] = lock
    return lock


def _prune_idle_locks(*, keep: str, limit: int = 128) -> None:
    """Drop unused locks once the map is large. A lock that is held stays."""
    if len(_market_locks) <= limit:
        return
    for key, lock in list(_market_locks.items()):
        if key == keep or lock.locked():
            continue
        _market_locks.pop(key, None)
        if len(_market_locks) <= limit:
            return


async def run_parallel_cycles(
    monitor_once: Callable[[], Awaitable[None]],
    scan_once: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    interval_sec: float,
) -> None:
    """Two loops. Each sleeps, then does its own work. Neither awaits the other."""

    async def _loop(name: str, work: Callable[[], Awaitable[None]]) -> None:
        while not stop.is_set():
            try:
                await asyncio.sleep(interval_sec)
            except asyncio.CancelledError:
                raise
            if stop.is_set():
                return
            try:
                if name == "monitor":
                    async with _monitor_gate:
                        await work()
                else:
                    await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One pass failing must not cancel the other task or end this loop.
                logging.getLogger("bot.supervision").error(
                    "%s pass failed (%s); next pass still runs",
                    name,
                    type(exc).__name__,
                )
                try:
                    await asyncio.sleep(min(10.0, max(float(interval_sec), 0.05)))
                except asyncio.CancelledError:
                    raise

    monitor_task = asyncio.create_task(_loop("monitor", monitor_once))
    scan_task = asyncio.create_task(_loop("scan", scan_once))
    try:
        await asyncio.gather(monitor_task, scan_task)
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        stop.set()
        for task in (monitor_task, scan_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(monitor_task, scan_task, return_exceptions=True)
