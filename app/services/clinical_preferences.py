"""Aplica socket_preferences del clinical_report sobre la respuesta del agente."""

from __future__ import annotations

from typing import Any

from app.services.mesh_analyzer import resolve_socket_trim_from_geometry

_OFFSET_SAMPLE_STEP_MM = 5.0
_LENGTH_PREF_MAP = {"shorter": 0.76, "standard": 0.80, "longer": 0.85}
TRANSTIBIAL_MAX_LENGTH_FRACTION = 0.85
TRANSTIBIAL_DEFAULT_LENGTH_FRACTION = 0.80


def is_transtibial_report(clinical_report: dict[str, Any]) -> bool:
    """True si el caso es transtibial (debajo de rodilla)."""
    amp = clinical_report.get("amputation_profile") or {}
    text = " ".join(
        str(amp.get(key, "")).lower()
        for key in ("level_interpreted", "level_reported", "limb")
    )
    return any(
        token in text
        for token in (
            "transtibial",
            "transtib",
            "debajo de la rodilla",
            "below knee",
            "below-knee",
            "bka",
        )
    )


def cap_transtibial_length_fraction(fraction: float, clinical_report: dict[str, Any]) -> float:
    """Evita socket que suba hacia la rodilla (no cubrir muslo/cadera)."""
    if is_transtibial_report(clinical_report):
        return min(float(fraction), TRANSTIBIAL_MAX_LENGTH_FRACTION)
    return float(fraction)


def _clamp_modifications_to_socket_height(
    mods: list[dict[str, Any]], socket_top_z_mm: float
) -> list[dict[str, Any]]:
    """Asegura que z_max_mm no exceda el borde proximal recortado del socket."""
    top = max(socket_top_z_mm, 1.0)
    clamped: list[dict[str, Any]] = []
    for mod in mods:
        item = dict(mod)
        z_min = float(item.get("z_min_mm", 0))
        z_max = float(item.get("z_max_mm", top))
        item["z_min_mm"] = round(min(z_min, top - 1.0), 3)
        item["z_max_mm"] = round(min(max(z_max, item["z_min_mm"] + 1.0), top), 3)
        clamped.append(item)
    return clamped


def _ensure_posterior_knee_relief(
    mods: list[dict[str, Any]], socket_top_z_mm: float
) -> list[dict[str, Any]]:
    """Alivio posterior en tercio proximal del socket (flexión de rodilla)."""
    top = max(socket_top_z_mm, 1.0)
    for mod in mods:
        if mod.get("kind") == "relief" and float(mod.get("angle_start_deg", 0)) <= 160:
            if float(mod.get("angle_end_deg", 0)) >= 200:
                return mods
    mods.append(
        {
            "kind": "relief",
            "z_min_mm": round(top * 0.28, 3),
            "z_max_mm": round(top * 0.88, 3),
            "angle_start_deg": 150.0,
            "angle_end_deg": 210.0,
            "depth_mm": 0.9,
            "clinical_reason": "alivio posterior para flexión de rodilla (transtibial)",
        }
    )
    return mods


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
    geometry: dict[str, Any] | None = None,
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
    geo = geometry or {}

    explicit_frac = float(prefs["socket_length_fraction"]) if "socket_length_fraction" in prefs else None
    explicit_trim = float(prefs["trim_height_mm"]) if prefs.get("trim_height_mm") else None
    if pref := prefs.get("socket_length_preference"):
        if explicit_frac is None:
            explicit_frac = _LENGTH_PREF_MAP.get(str(pref).lower(), TRANSTIBIAL_DEFAULT_LENGTH_FRACTION)
        overrides["socket_length_preference"] = pref

    default_frac = (
        TRANSTIBIAL_DEFAULT_LENGTH_FRACTION
        if is_transtibial_report(clinical_report)
        else 0.85
    )
    use_knee = is_transtibial_report(clinical_report) and explicit_frac is None and explicit_trim is None
    trim_mm, frac, trim_source = resolve_socket_trim_from_geometry(
        geo,
        height_mm,
        default_fraction=default_frac,
        explicit_fraction=explicit_frac,
        explicit_trim_mm=explicit_trim,
        use_knee_detection=use_knee,
    )
    frac = cap_transtibial_length_fraction(frac, clinical_report)
    if frac != trim_mm / max(height_mm, 1.0):
        trim_mm = round(height_mm * frac, 3)
        trim_source = f"{trim_source}+transtibial_cap"

    structure["trim_height_mm"] = round(trim_mm, 3)
    structure["socket_length_fraction"] = round(frac, 4)
    overrides["socket_length_fraction"] = structure["socket_length_fraction"]
    overrides["trim_height_mm"] = structure["trim_height_mm"]
    overrides["trim_source"] = trim_source
    if knee := geo.get("knee_landmark"):
        overrides["knee_landmark"] = knee

    socket_top_z = float(structure["trim_height_mm"])

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
                    "z_max_mm": round(socket_top_z * 0.3, 3),
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
                    "z_min_mm": round(socket_top_z * 0.2, 3),
                    "z_max_mm": round(socket_top_z * 0.82, 3),
                    "angle_start_deg": 80.0,
                    "angle_end_deg": 100.0,
                    "depth_mm": 2.0,
                    "clinical_reason": "socket_preferences.ventilation=true",
                }
            )

    if is_transtibial_report(clinical_report):
        mods = _ensure_posterior_knee_relief(mods, socket_top_z)

    socket["local_modifications"] = _clamp_modifications_to_socket_height(mods, socket_top_z)

    if overrides:
        design_params["clinical_overrides_applied"] = True
        design_params["clinical_overrides"] = overrides

    return agent_payload
