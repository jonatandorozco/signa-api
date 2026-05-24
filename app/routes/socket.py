"""Un solo endpoint: sube PLY/STL/OBJ → socket 3D."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.paths import SOCKET_OUTPUT_ROOT
from app.schemas.socket_design import SocketRunResponse
from app.services.socket_pipeline import (
    mesh_error_to_http_detail,
    run_socket_pipeline,
    save_uploaded_scan,
)

router = APIRouter(tags=["socket"])


@router.post("/socket", response_model=SocketRunResponse)
async def create_socket_from_scan(
    file: UploadFile = File(..., description="Escaneo del muñón (.ply, .stl, .obj)"),
    engine: str = Form("rules", description="rules | openai"),
    generate_stl: bool = Form(True),
    openai_model: str | None = Form(None),
    fallback_to_rules: bool = Form(True),
) -> SocketRunResponse:
    """
    Pipeline automático:

    1. Guarda el escaneo
    2. Analyze (contornos + quality gate)
    3. Socket-design (reglas o OpenAI + datos_reporte.json)
    4. CadQuery → socket.stl en output/socket_generate/{job_id}/
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="El archivo debe tener nombre")

    if engine not in ("rules", "openai"):
        raise HTTPException(status_code=400, detail="engine debe ser 'rules' o 'openai'")

    try:
        content = await file.read()
        scan_path = save_uploaded_scan(content, file.filename)
        result = run_socket_pipeline(
            scan_path,
            engine=engine,  # type: ignore[arg-type]
            openai_model=openai_model,
            fallback_to_rules=fallback_to_rules,
            generate_stl=generate_stl,
        )
    except Exception as exc:
        status, detail = mesh_error_to_http_detail(exc)
        raise HTTPException(status_code=status, detail=detail) from exc

    return SocketRunResponse(
        job_id=result["job_id"],
        case_id=result["case_id"],
        scan_file_path=result["scan_file_path"],
        status=result["status"],
        quality_gate=result["quality_gate"],
        download_urls=result["download_urls"],
        artifacts=result["artifacts"],
        cad_report=result.get("cad_report"),
    )


def _job_dir(job_id: str) -> Path:
    out_dir = SOCKET_OUTPUT_ROOT / job_id
    if not out_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return out_dir


@router.get("/socket/{job_id}/stl")
def download_stl(job_id: str):
    path = _job_dir(job_id) / "socket.stl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="STL no generado (status blocked?)")
    return FileResponse(path, media_type="model/stl", filename="socket.stl")


@router.get("/socket/{job_id}/step")
def download_step(job_id: str):
    path = _job_dir(job_id) / "socket.step"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="STEP no disponible")
    return FileResponse(path, media_type="application/step", filename="socket.step")


@router.get("/socket/{job_id}/report")
def download_report(job_id: str):
    path = _job_dir(job_id) / "agent_cad_report.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reporte CAD no encontrado")
    return FileResponse(path, media_type="application/json", filename="agent_cad_report.json")


@router.get("/socket/{job_id}/geometry")
def download_geometry(job_id: str):
    path = _job_dir(job_id) / "geometry_analysis.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="geometry_analysis.json no encontrado")
    return FileResponse(path, media_type="application/json", filename="geometry_analysis.json")


@router.get("/socket/{job_id}/agent")
def download_agent(job_id: str):
    path = _job_dir(job_id) / "agent_response.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="agent_response.json no encontrado")
    return FileResponse(path, media_type="application/json", filename="agent_response.json")
