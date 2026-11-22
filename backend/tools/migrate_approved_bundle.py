#!/usr/bin/env python3
"""Apply activity columns and the approved position cap without starting the bot.

From the backend directory, using the repo's existing virtualenv:

..\\venv\\Scripts\\python.exe tools\\migrate_approved_bundle.py trading_bot.db

The command first writes a new timestamped backup with the SQLite backup API.
It does not overwrite trading_bot.db.pre-bundle.bak. It does not call Kalshi.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.policy_migration import migrate_sqlite_file


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python tools/migrate_approved_bundle.py DATABASE", file=sys.stderr)
        return 2
    result = migrate_sqlite_file(Path(argv[0]), backup=True)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
