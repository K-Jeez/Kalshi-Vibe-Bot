"""Remember the markets-list cursor across process restarts.

A cursor older than 30 minutes is dropped so a restart does not keep
walking a stale slice. This is not a new trade threshold.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

STALE_SEC = 30 * 60


def checkpoint_path(base: Path) -> Path:
    return base / ".vibe-control" / "scan_cursor.json"


def write_scan_checkpoint(path: Path, cursor: Optional[str], key: Optional[str], *, now: Optional[float] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cursor": cursor or "",
        "key": key or "",
        "saved_at": time.time() if now is None else float(now),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def read_scan_checkpoint(path: Path, *, now: Optional[float] = None, stale_sec: float = STALE_SEC) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    saved = float(payload.get("saved_at") or 0)
    current = time.time() if now is None else float(now)
    if current - saved > float(stale_sec):
        return None
    cursor = str(payload.get("cursor") or "").strip()
    return cursor or None
