from functools import lru_cache
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

MODEL_URL_FIELDS = ("modelo miembro", "modelo protesis")


@lru_cache
def _s3_client():
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings["aws_s3_endpoint_url"],
        aws_access_key_id=settings["aws_access_key_id"],
        aws_secret_access_key=settings["aws_secret_access_key"],
        region_name=settings["aws_region"],
    )


def _to_object_key(value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return urlparse(value).path.lstrip("/")
    return value.lstrip("/")


def generate_presigned_url(object_key: str) -> str:
    settings = get_settings()
    client = _s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings["aws_s3_bucket"], "Key": object_key},
        ExpiresIn=settings["presigned_url_expiration"],
    )


def apply_presigned_model_urls(data: dict) -> None:
    for field in MODEL_URL_FIELDS:
        if field not in data:
            continue
        object_key = _to_object_key(data[field])
        data[field] = generate_presigned_url(object_key)
