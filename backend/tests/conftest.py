"""Keep pytest from appending to the live bot log."""

from __future__ import annotations

import os

os.environ["KALSHI_LOG_TO_FILE"] = "0"
