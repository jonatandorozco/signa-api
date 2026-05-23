from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

MODEL_URL_FIELDS = ("modelo miembro", "modelo protesis")
ALLOWED_SCAN_EXTENSIONS = {".stl", ".ply"}
CONTENT_TYPE_BY_EXTENSION = {
    ".stl": "model/stl",
    ".ply": "application/octet-stream",
}


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


def validate_scan_extension(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SCAN_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_SCAN_EXTENSIONS))
        raise ValueError(f"Formato no permitido. Solo se aceptan: {allowed}")
    return suffix


def generate_presigned_url(object_key: str) -> str:
    settings = get_settings()
    client = _s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings["aws_s3_bucket"], "Key": object_key},
        ExpiresIn=settings["presigned_url_expiration"],
    )


def upload_file_to_r2(
    file_path: str | Path,
    object_key: str,
    *,
    content_type: str | None = None,
) -> str:
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Archivo no encontrado: {path}")

    key = object_key.lstrip("/")
    resolved_content_type = content_type or CONTENT_TYPE_BY_EXTENSION.get(
        path.suffix.lower(), "application/octet-stream"
    )

    settings = get_settings()
    client = _s3_client()
    client.upload_file(
        str(path),
        settings["aws_s3_bucket"],
        key,
        ExtraArgs={"ContentType": resolved_content_type},
    )
    return key


def upload_fileobj_to_r2(
    file_obj,
    object_key: str,
    *,
    content_type: str | None = None,
) -> str:
    key = object_key.lstrip("/")
    suffix = Path(key).suffix.lower()
    resolved_content_type = content_type or CONTENT_TYPE_BY_EXTENSION.get(
        suffix, "application/octet-stream"
    )

    settings = get_settings()
    client = _s3_client()
    client.upload_fileobj(
        file_obj,
        settings["aws_s3_bucket"],
        key,
        ExtraArgs={"ContentType": resolved_content_type},
    )
    return key


def apply_presigned_model_urls(data: dict) -> None:
    for field in MODEL_URL_FIELDS:
        if field not in data:
            continue
        object_key = _to_object_key(data[field])
        data[field] = generate_presigned_url(object_key)
