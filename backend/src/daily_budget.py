"""Stop new model calls once today's recorded xAI spend hits the daily cap.

The day is America/Denver. Position monitoring does not use this check.
Spend is the estimated dollars already stored on decision rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import settings
from src.decision_engine.history_sizing import _analysis_cost

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
