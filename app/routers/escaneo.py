"""Escaneo: sube escaneo a R2 y ejecuta pipeline scan → socket 3D."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core import paths
from app.schemas.socket_design import EscaneoRunResponse
from app.services.s3 import upload_bytes_to_r2, validate_scan_extension
from app.services.socket_pipeline import (
    mesh_error_to_http_detail,
    run_socket_pipeline,
    save_uploaded_scan,
)

router = APIRouter(tags=["escaneo"])
logger = logging.getLogger(__name__)

ESCANEO_PREFIX = "escaneos"


@router.post("/escaneo", response_model=EscaneoRunResponse)
async def subir_escaneo(
    archivo: UploadFile = File(..., description="Escaneo del muñón (.ply, .stl, .obj)"),
    engine: str = Form("rules", description="rules | gemini"),
    generate_stl: bool = Form(True),
    gemini_model: str | None = Form(None),
    fallback_to_rules: bool = Form(True),
) -> EscaneoRunResponse:
    """
    Pipeline automático:

    1. Sube el escaneo a R2
    2. Guarda copia local para procesamiento
    3. Analyze (contornos + quality gate)
    4. Socket-design (reglas o Gemini + datos_reporte.json)
    5. CadQuery → socket.stl en output/socket_generate/{job_id}/
    """
    if not archivo.filename:
        raise HTTPException(status_code=400, detail="El archivo debe tener nombre")

    try:
        extension = validate_scan_extension(archivo.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if engine not in ("rules", "gemini"):
        raise HTTPException(status_code=400, detail="engine debe ser 'rules' o 'gemini'")

    request_start = time.perf_counter()
    logger.info(
        "POST /escaneo start filename=%s engine=%s generate_stl=%s gemini_model=%s fallback_to_rules=%s",
        archivo.filename,
        engine,
        generate_stl,
        gemini_model or "default",
        fallback_to_rules,
    )

    content = await archivo.read()
    object_key = f"{ESCANEO_PREFIX}/{uuid.uuid4().hex}{extension}"

    try:
        upload_bytes_to_r2(content, object_key)
    except KeyError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Variable de entorno requerida no configurada: {exc.args[0]}",
        )
    except (ClientError, BotoCoreError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo subir el escaneo a R2: {exc}",
        )

    try:
        logger.debug("POST /escaneo read upload bytes=%d", len(content))
        scan_path = save_uploaded_scan(content, archivo.filename)
        result = run_socket_pipeline(
            scan_path,
            engine=engine,  # type: ignore[arg-type]
            gemini_model=gemini_model,
            fallback_to_rules=fallback_to_rules,
            generate_stl=generate_stl,
        )
    except Exception as exc:
        logger.error(
            "POST /escaneo failed after %.1fs type=%s message=%s object_key=%s",
            time.perf_counter() - request_start,
            type(exc).__name__,
            exc,
            object_key,
        )
        logger.debug("POST /escaneo failure details", exc_info=exc)
        status, detail = mesh_error_to_http_detail(exc)
        raise HTTPException(
            status_code=status,
            detail={"message": detail, "object_key": object_key},
        ) from exc

    logger.info(
        "POST /escaneo done job_id=%s status=%s object_key=%s elapsed=%.1fs",
        result["job_id"],
        result["status"],
        object_key,
        time.perf_counter() - request_start,
    )

    return EscaneoRunResponse(
        job_id=result["job_id"],
        case_id=result["case_id"],
        scan_file_path=result["scan_file_path"],
        status=result["status"],
        quality_gate=result["quality_gate"],
        download_urls=result["download_urls"],
        artifacts=result["artifacts"],
        cad_report=result.get("cad_report"),
        object_key=object_key,
        filename=archivo.filename,
        format=extension.lstrip("."),
    )


def _job_dir(job_id: str) -> Path:
    out_dir = paths.SOCKET_OUTPUT_ROOT / job_id
    if not out_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return out_dir


@router.get("/escaneo/{job_id}/stl")
def download_stl(job_id: str):
    path = _job_dir(job_id) / "socket.stl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="STL no generado (status blocked?)")
    return FileResponse(path, media_type="model/stl", filename="socket.stl")


@router.get("/escaneo/{job_id}/step")
def download_step(job_id: str):
    path = _job_dir(job_id) / "socket.step"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="STEP no disponible")
    return FileResponse(path, media_type="application/step", filename="socket.step")


@router.get("/escaneo/{job_id}/report")
def download_report(job_id: str):
    path = _job_dir(job_id) / "agent_cad_report.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reporte CAD no encontrado")
    return FileResponse(path, media_type="application/json", filename="agent_cad_report.json")


@router.get("/escaneo/{job_id}/geometry")
def download_geometry(job_id: str):
    path = _job_dir(job_id) / "geometry_analysis.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="geometry_analysis.json no encontrado")
    return FileResponse(path, media_type="application/json", filename="geometry_analysis.json")


@router.get("/escaneo/{job_id}/agent")
def download_agent(job_id: str):
    path = _job_dir(job_id) / "agent_response.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="agent_response.json no encontrado")
    return FileResponse(path, media_type="application/json", filename="agent_response.json")
