"""Short in-memory pause after a shard routing rejection.

This is not a permanent blacklist and it is not stored in the database.
A later sweep may try the ticker again after the window.
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

_until: Dict[str, Tuple[float, str]] = {}
DEFAULT_MINUTES = 30


def note_untradeable(market_id: str, reason: str, *, minutes: int = DEFAULT_MINUTES, now: Optional[float] = None) -> None:
    key = (market_id or "").strip().upper()
    if not key:
        return
    start = time.monotonic() if now is None else float(now)
    _until[key] = (start + max(1, int(minutes)) * 60.0, reason)


def untradeable_reason(market_id: str, *, now: Optional[float] = None) -> Optional[str]:
    key = (market_id or "").strip().upper()
    row = _until.get(key)
    if row is None:
        return None
    deadline, reason = row
    current = time.monotonic() if now is None else float(now)
    if current >= deadline:
        _until.pop(key, None)
        return None
    return reason


def clear_untradeable() -> None:
    _until.clear()
