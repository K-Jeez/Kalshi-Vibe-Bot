from fastapi import APIRouter

from src.activity_log import intent_status_summary
from src.config import APPROVED_MAX_OPEN_POSITIONS
from src.decision_engine.strategy_gates import FEE_ESTIMATE_NOTE
from src.ops_status import alerts, monitor_age_sec
from src.util.datetimes import utc_iso_z, utc_now
from src.version import APP_VERSION

router = APIRouter(tags=["health"])


def _entry_caps() -> list:
    try:
        from src.database.models import get_session_local
        from src.decision_engine.bucket_caps import load_bucket_caps

        db = get_session_local()()
        try:
            return load_bucket_caps(db)
        finally:
            db.close()
    except Exception:
        from src.decision_engine.bucket_caps import caps_from_closed

        return caps_from_closed([])


def _blend_weight() -> float:
    from src.decision_engine.blend import model_blend_weight

    return model_blend_weight()


def public_health_body() -> dict:
    notes = list(alerts())
    summary = intent_status_summary()
    current_count = None
    historical_pending = None
    legacy_count = None
    if summary is None:
        notes.append("Order intent log could not be read.")
    else:
        current_count = int(summary["current_unresolved_intent_count"])
        historical_pending = int(summary["historical_pending_row_count"])
        legacy_count = int(summary["legacy_unresolved_without_client_order_id"])
        if current_count == 1:
            notes.append(
                "1 order is still unconfirmed. Its latest status is pending, ambiguous, or open at the exchange."
            )
        elif current_count > 1:
            notes.append(
                f"{current_count} orders are still unconfirmed. "
                "Their latest status is pending, ambiguous, or open at the exchange."
            )
        if legacy_count == 1:
            notes.append(
                "1 legacy activity row has no client order id. It is not counted as a current order."
            )
        elif legacy_count > 1:
            notes.append(
                f"{legacy_count} legacy activity rows have no client order id. "
                "They are not counted as current orders."
            )
    return {
        "status": "healthy",
        "timestamp": utc_iso_z(utc_now()),
        "version": APP_VERSION,
        "monitor_age_sec": monitor_age_sec(),
        "alerts": notes,
        "current_unresolved_intent_count": current_count,
        "historical_pending_row_count": historical_pending,
        "legacy_unresolved_without_client_order_id": legacy_count,
        "outbound_alerts": False,
        "config_source": "database",
        "approved_max_open_positions": APPROVED_MAX_OPEN_POSITIONS,
        "fee_estimate_note": FEE_ESTIMATE_NOTE,
        "entry_caps": _entry_caps(),
        "model_blend_weight": _blend_weight(),
    }


@router.get("/health")
async def health():
    return public_health_body()
