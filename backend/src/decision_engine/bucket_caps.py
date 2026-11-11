"""Per-bet cash cap from closed results in each raw-edge bucket.

Every bucket starts at 2.5% of available cash. After at least eight closed
positions, a negative fee-inclusive return moves the cap toward 1% and a
positive return moves it toward 5%, in steps of 0.25 percentage points.
Quarter Kelly is unchanged. The pre-model screen still uses 2.5% because the
edge is not known until the model returns.
"""

from __future__ import annotations

import math
from typing import Optional

BASE_CAP = 0.025
FLOOR_CAP = 0.01
CEILING_CAP = 0.05
MIN_SAMPLE = 8
STEP = 0.0025

BUCKETS = ("4-6", "6-8", "8-10", "10-13", "13+")


def edge_bucket(edge_pts: float) -> Optional[str]:
    edge = float(edge_pts)
    if not math.isfinite(edge) or edge < 4.0:
        return None
    if edge < 6.0:
        return "4-6"
    if edge < 8.0:
        return "6-8"
    if edge < 10.0:
        return "8-10"
    if edge < 13.0:
        return "10-13"
    return "13+"


def _snap(raw: float) -> float:
    ticks = int(math.floor((float(raw) - FLOOR_CAP) / STEP + 0.5))
    snapped = FLOOR_CAP + ticks * STEP
    return max(FLOOR_CAP, min(CEILING_CAP, round(snapped, 4)))


def cap_fraction_for_return(realized_pnl: float, cost: float, n: int) -> float:
    """Cap for one bucket. Fewer than eight closes stays at 2.5%."""
    if int(n) < MIN_SAMPLE or float(cost) <= 1e-9:
        return BASE_CAP
    roi = float(realized_pnl) / float(cost)
    if not math.isfinite(roi):
        return BASE_CAP
    if roi >= 0:
        raw = BASE_CAP + min(1.0, roi) * (CEILING_CAP - BASE_CAP)
    else:
        raw = BASE_CAP + max(-1.0, roi) * (BASE_CAP - FLOOR_CAP)
    return _snap(raw)


def caps_from_closed(rows: list[dict]) -> list[dict]:
    """``rows`` are closed positions with ``edge``, ``realized_pnl``, and ``cost``."""
    grouped = {name: {"n": 0, "realized": 0.0, "cost": 0.0} for name in BUCKETS}
    for row in rows or []:
        name = edge_bucket(float(row.get("edge") or 0.0))
        if name is None:
            continue
        grouped[name]["n"] += 1
        grouped[name]["realized"] += float(row.get("realized_pnl") or 0.0)
        grouped[name]["cost"] += max(0.0, float(row.get("cost") or 0.0))
    out = []
    for name in BUCKETS:
        item = grouped[name]
        cap = cap_fraction_for_return(item["realized"], item["cost"], item["n"])
        roi = None
        if item["n"] >= MIN_SAMPLE and item["cost"] > 1e-9:
            roi = round(item["realized"] / item["cost"], 4)
        out.append(
            {
                "bucket": name,
                "cap_fraction": cap,
                "cap_pct": round(cap * 100.0, 2),
                "n": item["n"],
                "roi": roi,
            }
        )
    return out


def cap_for_edge(table: list[dict], edge_pts: float) -> float:
    name = edge_bucket(edge_pts)
    if name is None:
        return BASE_CAP
    for row in table or []:
        if row.get("bucket") == name:
            return float(row.get("cap_fraction") or BASE_CAP)
    return BASE_CAP


def load_bucket_caps(db) -> list[dict]:
    """Closed positions joined to the entry decision's raw edge. Missing links are ignored."""
    from src.database.models import DecisionLog, Position

    rows = []
    try:
        found = (
            db.query(Position.realized_pnl, Position.entry_cost, Position.fees_paid, DecisionLog.edge)
            .join(DecisionLog, DecisionLog.id == Position.entry_decision_log_id)
            .filter(Position.status == "closed")
            .all()
        )
    except Exception:
        return caps_from_closed([])
    for realized, cost, fees, edge in found:
        rows.append(
            {
                "edge": float(edge or 0.0),
                "realized_pnl": float(realized or 0.0),
                "cost": float(cost or 0.0) + float(fees or 0.0),
            }
        )
    return caps_from_closed(rows)
