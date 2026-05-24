"""Tests del pipeline CadQuery agent-driven."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_GEOMETRY = ROOT / "app" / "data" / "geometry_analysis_example.json"


def _load_cad_module():
    name = "socket_design_cad_test"
    path = ROOT / "socket_design.cad.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cad():
    return _load_cad_module()


@pytest.fixture
def example_geometry() -> dict:
    with EXAMPLE_GEOMETRY.open(encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def minimal_agent_response(example_geometry) -> dict:
    from app.services.socket_design_agent import run_socket_design_agent

    return run_socket_design_agent(example_geometry)


def test_interpolate_offset_at_z(cad):
    samples = [
        {"z_mm": 0.0, "offset_mm": 2.0},
        {"z_mm": 100.0, "offset_mm": 2.5},
        {"z_mm": 200.0, "offset_mm": 3.0},
    ]
    assert cad.interpolate_offset_at_z(0.0, samples) == 2.0
    assert cad.interpolate_offset_at_z(200.0, samples) == 3.0
    mid = cad.interpolate_offset_at_z(50.0, samples)
    assert 2.0 < mid < 2.5


def test_apply_local_modifications_relief(cad, example_geometry):
    sections = cad.filter_valid_sections(example_geometry["sections"])[:5]
    base = [2.0] * len(sections)
    mods = [
        {
            "kind": "relief",
            "z_min_mm": 0.0,
            "z_max_mm": 999.0,
            "angle_start_deg": 0.0,
            "angle_end_deg": 360.0,
            "depth_mm": 0.5,
            "clinical_reason": "test",
        }
    ]
    final, log, _ = cad.apply_local_modifications_to_offsets(
        sections, base, mods, float(example_geometry["height_mm"])
    )
    assert all(f >= 2.0 for f in final)
    assert log[0]["applied"] is True


@pytest.mark.integration
def test_generate_socket_from_agent_creates_report(cad, example_geometry, minimal_agent_response, tmp_path):
    pytest.importorskip("cadquery")
    out = tmp_path / "out"
    payload = cad.generate_socket_from_agent(example_geometry, minimal_agent_response, out)
    assert payload["agent_driven"] is True
    assert (out / "agent_cad_report.json").is_file()
    assert payload["cad_execution"]["sections_loft"] >= 3
    if payload["status"] != "blocked":
        assert (out / "socket.stl").is_file()


def test_generate_socket_from_agent_blocked_when_no_socket_design(cad, example_geometry, tmp_path):
    agent = {
        "quality_gate": {
            "passed": False,
            "demo_eligible": False,
            "mean_error_mm": 5.0,
            "max_error_mm": 10.0,
            "section_similarity": 0.5,
            "volume_cm3": None,
            "volume_estimated": False,
            "messages": ["blocked"],
        },
        "geometry_reference": {"height_mm": 220, "section_count": 1, "coordinate_system": {}},
        "socket_design": None,
        "clinical_reasoning": {
            "pain_consideration": "x",
            "activity_adaptation": "y",
            "skin_safety_notes": "z",
            "contraindications": ["re-escaneo"],
        },
        "cadquery_handoff": {
            "steps": ["blocked"],
            "target_fit_tolerance_mm": {"min": 1, "max": 2},
            "design_mode": "blocked",
        },
        "design_parameters": {},
    }
    out = tmp_path / "blocked"
    payload = cad.generate_socket_from_agent(example_geometry, agent, out)
    assert payload["status"] == "blocked"
    assert not (out / "socket.stl").exists()
    assert (out / "agent_cad_report.json").is_file()

