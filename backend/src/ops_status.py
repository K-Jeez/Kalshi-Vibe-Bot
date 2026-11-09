"""Local health notes. Nothing here sends mail, chat, or exchange orders."""

from __future__ import annotations

import time
from typing import List, Optional

_last_monitor_at: float = 0.0
_rejections: List[float] = []
_last_api_error: str = ""


def stamp_monitor(now: Optional[float] = None) -> None:
    global _last_monitor_at
    _last_monitor_at = time.monotonic() if now is None else float(now)


def note_rejection(now: Optional[float] = None) -> None:
    current = time.monotonic() if now is None else float(now)
    _rejections.append(current)


def note_api_error(message: str) -> None:
    global _last_api_error
    _last_api_error = (message or "")[:180]


def monitor_age_sec(now: Optional[float] = None) -> Optional[float]:
    if _last_monitor_at <= 0:
        return None
    current = time.monotonic() if now is None else float(now)
    return max(0.0, current - _last_monitor_at)


def alerts(*, now: Optional[float] = None, monitor_stale_after: float = 90.0) -> List[str]:
    """Short local warnings. Empty means nothing in this snapshot looks late."""
    current = time.monotonic() if now is None else float(now)
    out: List[str] = []
    age = monitor_age_sec(current)
    if age is None:
        out.append("Position monitor has not reported yet.")
    elif age > monitor_stale_after:
        out.append(f"Position monitor last ran {int(age)}s ago.")
    recent = [t for t in _rejections if current - t < 600]
    del _rejections[: len(_rejections) - len(recent)]
    if len(recent) >= 5:
        out.append(f"{len(recent)} order rejections in the last 10 minutes.")
    if _last_api_error:
        out.append(f"Last API error: {_last_api_error}")
    return out


def reset_for_tests() -> None:
    global _last_monitor_at, _last_api_error
    _last_monitor_at = 0.0
    _last_api_error = ""
    _rejections.clear()
