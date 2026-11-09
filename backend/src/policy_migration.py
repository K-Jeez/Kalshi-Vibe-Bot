"""Apply the approved sizing bundle to an existing SQLite file.

Does not start the bot. Copies a backup beside the database first.
Any stored open-position cap above 12 is lowered to 12.
A stored cap that is already 12 or lower is left alone.
Edge, win probability, and stop-loss rows are left as they are.
Kelly and the entry cap live in code, not in this table.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

POLICY_ID = "approved_bundle_2026_11_09_v1"
OLD_MAX_OPEN = 30
NEW_MAX_OPEN = 12


def sqlite_path_from_url(database_url: str) -> Optional[Path]:
    raw = (database_url or "").strip()
    if not raw.lower().startswith("sqlite:"):
        return None
    parsed = urlparse(raw)
    path = unquote(parsed.path or "")
    if path.startswith("/") and len(path) > 3 and path[2] == ":":
        path = path[1:]
    if not path:
        return None
    return Path(path)


def migrate_sqlite_file(path: Path, *, backup: bool = True) -> dict:
    if not path.is_file():
        return {"ok": False, "reason": "database file not found", "path": str(path)}
    backup_path = None
    if backup:
        backup_path = path.with_name(path.name + ".pre-bundle.bak")
        if not backup_path.exists():
            shutil.copy2(path, backup_path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _bot_patches (id TEXT PRIMARY KEY)"
        )
        already = conn.execute(
            "SELECT 1 FROM _bot_patches WHERE id = ?",
            (POLICY_ID,),
        ).fetchone()
        if already:
            return {
                "ok": True,
                "already_applied": True,
                "backup": str(backup_path) if backup_path else None,
            }
        previous = []
        try:
            previous = [
                row[0]
                for row in conn.execute("SELECT max_open_positions FROM tuning_state").fetchall()
            ]
        except sqlite3.OperationalError:
            previous = []
        changed = 0
        try:
            changed = conn.execute(
                "UPDATE tuning_state SET max_open_positions = ? WHERE max_open_positions > ?",
                (NEW_MAX_OPEN, NEW_MAX_OPEN),
            ).rowcount
        except sqlite3.OperationalError:
            changed = 0
        conn.execute("INSERT INTO _bot_patches (id) VALUES (?)", (POLICY_ID,))
        conn.commit()
        return {
            "ok": True,
            "already_applied": False,
            "previous_max_open_positions": previous,
            "rows_updated": int(changed or 0),
            "max_open_positions": NEW_MAX_OPEN,
            "unchanged": ["min_edge_to_buy_pct", "min_ai_win_prob_buy_side_pct", "stop_loss_drawdown_pct"],
            "backup": str(backup_path) if backup_path else None,
            "policy_id": POLICY_ID,
        }
    finally:
        conn.close()


def migrate_database_url(database_url: str) -> dict:
    path = sqlite_path_from_url(database_url)
    if path is None or not path.is_file():
        return {"ok": True, "skipped": True}
    return migrate_sqlite_file(path, backup=True)
