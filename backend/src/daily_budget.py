"""Stop new model calls once today's recorded xAI spend hits the daily cap.

The day is America/Denver. Position monitoring does not use this check.
Spend is the estimated dollars already stored on decision rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import settings
def _analysis_cost(raw_analysis) -> float:
    import json

    try:
        analysis = json.loads(raw_analysis) if isinstance(raw_analysis, str) else (raw_analysis or {})
    except (TypeError, ValueError):
        return 0.0
    usage = analysis.get("usage") if isinstance(analysis, dict) and isinstance(analysis.get("usage"), dict) else {}
    try:
        return max(0.0, float(usage.get("estimated_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0

_DENVER = ZoneInfo("America/Denver")
_note = ""


def denver_day_utc_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(_DENVER)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def spent_today_usd(db, now: datetime | None = None) -> float:
    from src.database.models import DecisionLog

    start, end = denver_day_utc_bounds(now)
    try:
        rows = (
            db.query(DecisionLog.xai_analysis)
            .filter(DecisionLog.timestamp >= start, DecisionLog.timestamp < end)
            .all()
        )
    except Exception:
        return 0.0
    total = 0.0
    for (raw,) in rows:
        total += _analysis_cost(raw)
    return round(total, 6)


def budget_status(db, now: datetime | None = None) -> dict:
    """Refresh the dashboard note and say whether new model calls should stop."""
    global _note
    cap = float(getattr(settings, "daily_xai_budget_usd", 3) or 0)
    spent = spent_today_usd(db, now)
    reached = cap > 0 and spent + 1e-9 >= cap
    _note = "Daily AI budget reached, analysis resumes tomorrow" if reached else ""
    return {"spent": spent, "cap": cap, "reached": reached, "note": _note}


def current_note() -> str:
    return _note
