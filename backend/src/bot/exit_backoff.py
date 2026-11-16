"""Stop selling into an empty book on every monitor pass.

The clock is monotonic and in memory. Three missed exits mark the position
to wait for settlement instead of sending another order.
"""

from __future__ import annotations

import time
from typing import Optional

_attempts: dict[str, int] = {}
_next_at: dict[str, float] = {}


def exit_is_backed_off(market_id: str, *, now: Optional[float] = None) -> bool:
    key = str(market_id or "").strip().upper()
    nxt = _next_at.get(key)
    if nxt is None:
        return False
    stamp = time.monotonic() if now is None else float(now)
    return stamp < nxt


def note_empty_exit(market_id: str, *, now: Optional[float] = None) -> int:
    """Record a zero-fill exit. Returns the attempt count. The third waits for settlement."""
    key = str(market_id or "").strip().upper()
    stamp = time.monotonic() if now is None else float(now)
    count = int(_attempts.get(key) or 0) + 1
    _attempts[key] = count
    if count < 3:
        delay = 60.0 * (2 ** (count - 1))
        _next_at[key] = stamp + delay
    else:
        _next_at.pop(key, None)
    return count


def clear_exit_backoff(market_id: str) -> None:
    key = str(market_id or "").strip().upper()
    _attempts.pop(key, None)
    _next_at.pop(key, None)


def reset_for_tests() -> None:
    _attempts.clear()
    _next_at.clear()
