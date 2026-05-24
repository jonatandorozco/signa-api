import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@lru_cache
def get_settings() -> dict:
    return {
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.getenv("AWS_REGION", "auto"),
        "aws_s3_endpoint_url": os.environ["AWS_S3_ENDPOINT_URL"],
        "aws_s3_bucket": os.environ["AWS_S3_BUCKET"],
        "presigned_url_expiration": int(os.getenv("AWS_S3_PRESIGNED_URL_EXPIRATION", "3600")),
    }


@lru_cache
def get_livekit_config() -> dict | None:
    livekit_url = os.getenv("LIVEKIT_URL")
    livekit_api_key = os.getenv("LIVEKIT_API_KEY")
    livekit_api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not livekit_url or not livekit_api_key or not livekit_api_secret:
        return None

    return {
        "livekit_url": livekit_url,
        "livekit_api_key": livekit_api_key,
        "livekit_api_secret": livekit_api_secret,
    }


@lru_cache
def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def get_gemini_api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY")
