"""Compare the model's claimed chance with settled bets. Reporting only."""

from __future__ import annotations

import math
from typing import Optional


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """95% interval for a win rate. None when there are no bets."""
    if n <= 0:
        return None, None
    p = wins / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - margin), min(1.0, centre + margin)


def track_record_note(rows: list[dict]) -> dict:
    """Plain summary of settled bets that still have the model's bought-side chance.

    ``rows`` items use ``won`` (bool), ``ai_side_pct`` (0–100 or missing),
    ``entry`` (dollars or missing), and ``pnl`` (dollars).
    """
    usable = [row for row in rows or [] if row.get("ai_side_pct") is not None]
    n = len(usable)
    if n < 8:
        return {
            "n": n,
            "note": "Not enough settled bets with a saved model chance to compare yet.",
        }
    wins = sum(1 for row in usable if row.get("won"))
    avg_ai = sum(float(row["ai_side_pct"]) for row in usable) / n
    rate = wins / n
    lo, hi = wilson_interval(wins, n)
    entries = [float(row["entry"]) for row in usable if row.get("entry") is not None]
    avg_entry = sum(entries) / len(entries) if entries else None
    pnl = sum(float(row.get("pnl") or 0.0) for row in usable)
    price = f"{avg_entry * 100:.0f}¢" if avg_entry is not None else "an unknown price"
    note = (
        f"On {n} settled bets the model gave the bought side a {avg_ai:.0f}% chance "
        f"and {rate * 100:.0f}% won (about {lo * 100:.0f}–{hi * 100:.0f}% allowing for sample size). "
        f"Average price {price}. Result ${pnl:.2f}."
    )
    return {
        "n": n,
        "wins": wins,
        "win_rate": round(rate, 4),
        "avg_model_pct": round(avg_ai, 1),
        "win_rate_low": None if lo is None else round(lo, 4),
        "win_rate_high": None if hi is None else round(hi, 4),
        "avg_entry": None if avg_entry is None else round(avg_entry, 4),
        "pnl": round(pnl, 2),
        "note": note,
    }
