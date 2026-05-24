"""Utilidades de conversión de mallas (fuera del pipeline /socket)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.services.mesh_convert import convert_stl_bytes_to_ply

router = APIRouter(tags=["convert"])


@router.post(
    "/convert/stl-to-ply",
    summary="Convierte STL → PLY",
    response_class=Response,
)
async def convert_stl_to_ply(
    file: UploadFile = File(..., description="Archivo .stl en mm"),
) -> Response:
    """
    Convierte un STL a PLY sin pasar por analyze ni el agente OpenAI.
    Devuelve el PLY como descarga directa.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="El archivo debe tener nombre")
    suffix = Path(file.filename).suffix.lower()
    if suffix != ".stl":
        raise HTTPException(status_code=400, detail="Solo se acepta extensión .stl")

    try:
        content = await file.read()
        ply_bytes, meta = convert_stl_bytes_to_ply(content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al convertir STL: {exc}") from exc

    stem = Path(file.filename).stem or "mesh"
    headers = {
        "Content-Disposition": f'attachment; filename="{stem}.ply"',
        "X-Mesh-Vertices": str(meta.get("vertices", 0)),
        "X-Mesh-Faces": str(meta.get("faces", 0)),
    }
    return Response(content=ply_bytes, media_type="application/octet-stream", headers=headers)
