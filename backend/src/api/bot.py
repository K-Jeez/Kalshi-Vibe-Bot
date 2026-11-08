from src.util.datetimes import utc_iso_z, utc_now

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.api.broadcast import broadcast_update
from src.api.common import ensure_bot_state
from src.api.schemas import BotStateRequest
from src.database.models import get_db
from src.logger import logger

router = APIRouter(tags=["bot"])


@router.get("/bot/state")
async def get_bot_state(db: Session = Depends(get_db)):
    row = ensure_bot_state(db)
    return {
        "state": row.state,
        "updated_at": utc_iso_z(row.updated_at),
    }


@router.post("/bot/state")
async def set_bot_state(req: BotStateRequest, db: Session = Depends(get_db)):
    if req.state not in ("play", "pause", "stop"):
        raise HTTPException(status_code=400, detail="state must be play, pause, or stop")
    row = ensure_bot_state(db)
    row.state = req.state
    row.updated_at = utc_now()
    db.commit()
    logger.info("Bot state -> %s", req.state)
    await broadcast_update({"type": "bot_state", "data": {"state": req.state}})
    return {"state": req.state}
