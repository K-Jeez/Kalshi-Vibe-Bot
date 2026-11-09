"""Apply the approved sizing bundle and additive activity columns.

Does not start the bot and does not call Kalshi. Before any write, the
SQLite backup API copies a consistent snapshot, including a WAL left by a
killed process. Each run writes a new timestamped file. An older
``trading_bot.db.pre-bundle.bak`` is identified and is not overwritten.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

POLICY_ID = "approved_bundle_2026_11_09_v2"
OLD_MAX_OPEN = 30
NEW_MAX_OPEN = 12

# Columns the activity log can add without rewriting existing rows.
_ACTIVITY_COLUMNS = (
    ("seq", "INTEGER NOT NULL DEFAULT 0"),
    ("ts_utc", "DATETIME"),
    ("run_id", "TEXT"),
    ("app_version", "TEXT"),
    ("git_sha", "TEXT"),
    ("event_type", "TEXT"),
    ("market_id", "TEXT"),
    ("event_ticker", "TEXT"),
    ("side", "TEXT"),
    ("action", "TEXT"),
    ("model_id", "TEXT"),
    ("ai_probability_yes_pct", "INTEGER"),
    ("market_implied_pct", "INTEGER"),
    ("edge_pct", "FLOAT"),
    ("sizing_tag", "TEXT"),
    ("kelly_contracts", "INTEGER"),
    ("quantity", "INTEGER"),
    ("limit_price", "FLOAT"),
    ("fill_price", "FLOAT"),
    ("fill_qty", "FLOAT"),
    ("fill_count_fp", "TEXT"),
    ("fees_dollars", "FLOAT"),
    ("client_order_id", "TEXT"),
    ("exchange_order_id", "TEXT"),
    ("fee_kind", "TEXT"),
    ("order_status", "TEXT"),
    ("quote_captured_at", "TEXT"),
    ("quote_age_ms", "INTEGER"),
    ("yes_bid", "FLOAT"),
    ("yes_ask", "FLOAT"),
    ("no_bid", "FLOAT"),
    ("no_ask", "FLOAT"),
    ("settings_json", "TEXT"),
    ("detail", "TEXT"),
    ("error", "TEXT"),
)


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


def consistent_sqlite_backup(path: Path) -> Path:
    """Transaction-consistent copy via the SQLite backup API. Never overwrites."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 0
    while dest.exists():
        suffix += 1
        dest = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
    source = sqlite3.connect(str(path))
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return dest


def ensure_activity_columns(conn: sqlite3.Connection) -> list[str]:
    """Add missing activity columns. Existing rows and other tables stay."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()}
    added: list[str] = []
    if not present:
        definitions = ", ".join(f"{name} {typedef}" for name, typedef in _ACTIVITY_COLUMNS)
        conn.execute(
            f"CREATE TABLE activity_events (id TEXT PRIMARY KEY, {definitions})"
        )
        return ["activity_events"]
    for name, typedef in _ACTIVITY_COLUMNS:
        if name in present:
            continue
        conn.execute(f"ALTER TABLE activity_events ADD COLUMN {name} {typedef}")
        added.append(name)
    return added


def migrate_sqlite_file(path: Path, *, backup: bool = True) -> dict:
    if not path.is_file():
        return {"ok": False, "reason": "database file not found", "path": str(path)}
    original_backup = path.with_name(path.name + ".pre-bundle.bak")
    backup_path = consistent_sqlite_backup(path) if backup else None
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _bot_patches (id TEXT PRIMARY KEY)"
        )
        added_columns = ensure_activity_columns(conn)
        already = conn.execute(
            "SELECT 1 FROM _bot_patches WHERE id = ?",
            (POLICY_ID,),
        ).fetchone()
        if already:
            conn.commit()
            return {
                "ok": True,
                "already_applied": True,
                "columns_added": added_columns,
                "backup": str(backup_path) if backup_path else None,
                "original_pre_bundle_backup": str(original_backup) if original_backup.is_file() else None,
                "original_pre_bundle_backup_preserved": original_backup.is_file(),
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
            "columns_added": added_columns,
            "backup": str(backup_path) if backup_path else None,
            "original_pre_bundle_backup": str(original_backup) if original_backup.is_file() else None,
            "original_pre_bundle_backup_preserved": original_backup.is_file(),
            "policy_id": POLICY_ID,
        }
    finally:
        conn.close()


def migrate_database_url(database_url: str) -> dict:
    path = sqlite_path_from_url(database_url)
    if path is None or not path.is_file():
        return {"ok": True, "skipped": True}
    return migrate_sqlite_file(path, backup=True)
