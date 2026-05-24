import logging
import uuid

from fastapi import APIRouter, HTTPException, Query

from app.config import get_livekit_config
from app.services.livekit import create_livekit_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["livekit"])


@router.get("/livekit/token")
async def get_livekit_token(
    room: str = Query(...),
    identity: str = Query(...),
):
    if get_livekit_config() is None:
        raise HTTPException(status_code=500, detail="LiveKit credentials are not configured")

    try:
        return await create_livekit_token(room=room, identity=identity)
    except Exception as exc:
        logger.exception("[API] Token error")
        raise HTTPException(status_code=500, detail="Failed to create LiveKit token") from exc


@router.get("/health")
def health():
    return {
        "web": "ok",
        "agentHint": "Run: uv run python -m app.agent.intake dev",
    }


@router.post("/sessions")
def create_session():
    return {"sessionId": str(uuid.uuid4())}
