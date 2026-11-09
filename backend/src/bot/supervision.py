"""Run position monitoring beside the scan, not behind it.

One slow model call must not delay stop-loss checks. Stop still means no
automatic exits. Pause still allows them. The scan task does not place a new
order after the operator leaves Play.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict

_market_locks: Dict[str, asyncio.Lock] = {}
_monitor_gate = asyncio.Lock()


def market_execution_lock(market_id: str) -> asyncio.Lock:
    """Serialize buy and sell HTTP for one market. Does not lock unrelated markets."""
    key = (market_id or "").strip().upper() or "_"
    lock = _market_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _market_locks[key] = lock
    return lock


async def run_parallel_cycles(
    monitor_once: Callable[[], Awaitable[None]],
    scan_once: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    interval_sec: float,
) -> None:
    """Two loops. Each sleeps, then does its own work. Neither awaits the other."""

    async def _monitor() -> None:
        while not stop.is_set():
            await asyncio.sleep(interval_sec)
            if stop.is_set():
                return
            async with _monitor_gate:
                await monitor_once()

    async def _scan() -> None:
        while not stop.is_set():
            await asyncio.sleep(interval_sec)
            if stop.is_set():
                return
            await scan_once()

    monitor_task = asyncio.create_task(_monitor())
    scan_task = asyncio.create_task(_scan())
    try:
        await asyncio.gather(monitor_task, scan_task)
    finally:
        stop.set()
        monitor_task.cancel()
        scan_task.cancel()
        await asyncio.gather(monitor_task, scan_task, return_exceptions=True)
