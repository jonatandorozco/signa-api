"""Wrapper para importar generate_socket_from_agent desde socket_design.cad.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_CAD_PATH = _ROOT / "socket_design.cad.py"


def _load_cad_module():
    name = "socket_design_cad"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _CAD_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"No se pudo cargar {_CAD_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def generate_socket_from_agent(
    geometry: dict[str, Any] | None,
    agent_response: dict[str, Any],
    out_dir: Path,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cad = _load_cad_module()
    return cad.generate_socket_from_agent(geometry, agent_response, out_dir, report=report)
