import json
import logging

from livekit import api

from app.config import get_livekit_config
from app.constants import AGENT_NAME

logger = logging.getLogger(__name__)


def _livekit_http_url(livekit_url: str) -> str:
    return livekit_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)


async def create_livekit_token(room: str, identity: str) -> dict[str, str]:
    config = get_livekit_config()
    if config is None:
        raise RuntimeError("LiveKit credentials are not configured")

    http_url = _livekit_http_url(config["livekit_url"])
    lkapi = api.LiveKitAPI(
        url=http_url,
        api_key=config["livekit_api_key"],
        api_secret=config["livekit_api_secret"],
    )

    try:
        await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room,
                metadata=json.dumps({"sessionId": room}),
            )
        )
    except Exception as exc:
        logger.warning("[LiveKit] Agent dispatch failed (may already exist): %s", exc)

    token = (
        api.AccessToken(config["livekit_api_key"], config["livekit_api_secret"])
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )

    await lkapi.aclose()

    return {
        "token": token,
        "url": config["livekit_url"],
    }
