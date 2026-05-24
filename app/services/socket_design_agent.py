"""
Agente de diseño de socket transtibial (reglas clínicas + geometry_analysis).

Produce JSON ejecutable por socket_design.cad.py o un pipeline CadQuery externo.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from app.schemas.geometry import GeometryResponse
from app.schemas.socket_design import SocketDesignAgentResponse
from app.services.clinical_preferences import (
    TRANSTIBIAL_DEFAULT_LENGTH_FRACTION,
    cap_transtibial_length_fraction,
    is_transtibial_report,
)
from app.services.mesh_analyzer import resolve_socket_trim_from_geometry
from app.services.socket_design_merge import finalize_agent_payload

DATOS_REPORTE_PATH = Path(__file__).resolve().parent.parent / "data" / "datos_reporte.json"

_MEAN_ERROR_HARD_LIMIT = 2.0
_MEAN_ERROR_TARGET = 1.0
_OFFSET_SAMPLE_STEP_MM = 5.0


def load_default_clinical_report() -> dict[str, Any]:
    with DATOS_REPORTE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _geometry_dict(geometry: GeometryResponse | dict[str, Any]) -> dict[str, Any]:
    if isinstance(geometry, GeometryResponse):
        return geometry.model_dump()
    return geometry


def _report_get(report: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = report
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key, default)
    return node


def _evaluate_agent_quality(geometry: dict[str, Any]) -> dict[str, Any]:
    qg = geometry.get("quality_gate") or {}
    recon = geometry.get("reconstruction_error") or {}
    mean_err = float(recon.get("mean_error_mm", 99.0))
    blocked = mean_err > _MEAN_ERROR_HARD_LIMIT

    messages = list(qg.get("messages", []))
    if blocked:
        messages.insert(
            0,
            f"mean_error_mm {mean_err:.2f} > {_MEAN_ERROR_HARD_LIMIT}: no emitir socket_design final",
        )

    passed = bool(qg.get("passed", False)) and not blocked
    demo_eligible = bool(qg.get("demo_eligible", False)) and not blocked

    return {
        "passed": passed,
        "demo_eligible": demo_eligible,
        "blocked": blocked,
        "mean_error_mm": mean_err,
        "max_error_mm": float(recon.get("max_error_mm", 0.0)),
        "section_similarity": float(geometry.get("section_similarity", 0.0)),
        "volume_cm3": geometry.get("volume_cm3"),
        "volume_estimated": bool(qg.get("volume_estimated", False)),
        "messages": messages,
    }


def _derive_clearance_mm(geometry: dict[str, Any], report: dict[str, Any], quality: dict[str, Any]) -> float:
    recon = geometry.get("reconstruction_error") or {}
    mean_err = float(recon.get("mean_error_mm", 0.0))
    residual = report.get("residual_limb_status") or {}

    if mean_err <= _MEAN_ERROR_TARGET:
        base = 2.0
    elif mean_err <= _MEAN_ERROR_HARD_LIMIT:
        base = 2.5
    else:
        base = 2.5

    sensitivity = str(residual.get("sensitivity_areas", "")).lower()
    if mean_err > _MEAN_ERROR_TARGET:
        base += 0.5
    if residual.get("volume_changes_reported"):
        base += 0.25

    if not quality["passed"] and quality["demo_eligible"]:
        base = max(base, 2.5)

    return float(np.clip(base, 1.0, 3.5))


def _build_offset_samples(
    sections: list[dict[str, Any]],
    height_mm: float,
    base_offset_mm: float,
    volume_changes: bool,
) -> list[dict[str, float]]:
    valid = sorted(
        [s for s in sections if int(s.get("contour_point_count", 0)) >= 3],
        key=lambda s: float(s["z_mm"]),
    )
    if not valid:
        return [{"z_mm": 0.0, "offset_mm": round(base_offset_mm, 2)}]

    z_vals = [float(s["z_mm"]) for s in valid]
    z_max = max(z_vals) if z_vals else height_mm
    samples: list[dict[str, float]] = []

    z = 0.0
    while z <= height_mm + 1e-6:
        offset = base_offset_mm
        if volume_changes and z >= 0.65 * z_max:
            offset += 0.5
        samples.append({"z_mm": round(z, 3), "offset_mm": round(offset, 2)})
        z += _OFFSET_SAMPLE_STEP_MM

    for z_mm in z_vals:
        if not any(abs(s["z_mm"] - z_mm) < 0.5 for s in samples):
            offset = base_offset_mm + (0.5 if volume_changes and z_mm >= 0.65 * z_max else 0.0)
            samples.append({"z_mm": round(z_mm, 3), "offset_mm": round(offset, 2)})

    samples.sort(key=lambda s: s["z_mm"])
    return samples


def _build_local_modifications(
    socket_top_z_mm: float,
    report: dict[str, Any],
    clearance_mm: float,
) -> list[dict[str, Any]]:
    mods: list[dict[str, Any]] = []
    residual = report.get("residual_limb_status") or {}
    sensitivity = str(residual.get("sensitivity_areas", "")).lower()
    pain_score = float(residual.get("pain_score_0_10", 0) or 0)

    if "distal" in sensitivity or pain_score >= 3:
        mods.append(
            {
                "kind": "relief",
                "z_min_mm": 0.0,
                "z_max_mm": round(socket_top_z_mm * 0.3, 3),
                "angle_start_deg": 0.0,
                "angle_end_deg": 360.0,
                "depth_mm": round(max(1.0, clearance_mm * 0.5), 2),
                "clinical_reason": "zona distal sensible / dolor reportado",
            }
        )

    environment = _report_get(report, "functional_goals", "environment", default=[]) or []
    env_text = " ".join(str(e).lower() for e in environment)
    if "caluroso" in env_text or "sudor" in str(residual.get("skin_issues", [])).lower():
        mods.append(
            {
                "kind": "ventilation_channel",
                "z_min_mm": round(socket_top_z_mm * 0.2, 3),
                "z_max_mm": round(socket_top_z_mm * 0.82, 3),
                "angle_start_deg": 80.0,
                "angle_end_deg": 100.0,
                "depth_mm": 2.0,
                "clinical_reason": "clima caluroso / sudoración — canal lateral (revisión clínica)",
            }
        )

    mods.extend(_default_transtibial_modifications(socket_top_z_mm))
    return mods


def _default_transtibial_modifications(socket_top_z_mm: float) -> list[dict[str, Any]]:
    """Modificaciones PTB base si el agente no las define explícitamente (z relativo al tope del socket)."""
    top = max(socket_top_z_mm, 1.0)
    return [
        {
            "kind": "build_up",
            "z_min_mm": round(top * 0.35, 3),
            "z_max_mm": round(top * 0.75, 3),
            "angle_start_deg": 330.0,
            "angle_end_deg": 30.0,
            "depth_mm": 1.2,
            "clinical_reason": "barra rotuliana PTB — contacto anterior controlado",
        },
        {
            "kind": "relief",
            "z_min_mm": round(top * 0.28, 3),
            "z_max_mm": round(top * 0.88, 3),
            "angle_start_deg": 150.0,
            "angle_end_deg": 210.0,
            "depth_mm": 0.9,
            "clinical_reason": "alivio posterior para flexión de rodilla (no subir a zona de rodilla)",
        },
        {
            "kind": "relief",
            "z_min_mm": round(top * 0.50, 3),
            "z_max_mm": round(top * 0.85, 3),
            "angle_start_deg": 240.0,
            "angle_end_deg": 300.0,
            "depth_mm": 0.5,
            "clinical_reason": "holgura lateral proximal en borde superior del socket",
        },
    ]


def _compute_fit_confidence(geometry: dict[str, Any], report: dict[str, Any]) -> float:
    recon = geometry.get("reconstruction_error") or {}
    mean_err = float(recon.get("mean_error_mm", 99.0))
    section_sim = float(geometry.get("section_similarity", 0.0))
    surface_irr = float(geometry.get("surface_irregularity", 0.0))

    confidence = 0.9
    if mean_err <= _MEAN_ERROR_TARGET and section_sim >= 0.9:
        confidence = 0.9
    elif mean_err <= _MEAN_ERROR_HARD_LIMIT:
        confidence = 0.8
    else:
        confidence = 0.5

    if _MEAN_ERROR_TARGET < mean_err <= _MEAN_ERROR_HARD_LIMIT:
        confidence -= 0.1
    if surface_irr > 0.15:
        confidence -= 0.15

    info_conf = str(_report_get(report, "professional_flags", "information_confidence", default="")).lower()
    if info_conf in {"media", "baja", "low", "medium"}:
        confidence -= 0.1

    residual = report.get("residual_limb_status") or {}
    flags = report.get("professional_flags") or {}
    if residual.get("open_wound_reported") or flags.get("requires_skin_review"):
        confidence = min(confidence, 0.5)

    return round(float(np.clip(confidence, 0.0, 1.0)), 3)


def _build_clinical_reasoning(
    report: dict[str, Any],
    clearance_mm: float,
    length_fraction: float,
    quality: dict[str, Any],
) -> dict[str, Any]:
    residual = report.get("residual_limb_status") or {}
    goals = report.get("functional_goals") or {}
    flags = report.get("professional_flags") or {}

    pain_bits = []
    if residual.get("pain_present"):
        pain_bits.append(f"dolor {residual.get('pain_score_0_10', '?')}/10")
    if "distal" in str(residual.get("sensitivity_areas", "")).lower():
        pain_bits.append("sensibilidad distal")
    pain_text = (
        "Alivio distal y holgura "
        f"{clearance_mm:.1f} mm; longitud socket {length_fraction:.0%} del muñón."
        if pain_bits
        else f"Holgura nominal {clearance_mm:.1f} mm."
    )

    hours = goals.get("daily_use_expected_hours", "?")
    activity = goals.get("activity_level", "?")
    activity_text = (
        f"Uso ~{hours} h/día, actividad {activity}: pared reforzada y ventilación si clima caluroso."
    )

    skin_notes = []
    if residual.get("skin_issues"):
        skin_notes.append(f"piel: {', '.join(residual['skin_issues'])}")
    if flags.get("requires_skin_review"):
        skin_notes.append("requiere revisión dermatológica (fit_confidence cap ≤ 0.75)")
    skin_text = "; ".join(skin_notes) if skin_notes else "Sin contraindicaciones cutáneas reportadas."

    contraindications: list[str] = []
    if quality["blocked"]:
        contraindications.append(
            f"mean_error_mm > {_MEAN_ERROR_HARD_LIMIT}: re-escaneo o limpieza de malla antes de fabricar"
        )
    if flags.get("missing_data"):
        contraindications.append("evaluación presencial y escaneo validado pendientes")
    if quality.get("volume_estimated"):
        contraindications.append("volumen estimado por secciones — malla no cerrada")

    return {
        "pain_consideration": pain_text,
        "activity_adaptation": activity_text,
        "skin_safety_notes": skin_text,
        "contraindications": contraindications,
    }


def _derive_structure_params(
    report: dict[str, Any], height_mm: float, geometry: dict[str, Any] | None = None
) -> dict[str, Any]:
    design_prefs = report.get("design_preferences") or {}
    priorities = [str(p).lower() for p in design_prefs.get("top_priorities", [])]
    activity = str(_report_get(report, "functional_goals", "activity_level", default="")).lower()

    wall_prox, wall_dist = 3.5, 4.0
    if "resistencia" in priorities:
        wall_prox, wall_dist = 4.0, 4.5

    transtibial = is_transtibial_report(report)
    default_fraction = TRANSTIBIAL_DEFAULT_LENGTH_FRACTION if transtibial else 0.85
    residual = report.get("residual_limb_status") or {}
    pain_score = float(residual.get("pain_score_0_10", 0) or 0)
    if pain_score >= 3 or "distal" in str(residual.get("sensitivity_areas", "")).lower():
        default_fraction = 0.76 if transtibial else 0.78

    socket_prefs = report.get("socket_preferences") or {}
    explicit_frac = (
        float(socket_prefs["socket_length_fraction"])
        if "socket_length_fraction" in socket_prefs
        else None
    )
    explicit_trim = float(socket_prefs["trim_height_mm"]) if socket_prefs.get("trim_height_mm") else None
    trim_mm, length_fraction, _trim_source = resolve_socket_trim_from_geometry(
        geometry or {},
        height_mm,
        default_fraction=default_fraction,
        explicit_fraction=explicit_frac,
        explicit_trim_mm=explicit_trim,
        use_knee_detection=transtibial and explicit_frac is None and explicit_trim is None,
    )
    length_fraction = cap_transtibial_length_fraction(length_fraction, report)
    if abs(length_fraction * height_mm - trim_mm) > 0.5:
        trim_mm = round(height_mm * length_fraction, 3)

    env = _report_get(report, "functional_goals", "environment", default=[]) or []
    hot_climate = any("caluroso" in str(e).lower() for e in env)
    ventilation = {
        "enabled": hot_climate or bool(residual.get("skin_issues")),
        "pattern": "lateral_slots",
        "count": 4 if hot_climate else 2,
    }

    material = "TPU semi-rigid transpirable" if hot_climate else "TPU semi-rigid"

    return {
        "wall_thickness_mm": {"proximal": wall_prox, "distal": wall_dist},
        "trim_height_mm": round(trim_mm, 3),
        "socket_length_fraction": round(length_fraction, 4),
        "knee_landmark": (geometry or {}).get("knee_landmark"),
        "ventilation": ventilation,
        "proximal_adapter": {
            "enabled": False,
            "flare_mm": 0.0,
            "flare_height_fraction": 0.0,
            "collar_height_mm": 0.0,
            "collar_extra_wall_mm": 0.0,
        },
        "transtibial_profile": {
            "enabled": True,
            "patellar_bar_depth_mm": 2.0,
            "posterior_relief_mm": 0.8,
            "lateral_flare_mm": 2.5,
        },
        "prosthesis_adapter": {
            "enabled": True,
            "solid_height_mm": 28.0,
            "cap_ring_mm": 3.0,
            "neck_transition_fraction": 0.10,
            "neck_wall_mm": 3.5,
            "adapter_diameter_mm": 38.0,
            "adapter_plate_mm": 10.0,
        },
        "distal_closure": {
            "enabled": True,
            "cap_thickness_mm": 3.0,
            "solid_height_mm": 28.0,
            "cap_ring_mm": 3.0,
        },
        "recommended_material": material,
    }


def run_socket_design_agent(
    geometry: GeometryResponse | dict[str, Any],
    clinical_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    geo = _geometry_dict(geometry)
    report = clinical_report if clinical_report is not None else load_default_clinical_report()

    quality = _evaluate_agent_quality(geo)
    height_mm = float(geo.get("height_mm", 0.0))
    sections = list(geo.get("sections") or [])
    section_count = len([s for s in sections if int(s.get("contour_point_count", 0)) >= 3])

    clearance = _derive_clearance_mm(geo, report, quality)
    structure = _derive_structure_params(report, height_mm, geo)
    fit_confidence = _compute_fit_confidence(geo, report)
    residual = report.get("residual_limb_status") or {}

    geometry_reference = {
        "height_mm": round(height_mm, 3),
        "section_count": section_count,
        "coordinate_system": {
            "z_origin": "distal",
            "z_direction": "proximal",
            "units": "mm",
            "angular_note": "0° = +X, 90° = +Y; medial/lateral aproximado sin landmarks óseos",
        },
    }

    recon = geo.get("reconstruction_error") or {}
    agent_quality = {
        "passed": quality["passed"],
        "demo_eligible": quality["demo_eligible"],
        "mean_error_mm": round(quality["mean_error_mm"], 4),
        "max_error_mm": round(quality["max_error_mm"], 4),
        "p95_error_mm": round(float(recon.get("p95_error_mm", quality["max_error_mm"])), 4),
        "section_similarity": round(quality["section_similarity"], 4),
        "volume_cm3": quality["volume_cm3"],
        "volume_estimated": quality["volume_estimated"],
        "messages": quality["messages"],
    }

    clinical_reasoning = _build_clinical_reasoning(
        report,
        clearance,
        structure["socket_length_fraction"],
        quality,
    )

    mode = "production" if quality["passed"] else ("demo" if quality["demo_eligible"] else "blocked")
    handoff_steps = [
        "load sections[].contour per z_mm",
        "apply base_offsets.samples interpolated by z",
        "apply local_modifications by z range and angle",
        "apply transtibial PTB angular profile on inner surface",
        "trim socket length below knee flexion zone (socket_length_fraction)",
        "loft thin shell distal→proximal; z_max = stump entry, z_min = prosthesis",
        "add proximal rim flare at z_max (stump opening only)",
        "solid prosthesis adapter at z_min (-Z): closed distal",
    ]
    if quality["blocked"]:
        handoff_steps = ["BLOQUEADO: quality_gate no aprobado — corregir escaneo o malla"]

    cadquery_handoff = {
        "steps": handoff_steps,
        "target_fit_tolerance_mm": {"min": 1.0, "max": 2.0},
        "design_mode": mode,
    }

    socket_design: dict[str, Any] | None = None
    design_parameters: dict[str, Any] = {
        "agent_engine": "rules",
        "mode": mode,
        "radial_clearance_mm": clearance,
        **structure,
    }

    if not quality["blocked"]:
        socket_design = {
            "type": "transtibial_custom_socket",
            "cad_strategy": {
                "inner_surface": "loft_from_sections",
                "section_source_field": "geometry_analysis.sections",
                "offset_mode": "normal_2d",
                "outer_surface": "offset_wall",
            },
            "base_offsets": {
                "interpolation": "linear",
                "samples": _build_offset_samples(
                    sections,
                    height_mm,
                    clearance,
                    bool(residual.get("volume_changes_reported")),
                ),
            },
            "local_modifications": _build_local_modifications(
                structure["trim_height_mm"], report, clearance
            ),
            "structure": {
                "wall_thickness_mm": structure["wall_thickness_mm"],
                "trim_height_mm": structure["trim_height_mm"],
                "socket_length_fraction": structure["socket_length_fraction"],
                "ventilation": structure["ventilation"],
                "proximal_adapter": structure["proximal_adapter"],
                "transtibial_profile": structure["transtibial_profile"],
                "distal_closure": structure["distal_closure"],
                "prosthesis_adapter": structure["prosthesis_adapter"],
            },
            "recommended_material": structure["recommended_material"],
            "fit_confidence": fit_confidence,
        }

    result = {
        "quality_gate": agent_quality,
        "geometry_reference": geometry_reference,
        "socket_design": socket_design,
        "clinical_reasoning": clinical_reasoning,
        "cadquery_handoff": cadquery_handoff,
        "design_parameters": design_parameters,
    }
    return finalize_agent_payload(result, geo, report)
