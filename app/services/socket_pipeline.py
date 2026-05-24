"""Orquestación: escaneo → analyze → agente → CadQuery."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from app.core.mesh_errors import EmptyMeshError, MeshError, MeshFileNotFoundError, MeshReadError
from app.core.paths import DATOS_REPORTE_PATH, SOCKET_OUTPUT_ROOT, TEMP_UPLOAD_DIR
from app.services.gemini_socket_agent import run_gemini_socket_agent
from app.services.llm_client import get_model_name
from app.services.mesh_analyzer import analyze_mesh
from app.services.mesh_cleaner import clean_mesh
from app.services.socket_cad_service import generate_socket_from_agent
from app.services.socket_design_agent import load_default_clinical_report, run_socket_design_agent

AgentEngine = Literal["gemini", "rules"]

logger = logging.getLogger(__name__)


def _job_prefix(job_id: str) -> str:
    return f"[job={job_id}]"


def _elapsed(start: float) -> str:
    return f"{time.perf_counter() - start:.1f}s"


def _valid_section_count(geometry: dict[str, Any]) -> int:
    return sum(
        1
        for s in geometry.get("sections") or []
        if int(s.get("contour_point_count", 0) or 0) >= 3
    )


def _run_agent(
    geometry: dict[str, Any],
    clinical_report: dict[str, Any],
    *,
    engine: AgentEngine,
    gemini_model: str | None,
    fallback_to_rules: bool,
    job_id: str,
) -> dict[str, Any]:
    if engine == "gemini":
        return run_gemini_socket_agent(
            geometry,
            clinical_report,
            model=gemini_model,
            fallback_to_rules=fallback_to_rules,
            job_id=job_id,
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
    gemini_model: str | None = None,
    fallback_to_rules: bool = True,
    generate_stl: bool = True,
    output_root: Path | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """
    Ejecuta el flujo completo sobre un escaneo ya guardado en disco.

    Usa app/data/datos_reporte.json para datos clínicos.
    """
    if not scan_path.is_file():
        raise FileNotFoundError(f"Escaneo no encontrado: {scan_path}")

    pipeline_start = time.perf_counter()
    job_id = job_id or str(uuid.uuid4())
    prefix = _job_prefix(job_id)
    case_id = scan_path.stem
    out_root = output_root or SOCKET_OUTPUT_ROOT
    job_dir = out_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "%s 1/6 upload saved path=%s bytes=%d ext=%s",
        prefix,
        scan_path,
        scan_path.stat().st_size,
        scan_path.suffix.lower(),
    )

    step_start = time.perf_counter()
    clean_mesh(str(scan_path))
    logger.info("%s 2/6 clean_mesh done (%s)", prefix, _elapsed(step_start))

    step_start = time.perf_counter()
    geometry = analyze_mesh(str(scan_path))
    qg_geom = geometry.get("quality_gate") or {}
    recon = geometry.get("reconstruction_error") or {}
    logger.info(
        "%s 3/6 analyze_mesh done (%s) height_mm=%.1f sections=%d mean_error_mm=%.2f passed=%s demo_eligible=%s",
        prefix,
        _elapsed(step_start),
        float(geometry.get("height_mm", 0)),
        _valid_section_count(geometry),
        float(recon.get("mean_error_mm", 0)),
        bool(qg_geom.get("passed", False)),
        bool(qg_geom.get("demo_eligible", False)),
    )

    clinical = load_default_clinical_report()

    model_name = get_model_name(gemini_model) if engine == "gemini" else None
    logger.info(
        "%s 4/6 agent start engine=%s model=%s fallback_to_rules=%s",
        prefix,
        engine,
        model_name or "n/a",
        fallback_to_rules,
    )

    step_start = time.perf_counter()
    agent_payload = _run_agent(
        geometry,
        clinical,
        engine=engine,
        gemini_model=gemini_model,
        fallback_to_rules=fallback_to_rules,
        job_id=job_id,
    )

    socket_design = agent_payload.get("socket_design")
    design_params = agent_payload.get("design_parameters") or {}
    local_mods = len((socket_design or {}).get("local_modifications") or [])
    fit_confidence = (socket_design or {}).get("fit_confidence")
    fit_confidence_str = f"{fit_confidence:.3f}" if fit_confidence is not None else "n/a"
    blocked_note = " socket_design=null" if socket_design is None else ""
    logger.info(
        "%s 4/6 agent done (%s) agent_engine=%s local_mods=%d fit_confidence=%s%s",
        prefix,
        _elapsed(step_start),
        design_params.get("agent_engine", engine),
        local_mods,
        fit_confidence_str,
        blocked_note,
    )

    _write_json(job_dir / "geometry_analysis.json", geometry)
    _write_json(job_dir / "agent_response.json", agent_payload)
    logger.info(
        "%s 5/6 artifacts written job_dir=%s geometry=%s agent=%s",
        prefix,
        job_dir,
        job_dir / "geometry_analysis.json",
        job_dir / "agent_response.json",
    )

    qg = agent_payload.get("quality_gate") or geometry.get("quality_gate") or {}
    status = "blocked" if socket_design is None else str(
        agent_payload.get("cadquery_handoff", {}).get("design_mode", "demo")
    )

    cad_report: dict[str, Any] | None = None
    socket_stl: str | None = None
    socket_step: str | None = None

    if generate_stl and socket_design is not None:
        step_start = time.perf_counter()
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
        logger.info(
            "%s 6/6 cad done (%s) status=%s stl=%s step=%s",
            prefix,
            _elapsed(step_start),
            status,
            exports.get("stl") or "none",
            exports.get("step") or "none",
        )
    elif not generate_stl:
        logger.info("%s 6/6 cad skipped (generate_stl=False)", prefix)
    else:
        logger.info("%s 6/6 cad skipped (socket_design=null, status=blocked)", prefix)

    base = f"/escaneo/{job_id}"
    download_urls = {
        "stl": f"{base}/stl" if socket_stl else None,
        "step": f"{base}/step" if socket_step else None,
        "report": f"{base}/report" if cad_report else None,
        "geometry": f"{base}/geometry",
        "agent": f"{base}/agent",
    }

    logger.info(
        "%s pipeline complete status=%s total=%s",
        prefix,
        status,
        _elapsed(pipeline_start),
    )

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
