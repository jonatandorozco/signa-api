"""Tests del pipeline POST /socket y servicios internos."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DATOS_REPORTE = ROOT / "app" / "data" / "datos_reporte.json"
EXAMPLE_GEOMETRY = ROOT / "app" / "data" / "geometry_analysis_example.json"
TEMP_UPLOAD = ROOT / "app" / "temp_upload"


@pytest.fixture
def example_geometry() -> dict:
    with EXAMPLE_GEOMETRY.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_socket_preferences_in_datos_reporte():
    with DATOS_REPORTE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    prefs = data.get("socket_preferences")
    assert prefs is not None
    assert prefs["radial_clearance_mm"] == 3.0


def test_attach_cad_geometry_has_contour(example_geometry):
    from app.services.socket_design_agent import run_socket_design_agent
    from app.services.socket_design_merge import attach_cad_geometry

    agent = run_socket_design_agent(example_geometry)
    base = {k: v for k, v in agent.items() if k != "cad_geometry"}
    merged = attach_cad_geometry(base, example_geometry)
    sections = merged["cad_geometry"]["sections"]
    assert sections[0].get("contour")
    assert len(sections[0]["contour"]) >= 3


def test_apply_clinical_preferences_increases_offset(example_geometry):
    from app.services.clinical_preferences import apply_clinical_preferences_to_agent
    from app.services.socket_design_agent import run_socket_design_agent

    report = {"socket_preferences": {"radial_clearance_mm": 3.5, "extra_holgura_mm": 0.0}}
    base_agent = run_socket_design_agent(example_geometry, clinical_report={"residual_limb_status": {}})
    base_agent.pop("cad_geometry", None)
    baseline = base_agent["socket_design"]["base_offsets"]["samples"][0]["offset_mm"]

    adjusted = apply_clinical_preferences_to_agent(
        base_agent, report, float(example_geometry["height_mm"])
    )
    assert adjusted["socket_design"]["base_offsets"]["samples"][0]["offset_mm"] >= 3.5
    assert adjusted["socket_design"]["base_offsets"]["samples"][0]["offset_mm"] > baseline


def test_post_socket_pipeline(monkeypatch, tmp_path, example_geometry):
    from app.core import paths
    from app.main import app
    from app.routes import socket as socket_route
    from app.services import socket_pipeline as pipeline

    case_id = str(uuid.uuid4())
    ply_path = TEMP_UPLOAD / f"{case_id}.ply"
    TEMP_UPLOAD.mkdir(parents=True, exist_ok=True)
    ply_path.write_bytes(b"mock ply content")

    monkeypatch.setattr(paths, "SOCKET_OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(socket_route, "SOCKET_OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "SOCKET_OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "clean_mesh", lambda _p: None)
    monkeypatch.setattr(pipeline, "analyze_mesh", lambda _p: example_geometry)

    def fake_generate(geometry, agent_payload, out_dir, report=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": "production",
            "agent_driven": True,
            "exports": {"stl": "socket.stl", "step": None, "report": "agent_cad_report.json"},
        }
        (out_dir / "agent_cad_report.json").write_text(json.dumps(payload), encoding="utf-8")
        (out_dir / "socket.stl").write_bytes(b"solid mock")
        return payload

    monkeypatch.setattr(pipeline, "generate_socket_from_agent", fake_generate)

    client = TestClient(app)
    with ply_path.open("rb") as handle:
        resp = client.post(
            "/socket",
            files={"file": ("munon.ply", handle, "application/octet-stream")},
            data={"engine": "rules", "generate_stl": "true"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"]
    assert body["download_urls"]["stl"] == f"/socket/{body['job_id']}/stl"
    assert (tmp_path / body["job_id"] / "geometry_analysis.json").is_file()
    assert (tmp_path / body["job_id"] / "agent_response.json").is_file()

    dl = client.get(body["download_urls"]["stl"])
    assert dl.status_code == 200
