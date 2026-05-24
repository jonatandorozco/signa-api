"""Fusiona geometry_analysis en la respuesta del agente para CadQuery autocontenido."""

from __future__ import annotations

from typing import Any

from app.schemas.socket_design import SocketDesignAgentResponse
from app.services.clinical_preferences import apply_clinical_preferences_to_agent


def attach_cad_geometry(payload: dict[str, Any], geometry: dict[str, Any]) -> dict[str, Any]:
    """Inyecta contornos y métricas en cad_geometry (el LLM no debe emitir contour)."""
    merged = dict(payload)
    merged["cad_geometry"] = {
        "height_mm": geometry.get("height_mm"),
        "volume_cm3": geometry.get("volume_cm3"),
        "surface_irregularity": geometry.get("surface_irregularity"),
        "taper_ratio": geometry.get("taper_ratio"),
        "section_similarity": geometry.get("section_similarity"),
        "reconstruction_error": geometry.get("reconstruction_error"),
        "quality_gate": geometry.get("quality_gate"),
        "shape_profile": geometry.get("shape_profile"),
        "sections": geometry.get("sections"),
    }
    return merged


def resolve_geometry_from_agent(
    agent: dict[str, Any],
    geometry_fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Obtiene geometry_analysis desde cad_geometry embebido o fallback explícito."""
    cad = agent.get("cad_geometry")
    if isinstance(cad, dict) and cad.get("sections"):
        return cad
    if geometry_fallback and geometry_fallback.get("sections"):
        return geometry_fallback
    raise ValueError(
        "Se requiere cad_geometry.sections en agent_response o geometry_analysis explícito"
    )


def finalize_agent_payload(
    payload: dict[str, Any],
    geometry: dict[str, Any],
    clinical_report: dict[str, Any],
) -> dict[str, Any]:
    """Preferencias clínicas post-LLM + cad_geometry + validación Pydantic."""
    height_mm = float(geometry.get("height_mm", 0))
    payload = apply_clinical_preferences_to_agent(payload, clinical_report, height_mm)
    payload = attach_cad_geometry(payload, geometry)
    qg = payload.get("quality_gate") or {}
    handoff = payload.setdefault("cadquery_handoff", {})
    handoff["design_mode"] = "production" if qg.get("passed") else "demo"
    return SocketDesignAgentResponse(**payload).model_dump()
