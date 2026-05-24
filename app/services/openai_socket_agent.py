"""
Agente de diseño de socket con OpenAI (prompt en app/prompts/socket_design_system.md).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.schemas.geometry import GeometryResponse
from app.schemas.socket_design import SocketDesignAgentResponse
from app.services.llm_client import chat_json_completion, get_deployment_name, get_llm_client, get_llm_provider
from app.services.socket_design_agent import (
    _build_local_modifications,
    _derive_clearance_mm,
    load_default_clinical_report,
)
from app.services.socket_design_merge import finalize_agent_payload

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "socket_design_system.md"

_LOCAL_MOD_REQUIRED = (
    "kind",
    "z_min_mm",
    "z_max_mm",
    "angle_start_deg",
    "angle_end_deg",
    "depth_mm",
    "clinical_reason",
)
_VALID_KINDS = frozenset({"relief", "ventilation_channel", "pressure_pad", "build_up"})


def _geometry_dict(geometry: GeometryResponse | dict[str, Any]) -> dict[str, Any]:
    if isinstance(geometry, GeometryResponse):
        return geometry.model_dump()
    return geometry


def load_system_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Prompt no encontrado: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def compact_geometry_for_llm(geometry: dict[str, Any]) -> dict[str, Any]:
    """Envía métricas y resumen de secciones sin arrays contour (ahorra tokens)."""
    compact = {k: v for k, v in geometry.items() if k != "sections"}
    compact["sections_summary"] = [
        {
            "z_mm": s.get("z_mm"),
            "area_mm2": s.get("area_mm2"),
            "perimeter_mm": s.get("perimeter_mm"),
            "curvature_score": s.get("curvature_score"),
            "contour_point_count": s.get("contour_point_count"),
        }
        for s in geometry.get("sections", [])
        if int(s.get("contour_point_count", 0) or 0) >= 3
    ]
    return compact


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("La respuesta del modelo no contiene JSON de objeto")
    return json.loads(cleaned[start : end + 1])


def _quality_dict_from_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    qg = geometry.get("quality_gate") or {}
    return {
        "passed": bool(qg.get("passed", False)),
        "demo_eligible": bool(qg.get("demo_eligible", False)),
    }


def _infer_kind(raw: dict[str, Any]) -> str | None:
    for key in ("kind", "type", "modification_type", "name"):
        val = raw.get(key)
        if val is None:
            continue
        text = str(val).lower().strip()
        if text in _VALID_KINDS:
            return text
        if "ventil" in text:
            return "ventilation_channel"
        if "relief" in text or "alivio" in text or "sensitive" in text or "distal" in text:
            return "relief"
        if "pressure" in text or "pad" in text:
            return "pressure_pad"
        if "build" in text:
            return "build_up"
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_local_modification_item(
    raw: dict[str, Any],
    *,
    height_mm: float,
    default_kind: str = "relief",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    kind = _infer_kind(raw) or default_kind
    if kind not in _VALID_KINDS:
        kind = default_kind

    z_min = _float_or_none(raw.get("z_min_mm"))
    if z_min is None:
        z_min = _float_or_none(raw.get("z_min"))
    if z_min is None:
        z_min = 0.0

    z_max = _float_or_none(raw.get("z_max_mm"))
    if z_max is None:
        z_max = _float_or_none(raw.get("z_max"))
    if z_max is None:
        if kind == "relief":
            z_max = height_mm * 0.3
        elif kind == "ventilation_channel":
            z_max = height_mm * 0.85
        else:
            z_max = height_mm * 0.5

    angle_start = _float_or_none(raw.get("angle_start_deg"))
    if angle_start is None:
        angle_start = _float_or_none(raw.get("angle_start"))
    if angle_start is None:
        angle_start = 80.0 if kind == "ventilation_channel" else 0.0

    angle_end = _float_or_none(raw.get("angle_end_deg"))
    if angle_end is None:
        angle_end = _float_or_none(raw.get("angle_end"))
    if angle_end is None:
        angle_end = 100.0 if kind == "ventilation_channel" else 360.0

    depth = _float_or_none(raw.get("depth_mm"))
    if depth is None:
        depth = _float_or_none(raw.get("relief_depth_mm"))
    if depth is None:
        depth = 2.0 if kind == "ventilation_channel" else 1.0

    reason = raw.get("clinical_reason")
    if not reason:
        for alt in ("reason", "description", "notes", "label", "name"):
            if raw.get(alt):
                reason = str(raw[alt])
                break
    if not reason:
        reason = f"modificación {kind} (normalizada desde respuesta LLM)"

    if z_max <= z_min:
        z_max = z_min + max(height_mm * 0.05, 1.0)

    return {
        "kind": kind,
        "z_min_mm": round(z_min, 3),
        "z_max_mm": round(z_max, 3),
        "angle_start_deg": round(angle_start, 3),
        "angle_end_deg": round(angle_end, 3),
        "depth_mm": round(depth, 3),
        "clinical_reason": str(reason),
    }


def _is_valid_local_mod(item: dict[str, Any]) -> bool:
    return all(key in item and item[key] is not None for key in _LOCAL_MOD_REQUIRED)


def _resolve_clearance_mm(
    payload: dict[str, Any],
    geometry: dict[str, Any],
    report: dict[str, Any],
) -> float:
    params = payload.get("design_parameters") or {}
    radial = _float_or_none(params.get("radial_clearance_mm"))
    if radial is not None:
        return radial
    socket = payload.get("socket_design") or {}
    samples = (socket.get("base_offsets") or {}).get("samples") or []
    if samples:
        first = _float_or_none(samples[0].get("offset_mm"))
        if first is not None:
            return first
    return _derive_clearance_mm(geometry, report, _quality_dict_from_geometry(geometry))


def _normalize_socket_design_payload(
    parsed: dict[str, Any],
    geometry: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    socket = parsed.get("socket_design")
    if not isinstance(socket, dict):
        return parsed

    height_mm = float(
        geometry.get("height_mm")
        or (parsed.get("geometry_reference") or {}).get("height_mm")
        or 0.0
    )
    raw_list = socket.get("local_modifications")
    if raw_list is None:
        raw_list = []
    if not isinstance(raw_list, list):
        raw_list = []

    normalized: list[dict[str, Any]] = []
    invalid_count = 0
    for item in raw_list:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        norm = _normalize_local_modification_item(item, height_mm=height_mm)
        if norm is None or not _is_valid_local_mod(norm):
            invalid_count += 1
            continue
        normalized.append({k: norm[k] for k in _LOCAL_MOD_REQUIRED})

    total = len(raw_list) if raw_list else 0
    need_rules = not normalized or (total > 0 and invalid_count / total > 0.5)
    if need_rules:
        clearance = _resolve_clearance_mm(parsed, geometry, report)
        normalized = _build_local_modifications(height_mm, report, clearance)

    socket["local_modifications"] = normalized
    parsed["socket_design"] = socket
    return parsed


def _sync_quality_gate_from_geometry(
    payload: dict[str, Any], geometry: dict[str, Any]
) -> dict[str, Any]:
    """Alinea quality_gate con métricas reales del analyze (el LLM no debe inventar)."""
    qg = geometry.get("quality_gate") or {}
    recon = geometry.get("reconstruction_error") or {}
    agent_qg = payload.setdefault("quality_gate", {})
    agent_qg["mean_error_mm"] = float(recon.get("mean_error_mm", agent_qg.get("mean_error_mm", 0)))
    agent_qg["max_error_mm"] = float(recon.get("max_error_mm", agent_qg.get("max_error_mm", 0)))
    agent_qg["section_similarity"] = float(
        geometry.get("section_similarity", agent_qg.get("section_similarity", 0))
    )
    agent_qg["volume_cm3"] = geometry.get("volume_cm3")
    agent_qg["volume_estimated"] = bool(qg.get("volume_estimated", False))
    agent_qg["passed"] = bool(qg.get("passed", False))
    agent_qg["demo_eligible"] = bool(qg.get("demo_eligible", False))
    if agent_qg["mean_error_mm"] > 2.0:
        agent_qg["passed"] = False
        msgs = list(agent_qg.get("messages", []))
        if not any("mean_error" in m for m in msgs):
            msgs.insert(0, f"mean_error_mm {agent_qg['mean_error_mm']:.2f} > 2.0")
        agent_qg["messages"] = msgs
        payload["socket_design"] = None
    return payload


def _sync_geometry_reference(payload: dict[str, Any], geometry: dict[str, Any]) -> dict[str, Any]:
    sections = geometry.get("sections") or []
    n_valid = sum(1 for s in sections if int(s.get("contour_point_count", 0) or 0) >= 3)
    payload["geometry_reference"] = {
        "height_mm": round(float(geometry.get("height_mm", 0)), 3),
        "section_count": n_valid,
        "coordinate_system": {
            "z_origin": "distal",
            "z_direction": "proximal",
            "units": "mm",
            "angular_note": "0° = +X, 90° = +Y; medial/lateral aproximado",
        },
    }
    return payload


def _format_llm_api_error(deployment: str, exc: Exception) -> ValueError:
    message = str(exc)
    if "DeploymentNotFound" in message:
        return ValueError(
            f"El deployment Azure '{deployment}' no existe en tu recurso. "
            "Revisa AZURE_OPENAI_DEPLOYMENT en .env."
        )
    return ValueError(f"Error al llamar al LLM (deployment={deployment}): {exc}")


def run_openai_socket_agent(
    geometry: GeometryResponse | dict[str, Any],
    clinical_report: dict[str, Any] | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    geo = _geometry_dict(geometry)
    report = clinical_report if clinical_report is not None else load_default_clinical_report()
    deployment = get_deployment_name(model)
    provider = get_llm_provider()

    user_payload = {
        "geometry_analysis": compact_geometry_for_llm(geo),
        "clinical_report": report,
    }
    user_message = json.dumps(user_payload, ensure_ascii=False)

    client = get_llm_client()
    system_prompt = load_system_prompt()
    try:
        raw = chat_json_completion(
            client,
            deployment=deployment,
            system_prompt=system_prompt,
            user_content=f"Genera el JSON de diseño de socket para estos datos:\n\n{user_message}",
        )
    except Exception as exc:
        raise _format_llm_api_error(deployment, exc) from exc

    try:
        parsed = _extract_json_object(raw)
        parsed = _sync_quality_gate_from_geometry(parsed, geo)
        parsed = _sync_geometry_reference(parsed, geo)
        parsed = _normalize_socket_design_payload(parsed, geo, report)
        parsed.setdefault("design_parameters", {})
        parsed["design_parameters"]["agent_engine"] = f"{provider}_openai"
        parsed["design_parameters"]["llm_deployment"] = deployment
        parsed["design_parameters"]["llm_provider"] = provider
        return finalize_agent_payload(parsed, geo, report)
    except Exception as exc:
        raise ValueError(f"OpenAI devolvió JSON inválido: {exc}") from exc
