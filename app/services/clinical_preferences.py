"""Aplica socket_preferences del clinical_report sobre la respuesta del agente."""

from __future__ import annotations

from typing import Any

_OFFSET_SAMPLE_STEP_MM = 5.0
_LENGTH_PREF_MAP = {"shorter": 0.78, "standard": 0.85, "longer": 0.92}


def _ensure_offset_samples(
    socket: dict[str, Any], height_mm: float, default_offset: float
) -> list[dict[str, float]]:
    base_offsets = socket.setdefault("base_offsets", {})
    samples = list(base_offsets.get("samples") or [])
    if samples:
        return samples
    z = 0.0
    while z <= height_mm + 1e-6:
        samples.append({"z_mm": round(z, 3), "offset_mm": round(default_offset, 3)})
        z += _OFFSET_SAMPLE_STEP_MM
    base_offsets["interpolation"] = base_offsets.get("interpolation", "linear")
    base_offsets["samples"] = samples
    return samples


def apply_clinical_preferences_to_agent(
    agent_payload: dict[str, Any],
    clinical_report: dict[str, Any],
    height_mm: float,
) -> dict[str, Any]:
    """
    Post-proceso fiable: socket_preferences del JSON clínico override al LLM/reglas.
    """
    prefs = clinical_report.get("socket_preferences") or {}
    if not prefs:
        return agent_payload

    socket = agent_payload.get("socket_design")
    if not socket:
        return agent_payload

    design_params = agent_payload.setdefault("design_parameters", {})
    overrides: dict[str, Any] = {}

    extra_holgura = float(prefs.get("extra_holgura_mm", 0) or 0)
    default_offset = float(prefs.get("radial_clearance_mm", 2.5))

    if "radial_clearance_mm" in prefs:
        target = float(prefs["radial_clearance_mm"])
        samples = _ensure_offset_samples(socket, height_mm, target)
        for sample in samples:
            sample["offset_mm"] = round(target + extra_holgura, 3)
        overrides["radial_clearance_mm"] = target
        design_params["radial_clearance_mm"] = target
    elif extra_holgura > 0:
        samples = _ensure_offset_samples(socket, height_mm, default_offset)
        for sample in samples:
            sample["offset_mm"] = round(float(sample["offset_mm"]) + extra_holgura, 3)
        overrides["extra_holgura_mm"] = extra_holgura

    structure = socket.setdefault("structure", {})

    if "socket_length_fraction" in prefs:
        frac = float(prefs["socket_length_fraction"])
        structure["socket_length_fraction"] = frac
        structure["trim_height_mm"] = round(height_mm * frac, 3)
        overrides["socket_length_fraction"] = frac
    elif pref := prefs.get("socket_length_preference"):
        frac = _LENGTH_PREF_MAP.get(str(pref).lower(), 0.85)
        structure["socket_length_fraction"] = frac
        structure["trim_height_mm"] = round(height_mm * frac, 3)
        overrides["socket_length_preference"] = pref
        overrides["socket_length_fraction"] = frac

    if wall := prefs.get("wall_thickness_mm"):
        structure["wall_thickness_mm"] = wall
        overrides["wall_thickness_mm"] = wall

    if prefs.get("ventilation") is True:
        structure["ventilation"] = {
            "enabled": True,
            "pattern": "lateral_slots",
            "count": 4,
        }
        overrides["ventilation"] = True

    clearance = float(design_params.get("radial_clearance_mm", default_offset))
    mods = list(socket.get("local_modifications") or [])

    if prefs.get("distal_relief") is True:
        has_relief = any(m.get("kind") == "relief" for m in mods)
        if not has_relief:
            mods.append(
                {
                    "kind": "relief",
                    "z_min_mm": 0.0,
                    "z_max_mm": round(height_mm * 0.3, 3),
                    "angle_start_deg": 0.0,
                    "angle_end_deg": 360.0,
                    "depth_mm": round(max(1.0, clearance * 0.5), 2),
                    "clinical_reason": "socket_preferences.distal_relief=true",
                }
            )
            overrides["distal_relief"] = True

    if prefs.get("ventilation") is True:
        has_vent = any(m.get("kind") == "ventilation_channel" for m in mods)
        if not has_vent:
            mods.append(
                {
                    "kind": "ventilation_channel",
                    "z_min_mm": round(height_mm * 0.2, 3),
                    "z_max_mm": round(height_mm * 0.85, 3),
                    "angle_start_deg": 80.0,
                    "angle_end_deg": 100.0,
                    "depth_mm": 2.0,
                    "clinical_reason": "socket_preferences.ventilation=true",
                }
            )

    socket["local_modifications"] = mods

    if overrides:
        design_params["clinical_overrides_applied"] = True
        design_params["clinical_overrides"] = overrides

    return agent_payload
