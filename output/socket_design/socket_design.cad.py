#!/usr/bin/env python3
"""
Diseño paramétrico de socket transtibial a partir de geometry_analysis.json y datos_reporte.json.

Uso:
  python socket_design.cad.py --geometry path/to/geometry_analysis.json \\
      --report path/to/datos_reporte.json --out-dir ./output
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass, field
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
DEFAULT_END_TRIM_FRACTION = 0.10
DEFAULT_PROXIMAL_FILLET_MM = 2.0
IRREGULARITY_TRIM_MULTIPLIER = 2.0
HEIGHT_TOLERANCE_FRACTION = 0.15


@dataclass
class DesignParameters:
    radial_clearance_mm: float = DEFAULT_RADIAL_CLEARANCE_MM
    wall_thickness_mm: float = DEFAULT_WALL_THICKNESS_MM
    socket_length_fraction: float = DEFAULT_SOCKET_LENGTH_FRACTION
    proximal_fillet_mm: float = DEFAULT_PROXIMAL_FILLET_MM
    trim_bottom_fraction: float = 0.0
    trim_top_fraction: float = 0.0
    mode: str = "production"
    adjustments: list[str] = field(default_factory=list)


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


def evaluate_quality_gate(geometry: dict[str, Any]) -> QualityDecision:
    qg = geometry.get("quality_gate", {})
    passed = bool(qg.get("passed", False))
    demo_eligible = bool(qg.get("demo_eligible", False))
    blocked = not passed and not demo_eligible
    return QualityDecision(
        passed=passed,
        demo_eligible=demo_eligible,
        blocked=blocked,
        messages=list(qg.get("messages", [])),
        volume_estimated=bool(qg.get("volume_estimated", False)),
    )


def _report_get(report: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = report
    for key in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(key, default)
    return node


def derive_design_parameters(
    geometry: dict[str, Any],
    report: dict[str, Any],
    quality: QualityDecision,
) -> DesignParameters:
    params = DesignParameters()
    recon = geometry.get("reconstruction_error", {})
    mean_err = float(recon.get("mean_error_mm", 0.0))
    max_err = float(recon.get("max_error_mm", 0.0))

    if quality.passed:
        params.mode = "production"
    elif quality.demo_eligible:
        params.mode = "demo"
        params.adjustments.append("Modo demo: holguras conservadoras por quality gate no aprobado")

    # Holgura radial base
    clearance = DEFAULT_RADIAL_CLEARANCE_MM
    residual = report.get("residual_limb_status", {})
    prefs = report.get("design_preferences", {})

    sensitivity = str(residual.get("sensitivity_areas", "")).lower()
    conservative_triggers = [
        bool(residual.get("volume_changes_reported")),
        "distal" in sensitivity,
        mean_err > 2.0,
    ]
    if any(conservative_triggers):
        clearance += 0.5
        reasons = []
        if residual.get("volume_changes_reported"):
            reasons.append("cambios de volumen")
        if "distal" in sensitivity:
            reasons.append("sensibilidad distal")
        if mean_err > 2.0:
            reasons.append(f"mean_error_mm={mean_err:.2f}")
        params.adjustments.append(f"+0.5 mm holgura por: {', '.join(reasons)}")

    if params.mode == "demo":
        clearance = max(clearance, 2.0)
        params.adjustments.append("Holgura mínima 2.0 mm en modo demo")

    clearance = float(np.clip(clearance, MIN_RADIAL_CLEARANCE_MM, MAX_RADIAL_CLEARANCE_MM))
    params.radial_clearance_mm = round(clearance, 2)

    # Espesor de pared
    wall = DEFAULT_WALL_THICKNESS_MM
    priorities = [str(p).lower() for p in prefs.get("top_priorities", [])]
    activity = str(_report_get(report, "functional_goals", "activity_level", default="")).lower()
    if "resistencia" in priorities and activity in {"moderado", "moderada", "alto", "alta"}:
        wall += 1.0
        params.adjustments.append("+1 mm espesor por prioridad resistencia y actividad moderada+")
    params.wall_thickness_mm = wall

    # Longitud útil
    length_frac = DEFAULT_SOCKET_LENGTH_FRACTION
    pain_score = float(residual.get("pain_score_0_10", 0) or 0)
    if pain_score >= 3 or "distal" in sensitivity:
        length_frac = 0.78
        params.adjustments.append("Longitud reducida a 78% por dolor/sensibilidad distal")
    params.socket_length_fraction = length_frac

    # Filete proximal
    fillet = DEFAULT_PROXIMAL_FILLET_MM
    if "comodidad" in priorities:
        fillet = 3.0
        params.adjustments.append("Filete proximal 3 mm por prioridad comodidad")
    params.proximal_fillet_mm = fillet

    # Recorte de extremos
    shape_profile = geometry.get("shape_profile", {})
    irregularity = shape_profile.get("irregularity_index", [])
    trim_bottom = trim_top = False

    if max_err > 15.0:
        trim_bottom = trim_top = True
        params.adjustments.append(f"Recorte 10% extremos por max_error_mm={max_err:.2f} > 15")

    if irregularity:
        positive = [v for v in irregularity if v > 0]
        if positive:
            median_irr = float(np.median(positive))
            threshold = IRREGULARITY_TRIM_MULTIPLIER * median_irr
            n = len(irregularity)
            n_edge = max(1, int(math.ceil(n * DEFAULT_END_TRIM_FRACTION)))
            if any(v > threshold for v in irregularity[:n_edge]):
                trim_bottom = True
                params.adjustments.append("Recorte inferior por irregularity_index elevado en extremo distal")
            if any(v > threshold for v in irregularity[-n_edge:]):
                trim_top = True
                params.adjustments.append("Recorte superior por irregularity_index elevado en extremo proximal")

    if trim_bottom:
        params.trim_bottom_fraction = DEFAULT_END_TRIM_FRACTION
    if trim_top:
        params.trim_top_fraction = DEFAULT_END_TRIM_FRACTION

    return params


def build_clinical_rationale(report: dict[str, Any], params: DesignParameters) -> list[str]:
    bullets: list[str] = []
    side = _report_get(report, "amputation_profile", "side", default="?")
    level = _report_get(report, "amputation_profile", "level_interpreted", default="?")
    bullets.append(f"Muñón {level} ({side}): socket basado en contornos del analyze, sin copiar STL de referencia.")

    hours = _report_get(report, "functional_goals", "daily_use_expected_hours")
    if hours:
        bullets.append(f"Uso previsto {hours} h/día → holgura {params.radial_clearance_mm} mm y longitud {params.socket_length_fraction:.0%}.")

    prefs = report.get("design_preferences", {}).get("top_priorities", [])
    if prefs:
        bullets.append(f"Prioridades del paciente ({', '.join(prefs[:3])}) traducidas a espesor {params.wall_thickness_mm} mm y filete {params.proximal_fillet_mm} mm.")

    residual = report.get("residual_limb_status", {})
    if residual.get("skin_issues"):
        bullets.append(
            "Sudoración/irritación: holgura conservadora; ventilación distal recomendada en revisión clínica (no cerrar herméticamente)."
        )
    if residual.get("pain_present"):
        bullets.append(
            f"Dolor reportado ({residual.get('pain_score_0_10', '?')}/10): longitud acortada para aliviar presión distal."
        )

    flags = report.get("professional_flags", {})
    missing = flags.get("missing_data", [])
    if missing:
        bullets.append(f"Datos faltantes ({', '.join(missing[:3])}): diseño preliminar, no apto sin evaluación presencial.")

    return bullets


def filter_valid_sections(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = [s for s in sections if int(s.get("contour_point_count", 0)) >= 3 and s.get("contour")]
    if not valid:
        raise ValueError("No hay secciones con contour_point_count ≥ 3")
    return sorted(valid, key=lambda s: float(s["z_mm"]))


def trim_section_extremes(
    sections: list[dict[str, Any]], params: DesignParameters
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    n = len(sections)
    start = int(round(n * params.trim_bottom_fraction))
    end = n - int(round(n * params.trim_top_fraction))
    if end - start < 3:
        raise ValueError(
            f"Recorte de extremos deja menos de 3 secciones ({end - start}). "
            "Reduce trim o mejora el escaneo."
        )
    trimmed = sections[start:end]
    meta = {
        "sections_before": n,
        "sections_after_trim": len(trimmed),
        "trim_bottom_fraction": params.trim_bottom_fraction,
        "trim_top_fraction": params.trim_top_fraction,
        "z_range_mm": [trimmed[0]["z_mm"], trimmed[-1]["z_mm"]],
    }
    return trimmed, meta


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


def _loft_from_sections(
    sections: list[dict[str, Any]],
    offset_mm: float,
) -> cq.Workplane:
    """Loft longitudinal a partir de contornos con offset radial uniforme."""
    z_base = float(sections[0]["z_mm"])
    loft_wp: cq.Workplane | None = None
    prev_z = 0.0

    for i, sec in enumerate(sections):
        z = float(sec["z_mm"]) - z_base
        delta = z - prev_z
        prev_z = z

        pts = offset_contour_2d(sec["contour"], offset_mm)
        pts = resample_contour(pts, 64)

        if loft_wp is None:
            loft_wp = cq.Workplane("XY").polyline(pts).close()
        else:
            loft_wp = loft_wp.workplane(offset=delta).polyline(pts).close()

    if loft_wp is None:
        raise ValueError("No hay perfiles para loft")
    return loft_wp.loft(combine=True)


def loft_shell(
    sections: list[dict[str, Any]],
    inner_offset_mm: float,
    wall_thickness_mm: float,
) -> cq.Workplane:
    outer_body = _loft_from_sections(sections, inner_offset_mm + wall_thickness_mm)
    inner_body = _loft_from_sections(sections, inner_offset_mm)
    return outer_body.cut(inner_body)


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


def build_warnings(
    geometry: dict[str, Any],
    quality: QualityDecision,
    params: DesignParameters,
    report: dict[str, Any],
    height_check: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if quality.volume_estimated:
        warnings.append("volume_cm3 estimado por integración de secciones (malla no cerrada).")
    if not quality.passed:
        warnings.append("quality_gate.passed=false: no afirmar aptitud clínica sin revisión.")
    if params.mode == "demo":
        warnings.append("Socket demo: holguras conservadoras; fabricación clínica requiere re-escaneo/mejor calidad.")

    recon = geometry.get("reconstruction_error", {})
    if float(recon.get("mean_error_mm", 0)) > 2.0:
        warnings.append(f"mean_error_mm elevado ({recon['mean_error_mm']}) → holgura aumentada.")

    expected_h = float(geometry.get("height_mm", 0))
    actual_h = float(height_check.get("height_mm", 0))
    if expected_h > 0:
        rel_err = abs(actual_h - expected_h * params.socket_length_fraction) / expected_h
        if rel_err > HEIGHT_TOLERANCE_FRACTION:
            warnings.append(
                f"Altura del modelo ({actual_h} mm) difiere >15% de la longitud objetivo "
                f"({expected_h * params.socket_length_fraction:.1f} mm)."
            )

    if report.get("professional_flags", {}).get("requires_skin_review"):
        warnings.append("Revisión dermatológica recomendada antes de fabricación.")

    return warnings


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


def write_blocked_report(
    out_dir: Path,
    geometry: dict[str, Any],
    report: dict[str, Any],
    quality: QualityDecision,
    params: DesignParameters,
) -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "blocked",
        "quality_gate_summary": {
            "passed": quality.passed,
            "demo_eligible": quality.demo_eligible,
            "volume_estimated": quality.volume_estimated,
            "messages": quality.messages,
        },
        "parameters_used": {
            "mode": params.mode,
            "radial_clearance_mm": params.radial_clearance_mm,
            "wall_thickness_mm": params.wall_thickness_mm,
            "socket_length_fraction": params.socket_length_fraction,
            "proximal_fillet_mm": params.proximal_fillet_mm,
            "trim_bottom_fraction": params.trim_bottom_fraction,
            "trim_top_fraction": params.trim_top_fraction,
            "adjustments": params.adjustments,
        },
        "geometry_checks": {
            "height_mm": geometry.get("height_mm"),
            "n_sections": len(geometry.get("sections", [])),
            "volume_stump_cm3": geometry.get("volume_cm3"),
            "reconstruction_error": geometry.get("reconstruction_error"),
            "section_similarity": geometry.get("section_similarity"),
        },
        "clinical_rationale": build_clinical_rationale(report, params),
        "warnings": [
            "Quality gate bloqueante: demo_eligible=false. No se generó STL.",
            *build_warnings(geometry, quality, params, report, {}),
        ],
        "recommended_clinical_review": build_recommended_clinical_review(quality, report),
        "improvements_for_scan": [
            "Cerrar malla watertight o completar extremos del escaneo.",
            "Reducir mean_error_mm ≤ 3.0 y section_similarity ≥ 0.80 (demo) o ≤2.0 / ≥0.85 (producción).",
            "Aumentar resolución del escaneo en zona distal y proximal.",
            "Repetir analyze tras limpieza de artefactos en extremos.",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "socket_design_report.json"
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def generate_socket(
    geometry: dict[str, Any],
    report: dict[str, Any],
    out_dir: Path,
) -> dict[str, Any]:
    quality = evaluate_quality_gate(geometry)
    params = derive_design_parameters(geometry, report, quality)

    if quality.blocked:
        return write_blocked_report(out_dir, geometry, report, quality, params)

    sections = filter_valid_sections(geometry["sections"])
    sections, trim_meta = trim_section_extremes(sections, params)
    height_mm = float(geometry.get("height_mm", sections[-1]["z_mm"]))
    sections, length_meta = apply_socket_length(sections, height_mm, params.socket_length_fraction)

    model = loft_shell(
        sections,
        inner_offset_mm=params.radial_clearance_mm,
        wall_thickness_mm=params.wall_thickness_mm,
    )
    model = apply_proximal_fillet(model, params.proximal_fillet_mm)

    bbox = compute_bbox_mm(model)
    socket_volume = approximate_volume_cm3(model)

    height_check = {
        "expected_height_mm": round(height_mm * params.socket_length_fraction, 3),
        **bbox,
    }
    warnings = build_warnings(geometry, quality, params, report, height_check)

    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / "socket.stl"
    step_path = out_dir / "socket.step"
    script_copy = out_dir / "socket_design.cad.py"

    cq.exporters.export(model, str(stl_path))
    try:
        cq.exporters.export(model, str(step_path))
        step_exported = True
    except Exception as exc:
        step_exported = False
        warnings.append(f"Export STEP omitido: {exc}")

    shutil.copy2(Path(__file__).resolve(), script_copy)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": params.mode,
        "quality_gate_summary": {
            "passed": quality.passed,
            "demo_eligible": quality.demo_eligible,
            "volume_estimated": quality.volume_estimated,
            "messages": quality.messages,
        },
        "parameters_used": {
            "mode": params.mode,
            "radial_clearance_mm": params.radial_clearance_mm,
            "wall_thickness_mm": params.wall_thickness_mm,
            "socket_length_fraction": params.socket_length_fraction,
            "proximal_fillet_mm": params.proximal_fillet_mm,
            "trim_bottom_fraction": params.trim_bottom_fraction,
            "trim_top_fraction": params.trim_top_fraction,
            "adjustments": params.adjustments,
        },
        "section_processing": {
            "trim": trim_meta,
            "length": length_meta,
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
        "clinical_rationale": build_clinical_rationale(report, params),
        "warnings": warnings,
        "recommended_clinical_review": build_recommended_clinical_review(quality, report),
        "exports": {
            "stl": str(stl_path.name),
            "step": str(step_path.name) if step_exported else None,
            "script": str(script_copy.name),
        },
    }

    report_path = out_dir / "socket_design_report.json"
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    return payload


def print_summary(payload: dict[str, Any]) -> None:
    status = payload.get("status", "?")
    params = payload.get("parameters_used", {})
    print(f"Estado: {status.upper()}")
    print(f"Holgura radial: {params.get('radial_clearance_mm')} mm")
    print(f"Espesor pared: {params.get('wall_thickness_mm')} mm")
    print(f"Longitud socket: {params.get('socket_length_fraction', 0):.0%} de height_mm")
    qg = payload.get("quality_gate_summary", {})
    print(f"Quality gate passed={qg.get('passed')} demo_eligible={qg.get('demo_eligible')}")
    if payload.get("warnings"):
        print("Advertencias:")
        for w in payload["warnings"][:5]:
            print(f"  - {w}")
    exports = payload.get("exports")
    if exports:
        print(f"Exportado: {exports.get('stl')}" + (f", {exports.get('step')}" if exports.get("step") else ""))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diseño paramétrico de socket transtibial (CadQuery)")
    parser.add_argument("--geometry", required=True, type=Path, help="Ruta a geometry_analysis.json")
    parser.add_argument("--report", required=True, type=Path, help="Ruta a datos_reporte.json")
    parser.add_argument("--out-dir", required=True, type=Path, help="Directorio de salida")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        geometry = load_json(args.geometry)
        report = load_json(args.report)
        payload = generate_socket(geometry, report, args.out_dir)
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
