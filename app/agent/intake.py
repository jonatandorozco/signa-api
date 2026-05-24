import logging

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins.google import realtime as google_realtime

from app.config import get_gemini_api_key
from app.constants import AGENT_NAME
from app.intake_instructions import INTAKE_AGENT_INSTRUCTIONS, INTAKE_GREETING_INSTRUCTIONS

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"


async def entrypoint(ctx: JobContext) -> None:
    room_name = ctx.room.name if ctx.room else "(unknown)"
    logger.info("[Agent] Job entry — room: %s", room_name)

    try:
        await ctx.connect()
        room_name = ctx.room.name
        logger.info(
            "[Agent] Connected. Remote participants: %s",
            len(ctx.room.remote_participants),
        )
    except Exception as exc:
        logger.exception("[Agent] Connection error")
        return

    gemini_api_key = get_gemini_api_key()
    if not gemini_api_key:
        logger.error("[Agent] No Gemini API key. Set GEMINI_API_KEY in .env")
        return

    llm = google_realtime.RealtimeModel(
        api_key=gemini_api_key,
        model=GEMINI_MODEL,
        voice="Kore",
        language="es-US",
        temperature=0.8,
    )

    agent = Agent(instructions=INTAKE_AGENT_INSTRUCTIONS)
    session = AgentSession(llm=llm)

    logger.info("[Agent] Starting voice session...")
    try:
        await session.start(agent=agent, room=ctx.room)
        logger.info("[Agent] Session active")
    except Exception:
        logger.exception("[Agent] Failed to start voice session")
        return

    try:
        session.generate_reply(instructions=INTAKE_GREETING_INSTRUCTIONS)
        logger.info("[Agent] Greeting sent")
    except Exception:
        logger.exception(
            "[Agent] Failed to generate greeting (check GEMINI_API_KEY and model access)"
        )


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
        )
    )
