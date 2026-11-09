"""Local status for the dashboard. No Kalshi call and no outbound message."""

from fastapi import APIRouter

from src.api.health import public_health_body

router = APIRouter(tags=["ops"])


@router.get("/ops/status")
async def ops_status():
    body = public_health_body()
    return {
        "monitor_age_sec": body["monitor_age_sec"],
        "alerts": body["alerts"],
        "outbound_alerts": body["outbound_alerts"],
        "config_source": body["config_source"],
        "approved_max_open_positions": body["approved_max_open_positions"],
        "fee_estimate_note": body["fee_estimate_note"],
    }
