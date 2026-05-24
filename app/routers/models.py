from fastapi import APIRouter

from app.services.s3 import generate_presigned_url

router = APIRouter(tags=["models"])


@router.get("/models/escaneo")
def get_escaneo_url():
    return {"url": generate_presigned_url("escaneo.ply")}


@router.get("/models/protesis")
def get_protesis_url():
    return {"url": generate_presigned_url("protesis.ply")}
