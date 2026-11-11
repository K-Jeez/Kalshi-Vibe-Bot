#!/usr/bin/env python3
"""Score a supplied decision file. It does not call a model or Kalshi.

Each row needs a decision time, an outcome time, the model's P(YES), the
executable ask, and the official outcome. Rows whose outcome is not after
the decision are excluded. This is not a backtest of a live day.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def score_rows(rows: list[dict]) -> dict:
    used = []
    excluded = []
    for row in rows:
        if not str(row.get("decided_at") or "").strip() or not str(row.get("resolved_at") or "").strip():
            excluded.append({"id": row.get("id"), "reason": "missing_time"})
            continue
        try:
            decided = _parse(str(row["decided_at"]))
            resolved = _parse(str(row["resolved_at"]))
        except (KeyError, ValueError):
            excluded.append({"id": row.get("id"), "reason": "bad_timestamp"})
            continue
        if resolved <= decided:
            excluded.append({"id": row.get("id"), "reason": "outcome_not_after_decision"})
            continue
        if row.get("outcome") not in ("yes", "no"):
            excluded.append({"id": row.get("id"), "reason": "missing_outcome"})
            continue
        used.append(row)
    if not used:
        return {
            "n": 0,
            "excluded": excluded,
            "brier_model": None,
            "brier_market": None,
            "note": "No comparable settled rows. This is not an edge result.",
        }
    model = 0.0
    market = 0.0
    for row in used:
        y = 1.0 if row["outcome"] == "yes" else 0.0
        p = float(row["model_p_yes"])
        m = float(row["market_p_yes"])
        model += (p - y) ** 2
        market += (m - y) ** 2
    n = len(used)
    return {
        "n": n,
        "prompt_version": used[0].get("prompt_version"),
        "brier_model": model / n,
        "brier_market": market / n,
        "excluded": excluded,
        "note": "Sample score only. Correlated legs are not independent trials.",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python tools/shadow_eval.py ROWS.json", file=sys.stderr)
        return 2
    rows = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    print(json.dumps(score_rows(rows), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
