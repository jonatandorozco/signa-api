#!/usr/bin/env python3
"""CLI: convierte socket.stl (u otro STL) a PLY."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.mesh_convert import convert_stl_file_to_ply


def main() -> int:
    parser = argparse.ArgumentParser(description="Convierte STL → PLY (fuera del pipeline agentico)")
    parser.add_argument("input_stl", type=Path, help="Ruta al archivo .stl")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Ruta de salida .ply (default: mismo nombre que el STL)",
    )
    args = parser.parse_args()

    stl_path = args.input_stl.resolve()
    if stl_path.suffix.lower() != ".stl":
        print("Error: la entrada debe ser .stl", file=sys.stderr)
        return 1
    if not stl_path.is_file():
        print(f"Error: no existe {stl_path}", file=sys.stderr)
        return 1

    ply_path = (args.output or stl_path.with_suffix(".ply")).resolve()
    meta = convert_stl_file_to_ply(stl_path, ply_path)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"Exportado: {ply_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
