#!/usr/bin/env python3
"""
CadQuery: genera socket.stl desde SocketDesignAgentResponse (POST /socket-design).

Uso local:
  python socket_design.cad.py --agent agent.json --out-dir ./output
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import Polygon
from shapely.validation import make_valid

try:
    import cadquery as cq
except ImportError as exc:
    raise SystemExit(
        "CadQuery no está instalado. Ejecuta: py -3.12 -m pip install cadquery"
    ) from exc


# ---------------------------------------------------------------------------
# Defaults de diseño (mm)
# ---------------------------------------------------------------------------
DEFAULT_RADIAL_CLEARANCE_MM = 1.5
MIN_RADIAL_CLEARANCE_MM = 1.0
MAX_RADIAL_CLEARANCE_MM = 3.0
DEFAULT_WALL_THICKNESS_MM = 4.0
DEFAULT_SOCKET_LENGTH_FRACTION = 0.85
DEFAULT_PROXIMAL_FILLET_MM = 2.0


@dataclass
class QualityDecision:
    passed: bool
    demo_eligible: bool
    blocked: bool
    messages: list[str]
    volume_estimated: bool


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Archivo no encontrado: {path}")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _report_get(report: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = report
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key, default)
    return node


def filter_valid_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [s for s in sections if int(s.get("contour_point_count", 0)) >= 3 and s.get("contour")]
    if not valid:
        raise ValueError("No hay secciones con contour_point_count ≥ 3")
    return sorted(valid, key=lambda s: float(s["z_mm"]))


def apply_socket_length(
    sections: list[dict[str, Any]], height_mm: float, length_fraction: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    z_min = float(sections[0]["z_mm"])
    z_max = float(sections[-1]["z_mm"])
    span = z_max - z_min
    if span <= 0:
        return sections, {"z_min": z_min, "z_max": z_max, "applied_fraction": length_fraction}

    target_span = min(span, height_mm * length_fraction)
    cutoff = z_min + target_span
    selected = [s for s in sections if float(s["z_mm"]) <= cutoff + 1e-6]
    if len(selected) < 3:
        selected = sections[: max(3, len(sections))]

    return selected, {
        "z_min": z_min,
        "z_max": float(selected[-1]["z_mm"]),
        "target_span_mm": round(target_span, 3),
        "applied_fraction": length_fraction,
        "sections_used": len(selected),
    }


def offset_contour_2d(
    contour: list[list[float]], distance_mm: float, max_retries: int = 4
) -> list[tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y in contour]
    if len(pts) < 3:
        raise ValueError("Contorno con menos de 3 puntos")

    if pts[0] != pts[-1]:
        pts.append(pts[0])

    poly = Polygon(pts)
    if not poly.is_valid:
        poly = make_valid(poly)
        if poly.geom_type != "Polygon":
            raise ValueError("Contorno inválido tras make_valid")

    last_error = "offset desconocido"
    for attempt in range(max_retries):
        d = distance_mm - attempt * 0.25
        if d <= 0:
            break
        try:
            expanded = poly.buffer(d, join_style=2, resolution=16)
        except Exception as exc:
            last_error = str(exc)
            continue

        if expanded.is_empty:
            last_error = "buffer vacío"
            continue

        if expanded.geom_type == "MultiPolygon":
            expanded = max(expanded.geoms, key=lambda g: g.area)

        if expanded.geom_type != "Polygon":
            last_error = f"geometría {expanded.geom_type}"
            continue

        coords = list(expanded.exterior.coords)
        if len(coords) < 4:
            last_error = "contorno degenerado"
            continue

        return [(float(x), float(y)) for x, y in coords[:-1]]

    raise ValueError(f"Offset 2D fallido ({distance_mm:.2f} mm): {last_error}")


def resample_contour(points: list[tuple[float, float]], n: int = 128) -> list[tuple[float, float]]:
    arr = np.array(points, dtype=np.float64)
    if arr.shape[0] < 3:
        return points
    closed = np.vstack([arr, arr[0]])
    seg_lens = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total = cum[-1]
    if total < 1e-9:
        return points
    targets = np.linspace(0.0, total, n, endpoint=False)
    resampled: list[tuple[float, float]] = []
    for t in targets:
        idx = int(np.searchsorted(cum, t, side="right") - 1)
        idx = min(max(idx, 0), len(seg_lens) - 1)
        seg_len = seg_lens[idx]
        if seg_len < 1e-12:
            resampled.append((float(closed[idx, 0]), float(closed[idx, 1])))
        else:
            alpha = (t - cum[idx]) / seg_len
            p = (1 - alpha) * closed[idx] + alpha * closed[idx + 1]
            resampled.append((float(p[0]), float(p[1])))
    return resampled


def interpolate_offset_at_z(
    z_mm: float,
    samples: list[dict[str, Any]],
    interpolation: str = "linear",
) -> float:
    """Interpola holgura radial desde socket_design.base_offsets.samples del agente."""
    if not samples:
        return DEFAULT_RADIAL_CLEARANCE_MM
    if interpolation != "linear":
        raise ValueError(f"Interpolación no soportada: {interpolation}")

    ordered = sorted(samples, key=lambda s: float(s["z_mm"]))
    z = float(z_mm)
    if z <= float(ordered[0]["z_mm"]):
        return float(ordered[0]["offset_mm"])
    if z >= float(ordered[-1]["z_mm"]):
        return float(ordered[-1]["offset_mm"])

    for i in range(len(ordered) - 1):
        z0 = float(ordered[i]["z_mm"])
        z1 = float(ordered[i + 1]["z_mm"])
        if z0 <= z <= z1:
            o0 = float(ordered[i]["offset_mm"])
            o1 = float(ordered[i + 1]["offset_mm"])
            if abs(z1 - z0) < 1e-9:
                return o0
            t = (z - z0) / (z1 - z0)
            return o0 + t * (o1 - o0)
    return float(ordered[-1]["offset_mm"])


def _angle_in_arc(deg: float, start_deg: float, end_deg: float) -> bool:
    deg = float(deg) % 360.0
    start = float(start_deg) % 360.0
    end = float(end_deg) % 360.0
    if abs(start) < 1e-6 and abs(end - 360.0) < 1e-3:
        return True
    if start <= end:
        return start <= deg <= end
    return deg >= start or deg <= end


def _section_angular_depth_factor(contour: list[list[float]], mod: dict[str, Any]) -> float:
    """Fracción de vértices del contorno dentro del arco angular del alivio."""
    start = float(mod.get("angle_start_deg", 0))
    end = float(mod.get("angle_end_deg", 360))
    if abs(start) < 1e-6 and abs(end - 360.0) < 1e-3:
        return 1.0
    arr = np.array(contour, dtype=np.float64)
    if arr.shape[0] < 3:
        return 1.0
    angles = np.degrees(np.arctan2(arr[:, 1], arr[:, 0])) % 360.0
    in_arc = sum(1 for a in angles if _angle_in_arc(float(a), start, end))
    return in_arc / max(len(angles), 1)


def apply_local_modifications_to_offsets(
    sections: list[dict[str, Any]],
    base_offsets_per_section: list[float],
    local_modifications: list[dict[str, Any]],
    height_mm: float,
) -> tuple[list[float], list[dict[str, Any]], list[str]]:
    """
    Ajusta offsets por sección según local_modifications del agente.
    Devuelve (offsets_finales, log_aplicación, notas).
    """
    if len(base_offsets_per_section) != len(sections):
        raise ValueError("base_offsets_per_section debe tener un valor por sección")

    final = list(base_offsets_per_section)
    applied_log: list[dict[str, Any]] = []
    notes: list[str] = []

    for mod in local_modifications:
        kind = str(mod.get("kind", "")).lower()
        z_min = float(mod.get("z_min_mm", 0))
        z_max = float(mod.get("z_max_mm", height_mm))
        depth = float(mod.get("depth_mm", 0))
        reason = str(mod.get("clinical_reason", ""))

        if kind == "ventilation_channel":
            applied_log.append(
                {
                    "kind": kind,
                    "z_range_mm": [z_min, z_max],
                    "depth_added_mm": 0.0,
                    "clinical_reason": reason,
                    "applied": False,
                    "note": "planned_not_meshed",
                }
            )
            notes.append(
                f"ventilation_channel z=[{z_min:.1f},{z_max:.1f}]: planificado, sin malla en MVP"
            )
            continue

        touched = False
        for i, sec in enumerate(sections):
            z = float(sec["z_mm"])
            if z < z_min - 1e-6 or z > z_max + 1e-6:
                continue
            factor = _section_angular_depth_factor(sec["contour"], mod)
            if factor <= 0:
                continue
            delta = depth * factor
            if kind in {"relief", "pressure_pad"}:
                final[i] += delta
                touched = True
            elif kind == "build_up":
                final[i] = max(final[i] - delta, MIN_RADIAL_CLEARANCE_MM)
                touched = True

        applied_log.append(
            {
                "kind": kind,
                "z_range_mm": [round(z_min, 3), round(z_max, 3)],
                "depth_added_mm": depth,
                "clinical_reason": reason,
                "applied": touched,
                "note": None if touched else "no_section_in_range",
            }
        )

    return final, applied_log, notes


def _loft_from_sections_per_offset(
    sections: list[dict[str, Any]],
    offsets_per_section: list[float],
) -> cq.Workplane:
    """Loft con offset radial distinto por sección (agent-driven)."""
    if len(sections) != len(offsets_per_section):
        raise ValueError("offsets_per_section debe alinearse con sections")

    z_base = float(sections[0]["z_mm"])
    loft_wp: cq.Workplane | None = None
    prev_z = 0.0

    for sec, offset_mm in zip(sections, offsets_per_section):
        z = float(sec["z_mm"]) - z_base
        delta = z - prev_z
        prev_z = z

        pts = offset_contour_2d(sec["contour"], float(offset_mm))
        pts = resample_contour(pts, 64)

        if loft_wp is None:
            loft_wp = cq.Workplane("XY").polyline(pts).close()
        else:
            loft_wp = loft_wp.workplane(offset=delta).polyline(pts).close()

    if loft_wp is None:
        raise ValueError("No hay perfiles para loft")
    return loft_wp.loft(combine=True)


def loft_shell_from_agent(
    sections: list[dict[str, Any]],
    inner_offsets: list[float],
    wall_thickness_mm: float,
) -> cq.Workplane:
    outer_offsets = [o + wall_thickness_mm for o in inner_offsets]
    outer_body = _loft_from_sections_per_offset(sections, outer_offsets)
    inner_body = _loft_from_sections_per_offset(sections, inner_offsets)
    return outer_body.cut(inner_body)


def _validate_agent_response(agent_response: dict[str, Any]) -> dict[str, Any]:
    try:
        from app.schemas.socket_design import SocketDesignAgentResponse

        return SocketDesignAgentResponse(**agent_response).model_dump()
    except ImportError:
        return agent_response


def _agent_quality_decision(agent: dict[str, Any], geometry: dict[str, Any]) -> QualityDecision:
    qg = agent.get("quality_gate") or geometry.get("quality_gate") or {}
    passed = bool(qg.get("passed", False))
    demo_eligible = bool(qg.get("demo_eligible", False))
    blocked = agent.get("socket_design") is None or (not passed and not demo_eligible)
    return QualityDecision(
        passed=passed,
        demo_eligible=demo_eligible,
        blocked=blocked,
        messages=list(qg.get("messages", [])),
        volume_estimated=bool(qg.get("volume_estimated", False)),
    )


def write_agent_blocked_report(
    out_dir: Path,
    geometry: dict[str, Any],
    agent_response: dict[str, Any],
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    qg = agent_response.get("quality_gate") or {}
    clinical = agent_response.get("clinical_reasoning") or {}
    design_params = agent_response.get("design_parameters") or {}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked",
        "agent_driven": True,
        "agent_source": {
            "design_parameters": design_params,
            "agent_engine": design_params.get("agent_engine"),
            "fit_confidence": (agent_response.get("socket_design") or {}).get("fit_confidence"),
            "recommended_material": (agent_response.get("socket_design") or {}).get(
                "recommended_material"
            ),
            "cadquery_handoff": agent_response.get("cadquery_handoff"),
            "clinical_reasoning": clinical,
        },
        "quality_gate": qg,
        "cad_execution": {
            "blocked_reason": reason or "socket_design is null or quality gate bloqueante",
            "sections_input": len(geometry.get("sections", [])),
            "sections_loft": 0,
        },
        "geometry_checks": {
            "height_mm_stump": geometry.get("height_mm"),
            "reconstruction_error": geometry.get("reconstruction_error"),
            "section_similarity": geometry.get("section_similarity"),
            "volume_stump_cm3": geometry.get("volume_cm3"),
        },
        "warnings": [
            reason or "No se generó STL: socket_design ausente o quality gate bloqueante.",
            *(clinical.get("contraindications") or []),
        ],
        "recommended_clinical_review": list(
            agent_response.get("cadquery_handoff", {}).get("steps", [])[:2]
        )
        or ["Revisión clínica presencial obligatoria."],
        "exports": {"stl": None, "step": None, "report": "agent_cad_report.json"},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "agent_cad_report.json"
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def resolve_cad_geometry(
    agent_response: dict[str, Any],
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Usa cad_geometry embebido en el agente o geometry_analysis externo."""
    cad = agent_response.get("cad_geometry")
    if isinstance(cad, dict) and cad.get("sections"):
        return cad
    if geometry and geometry.get("sections"):
        return geometry
    raise ValueError(
        "Se requiere cad_geometry.sections en agent_response o --geometry / geometry_analysis"
    )


def generate_socket_from_agent(
    geometry: dict[str, Any] | None,
    agent_response: dict[str, Any],
    out_dir: Path,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Pipeline agent-driven: SocketDesignAgentResponse (+ cad_geometry) → STL/STEP + reporte.

    Flujo Signa: POST /upload → POST /analyze → POST /socket-design → POST /socket-generate
    """
    agent = _validate_agent_response(agent_response)
    geometry = resolve_cad_geometry(agent, geometry)
    socket_spec = agent.get("socket_design")
    quality = _agent_quality_decision(agent, geometry)

    if socket_spec is None or quality.blocked:
        reason = "socket_design is null" if socket_spec is None else "quality_gate bloqueante"
        return write_agent_blocked_report(out_dir, geometry, agent, reason=reason)

    structure = socket_spec.get("structure") or {}
    base_offsets = socket_spec.get("base_offsets") or {}
    samples = list(base_offsets.get("samples") or [])
    local_mods = list(socket_spec.get("local_modifications") or [])
    wall = structure.get("wall_thickness_mm") or {}
    wall_prox = float(wall.get("proximal", DEFAULT_WALL_THICKNESS_MM))
    wall_dist = float(wall.get("distal", DEFAULT_WALL_THICKNESS_MM))
    wall_used = max(wall_prox, wall_dist)

    height_mm = float(geometry.get("height_mm") or agent.get("geometry_reference", {}).get("height_mm", 0))
    length_fraction = float(structure.get("socket_length_fraction", DEFAULT_SOCKET_LENGTH_FRACTION))
    trim_height = float(structure.get("trim_height_mm", 0) or 0)
    if trim_height > 0 and height_mm > 0:
        length_fraction = min(trim_height / height_mm, 1.0)

    ventilation = structure.get("ventilation") or {}
    design_mode = (agent.get("cadquery_handoff") or {}).get("design_mode", "production")
    status = design_mode if design_mode in {"production", "demo"} else ("production" if quality.passed else "demo")

    sections = filter_valid_sections(geometry["sections"])
    sections, length_meta = apply_socket_length(sections, height_mm, length_fraction)

    base_per_section = [
        interpolate_offset_at_z(float(s["z_mm"]), samples, base_offsets.get("interpolation", "linear"))
        for s in sections
    ]
    inner_offsets, mods_log, mod_notes = apply_local_modifications_to_offsets(
        sections, base_per_section, local_mods, height_mm
    )

    fillet_mm = float(agent.get("design_parameters", {}).get("proximal_fillet_mm", DEFAULT_PROXIMAL_FILLET_MM))

    model = loft_shell_from_agent(sections, inner_offsets, wall_used)
    model = apply_proximal_fillet(model, fillet_mm)

    bbox = compute_bbox_mm(model)
    socket_volume = approximate_volume_cm3(model)
    height_check = {
        "expected_height_mm": round(height_mm * length_fraction, 3),
        **bbox,
    }

    warnings: list[str] = list(mod_notes)
    fit_conf = float(socket_spec.get("fit_confidence", 1.0))
    if fit_conf < 0.7:
        warnings.append(f"fit_confidence bajo ({fit_conf}): revisión clínica obligatoria.")
    if not quality.passed:
        warnings.append("quality_gate.passed=false: no afirmar aptitud clínica sin revisión.")
    if quality.volume_estimated:
        warnings.append("volume_cm3 estimado (malla no cerrada).")

    clinical = agent.get("clinical_reasoning") or {}
    if clinical.get("contraindications"):
        warnings.extend(str(c) for c in clinical["contraindications"])

    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / "socket.stl"
    step_path = out_dir / "socket.step"
    script_copy = out_dir / "socket_design.cad.py"

    cq.exporters.export(model, str(stl_path))
    step_exported = False
    try:
        cq.exporters.export(model, str(step_path))
        step_exported = True
    except Exception as exc:
        warnings.append(f"Export STEP omitido: {exc}")

    shutil.copy2(Path(__file__).resolve(), script_copy)

    offsets_applied = [
        {"z_mm": round(float(s["z_mm"]), 3), "offset_mm": round(float(o), 3)}
        for s, o in zip(sections, inner_offsets)
    ]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "agent_driven": True,
        "agent_source": {
            "design_parameters": agent.get("design_parameters") or {},
            "agent_engine": (agent.get("design_parameters") or {}).get("agent_engine"),
            "fit_confidence": socket_spec.get("fit_confidence"),
            "recommended_material": socket_spec.get("recommended_material"),
            "cadquery_handoff": agent.get("cadquery_handoff"),
            "clinical_reasoning": clinical,
        },
        "quality_gate": agent.get("quality_gate"),
        "cad_execution": {
            "base_offsets_applied": offsets_applied,
            "local_modifications_applied": mods_log,
            "wall_thickness_mm": {
                "proximal": wall_prox,
                "distal": wall_dist,
                "used_for_loft": wall_used,
            },
            "socket_length_fraction": length_fraction,
            "sections_input": len(geometry.get("sections", [])),
            "sections_loft": len(sections),
            "length_processing": length_meta,
            "ventilation": {
                "enabled": bool(ventilation.get("enabled", False)),
                "pattern": ventilation.get("pattern"),
                "count": ventilation.get("count"),
                "note": "no mesh in MVP" if ventilation.get("enabled") else None,
            },
            "proximal_fillet_mm": fillet_mm,
        },
        "geometry_checks": {
            "height_mm_stump": height_mm,
            "n_sections_input": len(geometry.get("sections", [])),
            "n_sections_loft": len(sections),
            "volume_stump_cm3": geometry.get("volume_cm3"),
            "volume_socket_cm3": socket_volume,
            "bbox_mm": bbox,
            "height_coherence": height_check,
            "reconstruction_error": geometry.get("reconstruction_error"),
            "section_similarity": geometry.get("section_similarity"),
        },
        "warnings": warnings,
        "recommended_clinical_review": (
            list(clinical.get("contraindications") or [])
            + build_recommended_clinical_review(quality, report or {})
        ),
        "exports": {
            "stl": str(stl_path.name),
            "step": str(step_path.name) if step_exported else None,
            "report": "agent_cad_report.json",
            "script": str(script_copy.name),
        },
    }

    report_path = out_dir / "agent_cad_report.json"
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def apply_proximal_fillet(model: cq.Workplane, fillet_mm: float) -> cq.Workplane:
    if fillet_mm <= 0:
        return model
    try:
        bbox = model.val().BoundingBox()
        z_max = bbox.zmax
        # Seleccionar aristas cercanas al borde proximal (apertura superior)
        result = model.edges(f"|Z| > {z_max - 0.5}").fillet(fillet_mm)
        return result
    except Exception:
        return model


def compute_bbox_mm(model: cq.Workplane) -> dict[str, float]:
    bb = model.val().BoundingBox()
    return {
        "x_min": round(bb.xmin, 3),
        "x_max": round(bb.xmax, 3),
        "y_min": round(bb.ymin, 3),
        "y_max": round(bb.ymax, 3),
        "z_min": round(bb.zmin, 3),
        "z_max": round(bb.zmax, 3),
        "height_mm": round(bb.zmax - bb.zmin, 3),
    }


def approximate_volume_cm3(model: cq.Workplane) -> float | None:
    try:
        vol_mm3 = model.val().Volume()
        if vol_mm3 > 0 and math.isfinite(vol_mm3):
            return round(vol_mm3 / 1000.0, 3)
    except Exception:
        pass
    return None


def build_recommended_clinical_review(
    quality: QualityDecision, report: dict[str, Any]
) -> list[str]:
    items = [
        "Evaluación presencial de interfaz muñón-socket y alineación.",
        "Verificación de holgura radial con liner de prueba.",
    ]
    if quality.demo_eligible and not quality.passed:
        items.append("Re-escaneo 3D con malla cerrada para pasar quality gate de producción.")
    if _report_get(report, "residual_limb_status", "pain_present"):
        items.append("Mapa de presión distal y ajuste de longitud tras prueba de marcha.")
    if _report_get(report, "professional_flags", "requires_skin_review"):
        items.append("Revisión de piel en zona distal y ventilación/perforaciones según sudoración.")
    return items


def print_summary(payload: dict[str, Any]) -> None:
    status = payload.get("status", "?")
    agent_driven = payload.get("agent_driven", False)
    print(f"Estado: {status.upper()}" + (" (agent-driven)" if agent_driven else ""))
    cad_exec = payload.get("cad_execution") or {}
    params = payload.get("parameters_used") or cad_exec
    if params.get("radial_clearance_mm") is not None:
        print(f"Holgura radial: {params.get('radial_clearance_mm')} mm")
    if cad_exec.get("wall_thickness_mm"):
        wt = cad_exec["wall_thickness_mm"]
        print(f"Espesor pared (loft): {wt.get('used_for_loft')} mm")
    if cad_exec.get("socket_length_fraction") is not None:
        print(f"Longitud socket: {cad_exec['socket_length_fraction']:.0%} de height_mm")
    elif params.get("socket_length_fraction") is not None:
        print(f"Longitud socket: {params.get('socket_length_fraction', 0):.0%} de height_mm")
    qg = payload.get("quality_gate") or payload.get("quality_gate_summary") or {}
    print(f"Quality gate passed={qg.get('passed')} demo_eligible={qg.get('demo_eligible')}")
    if payload.get("warnings"):
        print("Advertencias:")
        for w in payload["warnings"][:5]:
            print(f"  - {w}")
    exports = payload.get("exports")
    if exports:
        print(f"Exportado: {exports.get('stl')}" + (f", {exports.get('step')}" if exports.get("step") else ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Genera socket.stl desde SocketDesignAgentResponse (POST /socket-design)."
    )
    parser.add_argument(
        "--agent",
        required=True,
        type=Path,
        help="JSON de POST /socket-design",
    )
    parser.add_argument(
        "--geometry",
        type=Path,
        default=None,
        help="Salida de POST /analyze (solo si agent.cad_geometry no trae sections)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="datos_reporte.json opcional (notas en agent_cad_report.json)",
    )
    parser.add_argument("--out-dir", required=True, type=Path, help="Directorio de salida")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        geometry = load_json(args.geometry) if args.geometry else None
        report = load_json(args.report) if args.report else {}
        agent = load_json(args.agent)
        payload = generate_socket_from_agent(geometry, agent, args.out_dir, report=report or None)
        print_summary(payload)
        if payload.get("status") == "blocked":
            return 2
        return 0
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR inesperado: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
