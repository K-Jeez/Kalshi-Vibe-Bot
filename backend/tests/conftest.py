"""Keep pytest off the operator database, .env, and log files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_root = Path(tempfile.mkdtemp(prefix="vibe-pytest-"))
os.environ["VIBE_UNDER_TEST"] = "1"
os.environ["KALSHI_LOG_TO_FILE"] = "0"
os.environ["DATABASE_URL"] = f"sqlite:///{_root / 'test.db'}"
for _secret in (
    "KALSHI_API_KEY",
    "KALSHI_PRIVATE_KEY_PATH",
    "XAI_API_KEY",
    "XAI_MANAGEMENT_API_KEY",
    "LOCAL_API_TOKEN",
):
    os.environ[_secret] = ""
