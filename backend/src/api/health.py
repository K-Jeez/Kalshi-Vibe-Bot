from fastapi import APIRouter

from src.activity_log import pending_intent_count
from src.config import APPROVED_MAX_OPEN_POSITIONS
from src.decision_engine.strategy_gates import FEE_ESTIMATE_NOTE
from src.ops_status import alerts, monitor_age_sec
from src.util.datetimes import utc_iso_z, utc_now
from src.version import APP_VERSION

router = APIRouter(tags=["health"])


def public_health_body() -> dict:
    notes = list(alerts())
    pending = pending_intent_count()
    if pending is None:
        notes.append("Order intent log could not be read.")
    elif pending:
        notes.append(f"{pending} unconfirmed order intent(s) still pending.")
    return {
        "status": "healthy",
        "timestamp": utc_iso_z(utc_now()),
        "version": APP_VERSION,
        "monitor_age_sec": monitor_age_sec(),
        "alerts": notes,
        "outbound_alerts": False,
        "config_source": "database",
        "approved_max_open_positions": APPROVED_MAX_OPEN_POSITIONS,
        "fee_estimate_note": FEE_ESTIMATE_NOTE,
    }


@router.get("/health")
async def health():
    return public_health_body()
