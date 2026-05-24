"""Orquestación: escaneo → analyze → agente → CadQuery."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

from app.core.mesh_errors import EmptyMeshError, MeshError, MeshFileNotFoundError, MeshReadError
from app.core.paths import DATOS_REPORTE_PATH, SOCKET_OUTPUT_ROOT, TEMP_UPLOAD_DIR
from app.services.mesh_analyzer import analyze_mesh
from app.services.mesh_cleaner import clean_mesh
from app.services.openai_socket_agent import run_openai_socket_agent
from app.services.socket_cad_service import generate_socket_from_agent
from app.services.socket_design_agent import load_default_clinical_report, run_socket_design_agent

AgentEngine = Literal["openai", "rules"]


def _run_agent(
    geometry: dict[str, Any],
    clinical_report: dict[str, Any],
    *,
    engine: AgentEngine,
    openai_model: str | None,
    fallback_to_rules: bool,
) -> dict[str, Any]:
    if engine == "openai":
        return run_openai_socket_agent(
            geometry,
            clinical_report,
            model=openai_model,
            fallback_to_rules=fallback_to_rules,
        )
    return run_socket_design_agent(geometry, clinical_report)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def run_socket_pipeline(
    scan_path: Path,
    *,
    engine: AgentEngine = "rules",
    openai_model: str | None = None,
    fallback_to_rules: bool = True,
    generate_stl: bool = True,
    output_root: Path | None = None,
) -> dict[str, Any]:
    """
    Ejecuta el flujo completo sobre un escaneo ya guardado en disco.

    Usa app/data/datos_reporte.json para datos clínicos.
    """
    if not scan_path.is_file():
        raise FileNotFoundError(f"Escaneo no encontrado: {scan_path}")

    job_id = str(uuid.uuid4())
    case_id = scan_path.stem
    out_root = output_root or SOCKET_OUTPUT_ROOT
    job_dir = out_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    clean_mesh(str(scan_path))
    geometry = analyze_mesh(str(scan_path))
    clinical = load_default_clinical_report()

    agent_payload = _run_agent(
        geometry,
        clinical,
        engine=engine,
        openai_model=openai_model,
        fallback_to_rules=fallback_to_rules,
    )

    _write_json(job_dir / "geometry_analysis.json", geometry)
    _write_json(job_dir / "agent_response.json", agent_payload)

    socket_design = agent_payload.get("socket_design")
    qg = agent_payload.get("quality_gate") or geometry.get("quality_gate") or {}
    status = "blocked" if socket_design is None else str(
        agent_payload.get("cadquery_handoff", {}).get("design_mode", "demo")
    )

    cad_report: dict[str, Any] | None = None
    socket_stl: str | None = None
    socket_step: str | None = None

    if generate_stl and socket_design is not None:
        try:
            cad_report = generate_socket_from_agent(geometry, agent_payload, job_dir)
        except ImportError as exc:
            raise ValueError(f"CadQuery no instalado: {exc}. pip install cadquery") from exc
        status = str(cad_report.get("status", status))
        exports = cad_report.get("exports") or {}
        if exports.get("stl"):
            socket_stl = str(job_dir / exports["stl"])
        if exports.get("step"):
            socket_step = str(job_dir / exports["step"])

    base = f"/socket/{job_id}"
    download_urls = {
        "stl": f"{base}/stl" if socket_stl else None,
        "step": f"{base}/step" if socket_step else None,
        "report": f"{base}/report" if cad_report else None,
        "geometry": f"{base}/geometry",
        "agent": f"{base}/agent",
    }

    return {
        "job_id": job_id,
        "case_id": case_id,
        "scan_file_path": str(scan_path.resolve()),
        "status": status,
        "quality_gate": qg,
        "agent_response": agent_payload,
        "cad_report": cad_report,
        "artifacts": {
            "job_dir": str(job_dir.resolve()),
            "geometry_analysis": str((job_dir / "geometry_analysis.json").resolve()),
            "agent_response": str((job_dir / "agent_response.json").resolve()),
            "clinical_report": str(DATOS_REPORTE_PATH.resolve()),
        },
        "download_urls": download_urls,
    }


def save_uploaded_scan(content: bytes, filename: str) -> Path:
    """Guarda escaneo en temp_upload y devuelve la ruta."""
    suffix = Path(filename).suffix.lower()
    if suffix not in {".ply", ".stl", ".obj"}:
        raise ValueError(f"Extensión no permitida: {suffix}")
    if not content:
        raise ValueError("El archivo está vacío")

    case_id = str(uuid.uuid4())
    TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = TEMP_UPLOAD_DIR / f"{case_id}{suffix}"
    destination.write_bytes(content)
    return destination


def mesh_error_to_http_detail(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, MeshFileNotFoundError):
        return 404, str(exc)
    if isinstance(exc, EmptyMeshError):
        return 422, str(exc)
    if isinstance(exc, MeshReadError):
        return 400, str(exc)
    if isinstance(exc, MeshError):
        return 500, str(exc)
    if isinstance(exc, ValueError):
        return 422, str(exc)
    if isinstance(exc, FileNotFoundError):
        return 404, str(exc)
    return 500, str(exc)
