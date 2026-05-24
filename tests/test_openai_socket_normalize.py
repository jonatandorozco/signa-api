"""Tests de normalización local_modifications (OpenAI → Pydantic)."""

import json
from pathlib import Path

import pytest

from app.services.openai_socket_agent import (
    _LOCAL_MOD_REQUIRED,
    _normalize_local_modification_item,
    _normalize_socket_design_payload,
)
from app.services.socket_design_agent import load_default_clinical_report

EXAMPLE_GEOMETRY_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "data" / "geometry_analysis_example.json"
)


@pytest.fixture
def example_geometry() -> dict:
    with EXAMPLE_GEOMETRY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def clinical_report() -> dict:
    return load_default_clinical_report()


def test_normalize_alias_fields_to_valid_schema(example_geometry, clinical_report):
    height = float(example_geometry["height_mm"])
    raw = {
        "name": "distal_sensitivity",
        "description": "relief for distal pain",
        "z_min": 0,
        "z_max": height * 0.25,
        "relief_depth_mm": 1.2,
    }
    norm = _normalize_local_modification_item(raw, height_mm=height)
    assert norm is not None
    assert norm["kind"] == "relief"
    assert norm["z_min_mm"] == 0.0
    assert norm["clinical_reason"]
    assert all(k in norm for k in _LOCAL_MOD_REQUIRED)


def test_valid_local_mod_unchanged_by_item_normalizer():
    item = {
        "kind": "ventilation_channel",
        "z_min_mm": 40.0,
        "z_max_mm": 180.0,
        "angle_start_deg": 80.0,
        "angle_end_deg": 100.0,
        "depth_mm": 2.0,
        "clinical_reason": "clima caluroso",
    }
    norm = _normalize_local_modification_item(item, height_mm=220.0)
    assert norm == item


def test_payload_replaces_invalid_llm_modifications_with_rules(
    example_geometry, clinical_report
):
    parsed = {
        "quality_gate": example_geometry["quality_gate"],
        "geometry_reference": {"height_mm": 220.0, "section_count": 40},
        "socket_design": {
            "type": "transtibial_custom_socket",
            "cad_strategy": {
                "inner_surface": "loft_from_sections",
                "section_source_field": "geometry_analysis.sections",
                "offset_mode": "normal_2d",
                "outer_surface": "offset_wall",
            },
            "base_offsets": {"interpolation": "linear", "samples": [{"z_mm": 0, "offset_mm": 2.5}]},
            "local_modifications": [
                {"name": "distal_sensitivity", "description": "pain"},
                {"label": "vent", "type": "ventilation"},
            ],
            "structure": {
                "wall_thickness_mm": {"proximal": 4.0, "distal": 6.0},
                "trim_height_mm": 187.0,
                "socket_length_fraction": 0.85,
                "ventilation": {"enabled": True, "pattern": "lateral_slots", "count": 4},
            },
            "recommended_material": "TPU semi-rigid",
            "fit_confidence": 0.8,
        },
        "clinical_reasoning": {
            "pain_consideration": "x",
            "activity_adaptation": "y",
            "skin_safety_notes": "z",
            "contraindications": [],
        },
        "cadquery_handoff": {
            "steps": ["loft"],
            "target_fit_tolerance_mm": {"min": 1.0, "max": 2.0},
            "design_mode": "production",
        },
        "design_parameters": {},
    }
    out = _normalize_socket_design_payload(parsed, example_geometry, clinical_report)
    mods = out["socket_design"]["local_modifications"]
    assert len(mods) >= 1
    for m in mods:
        assert _is_complete(m)


def test_payload_keeps_valid_llm_modifications(example_geometry, clinical_report):
    valid_mod = {
        "kind": "relief",
        "z_min_mm": 0.0,
        "z_max_mm": 66.0,
        "angle_start_deg": 0.0,
        "angle_end_deg": 360.0,
        "depth_mm": 1.5,
        "clinical_reason": "distal",
    }
    parsed = {
        "socket_design": {
            "local_modifications": [valid_mod],
            "structure": {
                "wall_thickness_mm": {"proximal": 4, "distal": 6},
                "trim_height_mm": 187,
                "socket_length_fraction": 0.85,
                "ventilation": {"enabled": True, "pattern": "lateral_slots", "count": 4},
            },
        },
        "design_parameters": {"radial_clearance_mm": 2.0},
    }
    out = _normalize_socket_design_payload(parsed, example_geometry, clinical_report)
    mods = out["socket_design"]["local_modifications"]
    assert len(mods) == 1
    assert mods[0]["kind"] == "relief"
    assert mods[0]["z_max_mm"] == 66.0


def _is_complete(m: dict) -> bool:
    return all(k in m for k in _LOCAL_MOD_REQUIRED)
