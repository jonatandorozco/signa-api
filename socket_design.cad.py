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
DEFAULT_WALL_THICKNESS_MM = 3.5
DEFAULT_WALL_THICKNESS_DISTAL_MM = 4.0
MIN_SHELL_WALL_MM = 3.0
DEFAULT_SOCKET_LENGTH_FRACTION = 0.80
DEFAULT_PROXIMAL_FILLET_MM = 1.5
DEFAULT_PROXIMAL_FLARE_MM = 3.0
DEFAULT_PROXIMAL_FLARE_HEIGHT_FRACTION = 0.12
DEFAULT_ADAPTER_COLLAR_HEIGHT_MM = 18.0
DEFAULT_ADAPTER_COLLAR_EXTRA_WALL_MM = 1.5
DEFAULT_DISTAL_CAP_MM = 3.0
DEFAULT_PROSTHESIS_ADAPTER_HEIGHT_MM = 28.0
DEFAULT_DISTAL_CORE_FILL_FRACTION = 0.14
DEFAULT_DISTAL_CORE_MAX_Z_FRACTION = 0.22
DEFAULT_PROXIMAL_ENTRY_SECTIONS = 4
DEFAULT_PLUG_TAPER_TIERS = 4
DEFAULT_PROSTHESIS_NECK_FRACTION = 0.10
DEFAULT_PROSTHESIS_NECK_WALL_MM = 3.5
DEFAULT_PROSTHESIS_ADAPTER_DIAMETER_MM = 38.0
DEFAULT_PROSTHESIS_ADAPTER_PLATE_MM = 10.0
PROXIMAL_SIMPLE_FIT = True
# Boca proximal: solo contorno del escaneo + holgura (mm desde z_max), sin PTB ni sólidos
DEFAULT_PROXIMAL_ENTRY_DEPTH_MM = 12.0
MAX_PROXIMAL_ENTRY_DEPTH_MM = 18.0
DISTAL_ANCHOR_MAX_SECTIONS = 3
DEFAULT_PATELLAR_BAR_DEPTH_MM = 2.0
DEFAULT_POSTERIOR_RELIEF_MM = 0.8
MAX_LOFT_SECTIONS = 20
LOFT_CONTOUR_POINTS = 48
# Muñón transtibial plausible; por encima → error de unidades en entrada o export
_MAX_STL_EXTENT_MM = 900.0


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


def subsample_sections_for_loft(
    sections: list[dict[str, Any]], max_sections: int = MAX_LOFT_SECTIONS
) -> list[dict[str, Any]]:
    """Reduce secciones para loft OCCT estable (demasiadas secciones → Null TopoDS_Shape)."""
    if len(sections) <= max_sections:
        return sections
    indices = [
        int(round(i * (len(sections) - 1) / (max_sections - 1)))
        for i in range(max_sections)
    ]
    indices = sorted(set(indices) | {0, len(sections) - 1})
    return [sections[i] for i in indices]


def _shape_is_valid(model: cq.Workplane) -> bool:
    try:
        solid = model.val()
        if solid is None:
            return False
        volume = float(solid.Volume())
        return volume > 0 and math.isfinite(volume)
    except Exception:
        return False


def _build_socket_shell(
    sections: list[dict[str, Any]],
    inner_offsets: list[float],
    *,
    wall_proximal_mm: float,
    wall_distal_mm: float,
    transtibial_profile: dict[str, Any],
    outer_flares: list[float],
    extra_walls: list[float],
    neck_transition_fraction: float = DEFAULT_PROSTHESIS_NECK_FRACTION,
    neck_wall_mm: float = DEFAULT_PROSTHESIS_NECK_WALL_MM,
    proximal_entry_depth_mm: float = DEFAULT_PROXIMAL_ENTRY_DEPTH_MM,
) -> tuple[cq.Workplane, tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]], list[str]]:
    """
    Construye cáscara socket con fallback progresivo si OCCT devuelve sólido nulo.
    """
    notes: list[str] = []

    def _try_transtibial(profile: dict[str, Any]) -> tuple[cq.Workplane | None, tuple | None]:
        inner_profiles, outer_profiles = build_transtibial_socket_profiles(
            sections,
            inner_offsets,
            wall_proximal_mm=wall_proximal_mm,
            wall_distal_mm=wall_distal_mm,
            outer_flare_per_section=outer_flares,
            extra_wall_per_section=extra_walls,
            transtibial_profile=profile,
            neck_transition_fraction=neck_transition_fraction,
            neck_wall_mm=neck_wall_mm,
            proximal_entry_depth_mm=proximal_entry_depth_mm,
        )
        try:
            outer_body = _loft_from_section_profiles(sections, outer_profiles)
            inner_body = _loft_from_section_profiles(sections, inner_profiles)
            if not _shape_is_valid(outer_body) or not _shape_is_valid(inner_body):
                return None, None
            shell = outer_body.cut(inner_body)
            if not _shape_is_valid(shell):
                return None, None
            return shell, (inner_profiles, outer_profiles)
        except Exception:
            return None, None

    model, profiles = _try_transtibial(transtibial_profile)
    if model is not None and profiles is not None:
        return model, profiles, notes

    if transtibial_profile.get("enabled", True):
        notes.append("Fallback CAD: perfil PTB desactivado por geometría inestable")
        disabled_profile = {**transtibial_profile, "enabled": False}
        model, profiles = _try_transtibial(disabled_profile)
        if model is not None and profiles is not None:
            return model, profiles, notes

    notes.append("Fallback CAD: cáscara uniforme sin flare proximal")
    uniform_wall = max(wall_proximal_mm, wall_distal_mm)
    try:
        model = loft_shell_from_agent(sections, inner_offsets, uniform_wall)
        if not _shape_is_valid(model):
            raise ValueError("Null TopoDS_Shape object")
        empty_profiles: tuple[list, list] = ([], [])
        return model, empty_profiles, notes
    except Exception as exc:
        raise ValueError(
            f"CadQuery no pudo generar el sólido del socket: {exc}"
        ) from exc


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


def offset_points_2d(
    points: list[tuple[float, float]], distance_mm: float, max_retries: int = 4
) -> list[tuple[float, float]]:
    """Offset radial 2D desde una polilínea ya cerrada o abierta."""
    contour = [[float(x), float(y)] for x, y in points]
    return offset_contour_2d(contour, distance_mm, max_retries=max_retries)


def _contour_centroid(points: list[tuple[float, float]]) -> tuple[float, float]:
    arr = np.array(points, dtype=np.float64)
    if arr.shape[0] < 3:
        return float(arr[:, 0].mean()), float(arr[:, 1].mean())
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


def _smoothstep(t: float) -> float:
    t = float(np.clip(t, 0.0, 1.0))
    return t * t * (3.0 - 2.0 * t)


def interpolate_scalar_at_z(
    z_mm: float,
    z_min: float,
    z_max: float,
    value_at_distal: float,
    value_at_proximal: float,
) -> float:
    """Interpola linealmente entre distal (z_min) y proximal (z_max)."""
    span = float(z_max) - float(z_min)
    if span <= 1e-9:
        return float(value_at_proximal)
    t = (float(z_mm) - float(z_min)) / span
    t = float(np.clip(t, 0.0, 1.0))
    return float(value_at_distal) + t * (float(value_at_proximal) - float(value_at_distal))


def _angular_profile_delta_mm(
    angle_deg: float,
    z_frac: float,
    *,
    patellar_bar_depth_mm: float,
    posterior_relief_mm: float,
    lateral_flare_mm: float,
) -> float:
    """
    Deformación angular del contorno interior (mm, positivo = más holgura hacia afuera).
    Perfil PTB simplificado: barra rotuliana anterior, alivio posterior, flare lateral proximal.
    """
    angle = float(angle_deg) % 360.0
    delta = 0.0

    if 0.30 <= z_frac <= 0.82:
        anterior_span = min(abs(angle - 0.0), abs(angle - 360.0))
        anterior_weight = max(0.0, math.cos(math.radians(anterior_span))) ** 2
        if anterior_weight > 0.35:
            delta -= patellar_bar_depth_mm * anterior_weight

        post_span = min(abs(angle - 180.0), 360.0 - abs(angle - 180.0))
        if post_span <= 45.0:
            post_weight = math.cos(math.radians(post_span)) ** 2
            delta += posterior_relief_mm * post_weight

        medial_span = min(abs(angle - 90.0), 360.0 - abs(angle - 90.0))
        medial_weight = max(0.0, math.cos(math.radians(medial_span))) ** 2
        if medial_weight > 0.45 and z_frac < 0.65:
            delta += 0.35 * posterior_relief_mm * medial_weight

    if z_frac >= 0.68:
        lateral_targets = (250.0, 290.0)
        for target in lateral_targets:
            span = abs(((angle - target + 180.0) % 360.0) - 180.0)
            if span <= 35.0:
                lat_weight = _smoothstep((z_frac - 0.68) / 0.32) * math.cos(math.radians(span)) ** 2
                delta += lateral_flare_mm * lat_weight

    return delta


def apply_transtibial_angular_deform(
    points: list[tuple[float, float]],
    z_frac: float,
    profile: dict[str, Any],
) -> list[tuple[float, float]]:
    """Aplica perfil transtibial PTB al contorno interior por sector angular."""
    if not profile.get("enabled", True):
        return points

    cx, cy = _contour_centroid(points)
    patellar = float(profile.get("patellar_bar_depth_mm", DEFAULT_PATELLAR_BAR_DEPTH_MM))
    posterior = float(profile.get("posterior_relief_mm", DEFAULT_POSTERIOR_RELIEF_MM))
    lateral_flare = float(profile.get("lateral_flare_mm", profile.get("proximal_lateral_flare_mm", 2.5)))

    deformed: list[tuple[float, float]] = []
    for x, y in points:
        dx, dy = float(x) - cx, float(y) - cy
        radius = math.hypot(dx, dy)
        if radius < 1e-9:
            deformed.append((float(x), float(y)))
            continue
        angle_deg = math.degrees(math.atan2(dy, dx)) % 360.0
        delta = _angular_profile_delta_mm(
            angle_deg,
            z_frac,
            patellar_bar_depth_mm=patellar,
            posterior_relief_mm=posterior,
            lateral_flare_mm=lateral_flare,
        )
        scale = max(radius + delta, radius * 0.85) / radius
        deformed.append((cx + dx * scale, cy + dy * scale))
    return deformed


def compute_proximal_envelope_per_section(
    sections: list[dict[str, Any]],
    *,
    flare_mm: float,
    flare_height_fraction: float,
    collar_height_mm: float,
    collar_extra_wall_mm: float,
) -> tuple[list[float], list[float]]:
    """
    Calcula ensanchamiento exterior y refuerzo de pared en zona proximal (adaptador/prótesis).
    Devuelve (outer_flare_mm, extra_wall_mm) por sección.
    """
    z_min = float(sections[0]["z_mm"])
    z_max = float(sections[-1]["z_mm"])
    span = max(z_max - z_min, 1e-6)
    flare_z_start = z_max - span * float(np.clip(flare_height_fraction, 0.05, 0.45))
    collar_z_start = z_max - float(max(collar_height_mm, 0.0))

    outer_flares: list[float] = []
    extra_walls: list[float] = []
    for sec in sections:
        z = float(sec["z_mm"])
        if z < flare_z_start - 1e-6:
            outer_flares.append(0.0)
            extra_walls.append(0.0)
            continue

        flare_t = _smoothstep((z - flare_z_start) / max(z_max - flare_z_start, 1e-6))
        collar_t = 0.0
        if z >= collar_z_start - 1e-6:
            collar_t = _smoothstep((z - collar_z_start) / max(z_max - collar_z_start, 1e-6))

        outer_flares.append(round(flare_mm * flare_t, 4))
        extra_walls.append(round(collar_extra_wall_mm * max(flare_t, collar_t), 4))

    return outer_flares, extra_walls


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


def _build_section_profile_points(
    sec: dict[str, Any],
    offset_mm: float,
    *,
    z_frac: float,
    transtibial_profile: dict[str, Any] | None,
    apply_profile: bool,
) -> list[tuple[float, float]]:
    pts = offset_contour_2d(sec["contour"], float(offset_mm))
    if apply_profile and transtibial_profile:
        pts = apply_transtibial_angular_deform(pts, z_frac, transtibial_profile)
    return resample_contour(pts, LOFT_CONTOUR_POINTS)


def _loft_from_section_profiles(
    sections: list[dict[str, Any]],
    profile_points_per_section: list[list[tuple[float, float]]],
) -> cq.Workplane:
    if len(sections) != len(profile_points_per_section):
        raise ValueError("profile_points_per_section debe alinearse con sections")

    z_base = float(sections[0]["z_mm"])
    loft_wp: cq.Workplane | None = None
    prev_z = 0.0

    for sec, pts in zip(sections, profile_points_per_section):
        z = float(sec["z_mm"]) - z_base
        delta = z - prev_z
        prev_z = z

        if loft_wp is None:
            loft_wp = cq.Workplane("XY").polyline(pts).close()
        else:
            loft_wp = loft_wp.workplane(offset=delta).polyline(pts).close()

    if loft_wp is None:
        raise ValueError("No hay perfiles para loft")
    return loft_wp.loft(combine=True)


def _loft_from_sections_per_offset(
    sections: list[dict[str, Any]],
    offsets_per_section: list[float],
) -> cq.Workplane:
    """Loft con offset radial distinto por sección (legacy / compat)."""
    if len(sections) != len(offsets_per_section):
        raise ValueError("offsets_per_section debe alinearse con sections")

    z_min = float(sections[0]["z_mm"])
    z_max = float(sections[-1]["z_mm"])
    span = max(z_max - z_min, 1e-6)
    profiles = [
        _build_section_profile_points(
            sec,
            offset_mm,
            z_frac=(float(sec["z_mm"]) - z_min) / span,
            transtibial_profile=None,
            apply_profile=False,
        )
        for sec, offset_mm in zip(sections, offsets_per_section)
    ]
    return _loft_from_section_profiles(sections, profiles)


def build_transtibial_socket_profiles(
    sections: list[dict[str, Any]],
    inner_offsets: list[float],
    *,
    wall_proximal_mm: float,
    wall_distal_mm: float,
    outer_flare_per_section: list[float],
    extra_wall_per_section: list[float],
    transtibial_profile: dict[str, Any] | None,
    neck_transition_fraction: float = DEFAULT_PROSTHESIS_NECK_FRACTION,
    neck_wall_mm: float = DEFAULT_PROSTHESIS_NECK_WALL_MM,
    proximal_entry_depth_mm: float = DEFAULT_PROXIMAL_ENTRY_DEPTH_MM,
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    if len(sections) != len(inner_offsets):
        raise ValueError("inner_offsets debe alinearse con sections")

    z_min = float(sections[0]["z_mm"])
    z_max = float(sections[-1]["z_mm"])
    span = max(z_max - z_min, 1e-6)

    inner_profiles: list[list[tuple[float, float]]] = []
    outer_profiles: list[list[tuple[float, float]]] = []

    for sec, inner_off, flare_mm, extra_wall in zip(
        sections, inner_offsets, outer_flare_per_section, extra_wall_per_section
    ):
        z_frac = (float(sec["z_mm"]) - z_min) / span
        wall_mm = interpolate_scalar_at_z(
            float(sec["z_mm"]), z_min, z_max, wall_distal_mm, wall_proximal_mm
        )
        total_wall = max(wall_mm + float(extra_wall), MIN_SHELL_WALL_MM + float(extra_wall))

        z_mm = float(sec["z_mm"])
        in_entry = _is_proximal_entry_section(z_mm, z_min, z_max, proximal_entry_depth_mm)
        profile_enabled = bool(transtibial_profile and transtibial_profile.get("enabled", True))
        apply_profile = profile_enabled and not in_entry
        inner_pts = _build_section_profile_points(
            sec,
            inner_off,
            z_frac=z_frac,
            transtibial_profile=transtibial_profile,
            apply_profile=apply_profile,
        )
        outer_pts = offset_points_2d(inner_pts, total_wall + float(flare_mm))
        outer_pts = _ensure_outer_encloses_inner(inner_pts, outer_pts, total_wall)
        if z_frac < neck_transition_fraction and z_frac > 1e-6 and not in_entry:
            neck_blend = 1.0 - z_frac / max(neck_transition_fraction, 1e-6)
            outer_pts = _blend_outer_toward_neck(
                outer_pts,
                inner_pts,
                blend=neck_blend,
                neck_wall_mm=neck_wall_mm,
            )
        outer_pts = resample_contour(outer_pts, LOFT_CONTOUR_POINTS)

        inner_profiles.append(inner_pts)
        outer_profiles.append(outer_pts)

    return inner_profiles, outer_profiles


def loft_transtibial_socket_shell(
    sections: list[dict[str, Any]],
    inner_offsets: list[float],
    *,
    wall_proximal_mm: float,
    wall_distal_mm: float,
    outer_flare_per_section: list[float],
    extra_wall_per_section: list[float],
    transtibial_profile: dict[str, Any] | None,
) -> cq.Workplane:
    """Cáscara con pared variable, perfil PTB interior y ensanchamiento proximal exterior."""
    inner_profiles, outer_profiles = build_transtibial_socket_profiles(
        sections,
        inner_offsets,
        wall_proximal_mm=wall_proximal_mm,
        wall_distal_mm=wall_distal_mm,
        outer_flare_per_section=outer_flare_per_section,
        extra_wall_per_section=extra_wall_per_section,
        transtibial_profile=transtibial_profile,
    )
    outer_body = _loft_from_section_profiles(sections, outer_profiles)
    inner_body = _loft_from_section_profiles(sections, inner_profiles)
    return outer_body.cut(inner_body)


def loft_shell_from_agent(
    sections: list[dict[str, Any]],
    inner_offsets: list[float],
    wall_thickness_mm: float,
) -> cq.Workplane:
    """Compatibilidad: pared uniforme sin perfil transtibial."""
    outer_offsets = [o + wall_thickness_mm for o in inner_offsets]
    outer_body = _loft_from_sections_per_offset(sections, outer_offsets)
    inner_body = _loft_from_sections_per_offset(sections, inner_offsets)
    return outer_body.cut(inner_body)


def _scale_contour_2d(
    points: list[tuple[float, float]], scale: float
) -> list[tuple[float, float]]:
    cx, cy = _contour_centroid(points)
    return [((x - cx) * scale + cx, (y - cy) * scale + cy) for x, y in points]


def _last_section_index_below_z(
    sections: list[dict[str, Any]], z_limit_mm: float
) -> int:
    last_i = 0
    for i, sec in enumerate(sections):
        if float(sec["z_mm"]) <= z_limit_mm + 1e-6:
            last_i = i
    return last_i


def _loft_from_profiles_at_z(
    profile_z_pairs: list[tuple[float, list[tuple[float, float]]]],
) -> cq.Workplane | None:
    """Loft sólido siguiendo contornos en distintos planos Z (superficies no planas)."""
    if len(profile_z_pairs) < 2:
        return None
    ordered = sorted(profile_z_pairs, key=lambda p: p[0])
    loft_wp: cq.Workplane | None = None
    prev_z = float(ordered[0][0])
    for z, pts in ordered:
        if len(pts) < 3:
            continue
        delta = float(z) - prev_z
        if loft_wp is None:
            loft_wp = cq.Workplane("XY").workplane(offset=float(z)).polyline(pts).close()
        else:
            loft_wp = loft_wp.workplane(offset=delta).polyline(pts).close()
        prev_z = float(z)
    if loft_wp is None:
        return None
    return loft_wp.loft(combine=True)


def _loft_annular_between_profiles(
    sections: list[dict[str, Any]],
    inner_profiles: list[list[tuple[float, float]]],
    outer_profiles: list[list[tuple[float, float]]],
    start_i: int,
    end_i: int,
) -> cq.Workplane | None:
    if end_i <= start_i:
        return None
    sub_secs = sections[start_i : end_i + 1]
    sub_outer = outer_profiles[start_i : end_i + 1]
    sub_inner = inner_profiles[start_i : end_i + 1]
    if len(sub_secs) < 2:
        return None
    try:
        outer_body = _loft_from_section_profiles(sub_secs, sub_outer)
        inner_body = _loft_from_section_profiles(sub_secs, sub_inner)
        annulus = outer_body.cut(inner_body)
        return annulus if _shape_is_valid(annulus) else None
    except Exception:
        return None


def resolve_proximal_entry_depth_mm(
    sections: list[dict[str, Any]],
    geometry: dict[str, Any] | None = None,
) -> float:
    """
    Profundidad de encaje al muñón desde la boca proximal (z_max).
    Prioriza ~10–12 mm; si hay rodilla cerca, no invade esa zona.
    """
    depth = float(DEFAULT_PROXIMAL_ENTRY_DEPTH_MM)
    if not sections:
        return depth
    z_min = float(sections[0]["z_mm"])
    z_max = float(sections[-1]["z_mm"])
    span = max(z_max - z_min, 1e-6)
    knee = (geometry or {}).get("knee_landmark") or {}
    knee_z = knee.get("suggested_trim_height_mm") or knee.get("area_break_z_mm")
    if knee_z is not None:
        margin = max(span * 0.08, 15.0)
        max_depth = float(knee_z) - z_min - margin
        if max_depth > 6.0:
            depth = min(depth, max_depth)
    return float(np.clip(depth, 8.0, MAX_PROXIMAL_ENTRY_DEPTH_MM))


def _is_proximal_entry_section(
    z_mm: float, z_min: float, z_max: float, entry_depth_mm: float
) -> bool:
    return float(z_mm) >= float(z_max) - float(entry_depth_mm) - 1e-6


def _index_distal_anchor_section(sections: list[dict[str, Any]]) -> int:
    """Cuello distal (prótesis): siempre en el extremo z_min (primeras secciones)."""
    if not sections:
        return 0
    n = min(DISTAL_ANCHOR_MAX_SECTIONS, len(sections))
    best_i = 0
    best_a = float("inf")
    for i in range(n):
        area = float(sections[i].get("area_mm2", 0) or 0)
        if area > 0 and area < best_a:
            best_a = area
            best_i = i
    return best_i


def _index_min_area_section(sections: list[dict[str, Any]]) -> int:
    """Índice de la sección con menor área (cuello distal / encaje prótesis)."""
    return _index_distal_anchor_section(sections)


def _ensure_outer_encloses_inner(
    inner_pts: list[tuple[float, float]],
    outer_pts: list[tuple[float, float]],
    min_gap_mm: float,
) -> list[tuple[float, float]]:
    """Evita contornos cruzados (outer más pequeño que inner) que generan mallas mezcladas."""
    r_inner = _mean_contour_radius(inner_pts)
    r_outer = _mean_contour_radius(outer_pts)
    need = r_inner + max(min_gap_mm, MIN_SHELL_WALL_MM * 0.5)
    if r_outer >= need:
        return outer_pts
    cx, cy = _contour_centroid(inner_pts)
    scale = need / max(r_outer, 1e-6)
    return [
        (cx + (x - cx) * scale, cy + (y - cy) * scale)
        for x, y in outer_pts
    ]


def open_proximal_end(
    model: cq.Workplane,
    sections: list[dict[str, Any]],
    *,
    clearance_mm: float = 1.0,
) -> cq.Workplane:
    """Boca proximal (+Z) abierta: elimina cualquier tapa por encima de la última sección."""
    if not sections or not _shape_is_valid(model):
        return model
    try:
        z_base = float(sections[0]["z_mm"])
        z_top = float(sections[-1]["z_mm"]) - z_base
        span_xy = 800.0
        cutter = (
            cq.Workplane("XY")
            .workplane(offset=z_top + clearance_mm)
            .box(span_xy, span_xy, 500.0, centered=(True, True, False))
        )
        opened = model.cut(cutter)
        return opened if _shape_is_valid(opened) else model
    except Exception:
        return model


def _mean_contour_radius(points: list[tuple[float, float]]) -> float:
    cx, cy = _contour_centroid(points)
    radii = [math.hypot(x - cx, y - cy) for x, y in points]
    return float(np.mean(radii)) if radii else 0.0


def _circle_contour_points(
    cx: float, cy: float, radius: float, *, n: int = LOFT_CONTOUR_POINTS
) -> list[tuple[float, float]]:
    if radius <= 1e-6:
        radius = 1.0
    return [
        (cx + radius * math.cos(2.0 * math.pi * i / n), cy + radius * math.sin(2.0 * math.pi * i / n))
        for i in range(n)
    ]


def zero_proximal_envelope(n_sections: int) -> tuple[list[float], list[float]]:
    """Encaje proximal simple: sin flare/collar ni refuerzo sólido en la boca."""
    return [0.0] * n_sections, [0.0] * n_sections


def _blend_outer_toward_neck(
    outer_pts: list[tuple[float, float]],
    inner_pts: list[tuple[float, float]],
    *,
    blend: float,
    neck_wall_mm: float,
) -> list[tuple[float, float]]:
    """Suaviza el contorno exterior hacia un cuello circular (interfaz prótesis, z distal)."""
    blend = float(np.clip(blend, 0.0, 1.0))
    if blend <= 1e-6:
        return outer_pts
    cx, cy = _contour_centroid(inner_pts)
    r_inner = _mean_contour_radius(inner_pts)
    r_outer = _mean_contour_radius(outer_pts)
    r_target = max(r_inner + neck_wall_mm, r_outer * 0.88)
    blended: list[tuple[float, float]] = []
    for x, y in outer_pts:
        angle = math.atan2(y - cy, x - cx)
        tx = cx + r_target * math.cos(angle)
        ty = cy + r_target * math.sin(angle)
        blended.append(((1.0 - blend) * x + blend * tx, (1.0 - blend) * y + blend * ty))
    return resample_contour(blended, LOFT_CONTOUR_POINTS)


def add_prosthesis_adapter_solid(
    model: cq.Workplane,
    sections: list[dict[str, Any]],
    inner_profiles: list[list[tuple[float, float]]],
    outer_profiles: list[list[tuple[float, float]]],
    *,
    solid_height_mm: float,
    cap_ring_mm: float,
    core_fill_fraction: float = DEFAULT_DISTAL_CORE_FILL_FRACTION,
    core_max_z_fraction: float = DEFAULT_DISTAL_CORE_MAX_Z_FRACTION,
    adapter_diameter_mm: float = DEFAULT_PROSTHESIS_ADAPTER_DIAMETER_MM,
    adapter_plate_mm: float = DEFAULT_PROSTHESIS_ADAPTER_PLATE_MM,
) -> cq.Workplane:
    """
    Cierra el extremo distal (menor área) con sólido para acople de prótesis.

    - Tapa anular (+Z) en el cuello: une pared interior y exterior.
    - Plug hacia -Z: transición anatómica → cuello circular estándar de prótesis.
    - Placa circular inferior: superficie plana de encaje con adaptador piramidal.
    """
    del core_fill_fraction, core_max_z_fraction
    if solid_height_mm <= 0 or not inner_profiles or not outer_profiles or not sections:
        return model
    if len(sections) != len(inner_profiles):
        return model

    dist_idx = _index_min_area_section(sections)
    inner_pts = inner_profiles[dist_idx]
    outer_pts = outer_profiles[dist_idx]
    if len(inner_pts) < 3 or len(outer_pts) < 3:
        return model

    try:
        z_base = float(sections[0]["z_mm"])
        z_anchor = float(sections[dist_idx]["z_mm"]) - z_base
        result = model
        inner_wp = cq.Workplane("XY").workplane(offset=z_anchor).polyline(inner_pts).close()
        outer_wp = cq.Workplane("XY").workplane(offset=z_anchor).polyline(outer_pts).close()

        if cap_ring_mm > 0:
            # Hacia -Z (distal): no rellenar la cavidad hacia proximal (+Z)
            seal = outer_wp.cut(inner_wp).extrude(-abs(cap_ring_mm))
            if _shape_is_valid(seal):
                result = result.union(seal)

        cx, cy = _contour_centroid(inner_pts)
        r_neck = _mean_contour_radius(inner_pts)
        r_adapter = max(float(adapter_diameter_mm) / 2.0, r_neck * 0.92)

        plug_pairs: list[tuple[float, list[tuple[float, float]]]] = []
        tiers = max(4, DEFAULT_PLUG_TAPER_TIERS)
        for k in range(tiers):
            t = k / max(tiers - 1, 1)
            z_off = z_anchor - solid_height_mm * t
            if t < 0.45:
                scale = 1.0 - 0.10 * t
                pts = resample_contour(_scale_contour_2d(inner_pts, scale), LOFT_CONTOUR_POINTS)
            else:
                blend = (t - 0.45) / 0.55
                anatomical = resample_contour(
                    _scale_contour_2d(inner_pts, max(0.78, 1.0 - 0.12 * t)), LOFT_CONTOUR_POINTS
                )
                circular = _circle_contour_points(cx, cy, r_adapter * (1.0 - 0.06 * t))
                pts = [
                    (
                        (1.0 - blend) * ax + blend * bx,
                        (1.0 - blend) * ay + blend * by,
                    )
                    for (ax, ay), (bx, by) in zip(anatomical, circular)
                ]
            plug_pairs.append((z_off, pts))

        plug = _loft_from_profiles_at_z(plug_pairs)
        if plug is not None and _shape_is_valid(plug):
            result = result.union(plug)

        plate_h = max(float(adapter_plate_mm), 2.0)
        z_plate = z_anchor - solid_height_mm - plate_h
        plate = (
            cq.Workplane("XY")
            .workplane(offset=z_plate)
            .circle(r_adapter)
            .extrude(plate_h)
        )
        if _shape_is_valid(plate):
            result = result.union(plate)

        return result
    except Exception:
        return model


def _resolve_transtibial_profile(
    structure: dict[str, Any],
    design_parameters: dict[str, Any],
) -> dict[str, Any]:
    profile = dict(structure.get("transtibial_profile") or design_parameters.get("transtibial_profile") or {})
    adapter = structure.get("proximal_adapter") or design_parameters.get("proximal_adapter") or {}
    defaults = {
        "enabled": True,
        "patellar_bar_depth_mm": DEFAULT_PATELLAR_BAR_DEPTH_MM,
        "posterior_relief_mm": DEFAULT_POSTERIOR_RELIEF_MM,
        "lateral_flare_mm": 2.5,
    }
    defaults.update(profile)
    defaults["proximal_adapter"] = {
        "enabled": False,
        "flare_mm": 0.0,
        "flare_height_fraction": 0.0,
        "collar_height_mm": 0.0,
        "collar_extra_wall_mm": 0.0,
    }
    return defaults


def _resolve_prosthesis_adapter(
    structure: dict[str, Any], design_parameters: dict[str, Any]
) -> dict[str, Any]:
    """Adaptador sólido en extremo distal (z=0, encaje con prótesis)."""
    adapter = dict(
        structure.get("prosthesis_adapter")
        or design_parameters.get("prosthesis_adapter")
        or structure.get("distal_closure")
        or design_parameters.get("distal_closure")
        or {}
    )
    return {
        "enabled": bool(adapter.get("enabled", True)),
        "solid_height_mm": float(
            adapter.get("solid_height_mm", DEFAULT_PROSTHESIS_ADAPTER_HEIGHT_MM)
        ),
        "cap_ring_mm": float(
            adapter.get("cap_ring_mm", adapter.get("cap_thickness_mm", DEFAULT_DISTAL_CAP_MM))
        ),
        "core_fill_fraction": float(
            adapter.get("core_fill_fraction", DEFAULT_DISTAL_CORE_FILL_FRACTION)
        ),
        "core_max_z_fraction": float(
            adapter.get("core_max_z_fraction", DEFAULT_DISTAL_CORE_MAX_Z_FRACTION)
        ),
        "neck_transition_fraction": float(
            adapter.get("neck_transition_fraction", DEFAULT_PROSTHESIS_NECK_FRACTION)
        ),
        "neck_wall_mm": float(adapter.get("neck_wall_mm", DEFAULT_PROSTHESIS_NECK_WALL_MM)),
        "adapter_diameter_mm": float(
            adapter.get("adapter_diameter_mm", DEFAULT_PROSTHESIS_ADAPTER_DIAMETER_MM)
        ),
        "adapter_plate_mm": float(
            adapter.get("adapter_plate_mm", DEFAULT_PROSTHESIS_ADAPTER_PLATE_MM)
        ),
    }


def _resolve_distal_closure(structure: dict[str, Any], design_parameters: dict[str, Any]) -> dict[str, Any]:
    closure = dict(structure.get("distal_closure") or design_parameters.get("distal_closure") or {})
    return {
        "enabled": bool(closure.get("enabled", True)),
        "cap_thickness_mm": float(closure.get("cap_thickness_mm", DEFAULT_DISTAL_CAP_MM)),
    }


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
    wall_dist = float(wall.get("distal", DEFAULT_WALL_THICKNESS_DISTAL_MM))

    design_parameters = agent.get("design_parameters") or {}
    transtibial_profile = _resolve_transtibial_profile(structure, design_parameters)
    prosthesis_adapter = _resolve_prosthesis_adapter(structure, design_parameters)
    adapter = transtibial_profile.get("proximal_adapter") or {}

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
    proximal_entry_depth_mm = resolve_proximal_entry_depth_mm(sections, geometry)
    sections = subsample_sections_for_loft(sections)

    base_per_section = [
        interpolate_offset_at_z(float(s["z_mm"]), samples, base_offsets.get("interpolation", "linear"))
        for s in sections
    ]
    inner_offsets, mods_log, mod_notes = apply_local_modifications_to_offsets(
        sections, base_per_section, local_mods, height_mm
    )

    fillet_mm = 0.0 if PROXIMAL_SIMPLE_FIT else float(
        design_parameters.get("proximal_fillet_mm", DEFAULT_PROXIMAL_FILLET_MM)
    )

    if PROXIMAL_SIMPLE_FIT:
        outer_flares, extra_walls = zero_proximal_envelope(len(sections))
    else:
        outer_flares, extra_walls = compute_proximal_envelope_per_section(
            sections,
            flare_mm=float(adapter.get("flare_mm", DEFAULT_PROXIMAL_FLARE_MM)),
            flare_height_fraction=float(
                adapter.get("flare_height_fraction", DEFAULT_PROXIMAL_FLARE_HEIGHT_FRACTION)
            ),
            collar_height_mm=float(adapter.get("collar_height_mm", DEFAULT_ADAPTER_COLLAR_HEIGHT_MM)),
            collar_extra_wall_mm=float(
                adapter.get("collar_extra_wall_mm", DEFAULT_ADAPTER_COLLAR_EXTRA_WALL_MM)
            ),
        )

    inner_profiles, outer_profiles = [], []
    model, (inner_profiles, outer_profiles), cad_fallback_notes = _build_socket_shell(
        sections,
        inner_offsets,
        wall_proximal_mm=wall_prox,
        wall_distal_mm=wall_dist,
        transtibial_profile=transtibial_profile,
        outer_flares=outer_flares,
        extra_walls=extra_walls,
        neck_transition_fraction=prosthesis_adapter["neck_transition_fraction"],
        neck_wall_mm=prosthesis_adapter["neck_wall_mm"],
        proximal_entry_depth_mm=proximal_entry_depth_mm,
    )
    mods_log.extend(cad_fallback_notes)

    if prosthesis_adapter.get("enabled", True) and inner_profiles and outer_profiles:
        model = add_prosthesis_adapter_solid(
            model,
            sections,
            inner_profiles,
            outer_profiles,
            solid_height_mm=float(prosthesis_adapter["solid_height_mm"]),
            cap_ring_mm=float(prosthesis_adapter["cap_ring_mm"]),
            core_fill_fraction=float(
                prosthesis_adapter.get("core_fill_fraction", DEFAULT_DISTAL_CORE_FILL_FRACTION)
            ),
            core_max_z_fraction=float(
                prosthesis_adapter.get("core_max_z_fraction", DEFAULT_DISTAL_CORE_MAX_Z_FRACTION)
            ),
            adapter_diameter_mm=float(prosthesis_adapter.get("adapter_diameter_mm", DEFAULT_PROSTHESIS_ADAPTER_DIAMETER_MM)),
            adapter_plate_mm=float(prosthesis_adapter.get("adapter_plate_mm", DEFAULT_PROSTHESIS_ADAPTER_PLATE_MM)),
        )

    if PROXIMAL_SIMPLE_FIT and sections:
        model = open_proximal_end(model, sections)

    if not PROXIMAL_SIMPLE_FIT:
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

    if not _shape_is_valid(model):
        raise ValueError("Null TopoDS_Shape object")

    out_dir.mkdir(parents=True, exist_ok=True)
    stl_path = out_dir / "socket.stl"
    ply_path = out_dir / "socket.ply"
    step_path = out_dir / "socket.step"
    script_copy = out_dir / "socket_design.cad.py"

    try:
        stl_meta = export_socket_stl(model, stl_path)
    except Exception as exc:
        raise ValueError(f"Export STL fallido: {exc}") from exc

    ply_meta: dict[str, Any] | None = None
    try:
        from app.services.mesh_convert import convert_stl_file_to_ply

        ply_meta = convert_stl_file_to_ply(stl_path, ply_path)
    except Exception as exc:
        warnings.append(f"Export PLY omitido: {exc}")

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
                "mode": "interpolated_distal_to_proximal",
            },
            "proximal_adapter": {
                "mode": "simple_fit" if PROXIMAL_SIMPLE_FIT else "flare_collar",
                "flare_mm": 0.0 if PROXIMAL_SIMPLE_FIT else float(adapter.get("flare_mm", DEFAULT_PROXIMAL_FLARE_MM)),
                "flare_height_fraction": 0.0 if PROXIMAL_SIMPLE_FIT else float(
                    adapter.get("flare_height_fraction", DEFAULT_PROXIMAL_FLARE_HEIGHT_FRACTION)
                ),
                "collar_height_mm": 0.0 if PROXIMAL_SIMPLE_FIT else float(
                    adapter.get("collar_height_mm", DEFAULT_ADAPTER_COLLAR_HEIGHT_MM)
                ),
                "collar_extra_wall_mm": 0.0 if PROXIMAL_SIMPLE_FIT else float(
                    adapter.get("collar_extra_wall_mm", DEFAULT_ADAPTER_COLLAR_EXTRA_WALL_MM)
                ),
                "max_outer_flare_mm": round(max(outer_flares) if outer_flares else 0.0, 3),
                "max_extra_wall_mm": round(max(extra_walls) if extra_walls else 0.0, 3),
            },
            "transtibial_profile": {
                "enabled": bool(transtibial_profile.get("enabled", True)),
                "patellar_bar_depth_mm": float(
                    transtibial_profile.get("patellar_bar_depth_mm", DEFAULT_PATELLAR_BAR_DEPTH_MM)
                ),
                "posterior_relief_mm": float(
                    transtibial_profile.get("posterior_relief_mm", DEFAULT_POSTERIOR_RELIEF_MM)
                ),
            },
            "prosthesis_adapter": prosthesis_adapter,
            "distal_closure": prosthesis_adapter,
            "socket_length_fraction": length_fraction,
            "sections_input": len(geometry.get("sections", [])),
            "sections_loft": len(sections),
            "length_processing": {
                **length_meta,
                "knee_landmark": geometry.get("knee_landmark"),
                "trim_height_mm": round(float(structure.get("trim_height_mm", 0) or 0), 3),
            },
            "ventilation": {
                "enabled": bool(ventilation.get("enabled", False)),
                "pattern": ventilation.get("pattern"),
                "count": ventilation.get("count"),
                "note": "no mesh in MVP" if ventilation.get("enabled") else None,
            },
            "proximal_fillet_mm": fillet_mm,
            "proximal_stump_fit": {
                "mode": "open_contour" if PROXIMAL_SIMPLE_FIT else "standard",
                "entry_depth_mm": round(proximal_entry_depth_mm, 3),
            },
        },
        "geometry_checks": {
            "height_mm_stump": height_mm,
            "n_sections_input": len(geometry.get("sections", [])),
            "n_sections_loft": len(sections),
            "volume_stump_cm3": geometry.get("volume_cm3"),
            "volume_socket_cm3": socket_volume,
            "bbox_mm": bbox,
            "stl_export": stl_meta,
            "ply_export": ply_meta,
            "orientation": {
                "distal_prosthesis": "z_min (-Z en vista estándar Blender)",
                "proximal_stump": "z_max (+Z)",
            },
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
            "ply": str(ply_path.name) if ply_meta else None,
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
    if fillet_mm <= 0 or not _shape_is_valid(model):
        return model
    try:
        bbox = model.val().BoundingBox()
        z_max = bbox.zmax
        result = model.edges(f"|Z| > {z_max - 0.5}").fillet(fillet_mm)
        return result if _shape_is_valid(result) else model
    except Exception:
        return model


def export_socket_stl(model: cq.Workplane, stl_path: Path) -> dict[str, Any]:
    """Exporta STL en mm y valida la escala del archivo (evita ×1000 en visores)."""
    cq.exporters.export(model, str(stl_path))
    meta: dict[str, Any] = {"stl_units": "mm", "path": stl_path.name}
    try:
        import trimesh

        tm = trimesh.load(stl_path)
        extents = [round(float(x), 3) for x in tm.extents.tolist()]
        meta["stl_extent_mm"] = extents
        max_ext = max(extents) if extents else 0.0
        if max_ext > _MAX_STL_EXTENT_MM:
            raise ValueError(
                f"STL con extent {max_ext:.1f} mm (> {_MAX_STL_EXTENT_MM}): "
                "re-analiza el escaneo (unidades) o revisa geometry_analysis."
            )
    except ImportError:
        meta["stl_validation"] = "trimesh no disponible"
    return meta


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
        mode = wt.get("mode", "uniform")
        if mode == "interpolated_distal_to_proximal":
            print(f"Espesor pared: distal {wt.get('distal')} mm → proximal {wt.get('proximal')} mm")
        else:
            print(f"Espesor pared (loft): {wt.get('used_for_loft', wt.get('proximal'))} mm")
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
