"""Conversión de mallas fuera del pipeline agentico."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

import trimesh


def _load_trimesh_from_bytes(content: bytes, suffix: str) -> trimesh.Trimesh:
    if not content:
        raise ValueError("El archivo está vacío")
    mesh = trimesh.load(BytesIO(content), file_type=suffix.lstrip("."))
    if isinstance(mesh, trimesh.Scene):
        geometries = [g for g in mesh.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not geometries:
            raise ValueError("La escena STL no contiene mallas válidas")
        mesh = trimesh.util.concatenate(geometries)
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.vertices) == 0:
        raise ValueError("No se pudo leer una malla válida desde el STL")
    return mesh


def convert_stl_bytes_to_ply(content: bytes) -> tuple[bytes, dict[str, Any]]:
    """Convierte STL binario/ASCII a PLY (binary little endian)."""
    mesh = _load_trimesh_from_bytes(content, ".stl")
    ply_bytes = mesh.export(file_type="ply")
    if isinstance(ply_bytes, str):
        ply_bytes = ply_bytes.encode("utf-8")
    meta = {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "extent_mm": [round(float(x), 3) for x in mesh.extents.tolist()],
    }
    return ply_bytes, meta


def convert_stl_file_to_ply(stl_path: Path, ply_path: Path) -> dict[str, Any]:
    content = stl_path.read_bytes()
    ply_bytes, meta = convert_stl_bytes_to_ply(content)
    ply_path.parent.mkdir(parents=True, exist_ok=True)
    ply_path.write_bytes(ply_bytes)
    meta["stl"] = str(stl_path)
    meta["ply"] = str(ply_path)
    return meta
