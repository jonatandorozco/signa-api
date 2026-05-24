#!/usr/bin/env python3
"""Genera geometry_analysis.json sintético para probar socket_design.cad.py."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "geometry_analysis_example.json"


def ellipse_contour(a: float, b: float, n: int = 128) -> list[list[float]]:
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return [[round(a * math.cos(ti), 4), round(b * math.sin(ti), 4)] for ti in t]


def polygon_area_perimeter(contour: list[list[float]]) -> tuple[float, float]:
    pts = np.array(contour, dtype=np.float64)
    x, y = pts[:, 0], pts[:, 1]
    area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    perim = float(np.sum(np.linalg.norm(np.diff(np.vstack([pts, pts[0]]), axis=0), axis=1)))
    return area, perim


def main() -> None:
    height_mm = 220.0
    n_sections = 40
    z_levels = np.linspace(0.0, height_mm, n_sections)
    sections = []

    for z in z_levels:
        t = z / height_mm
        a = 32.0 + 18.0 * t
        b = 28.0 + 14.0 * t
        contour = ellipse_contour(a, b)
        area, perim = polygon_area_perimeter(contour)
        curvature = 0.15 + 0.05 * math.sin(t * math.pi * 4)
        if z < height_mm * 0.08 or z > height_mm * 0.92:
            curvature *= 2.5
        sections.append(
            {
                "z_mm": round(float(z), 3),
                "area_mm2": round(area, 3),
                "perimeter_mm": round(perim, 3),
                "curvature_score": round(curvature, 5),
                "contour_point_count": len(contour),
                "contour": contour,
            }
        )

    areas = [s["area_mm2"] for s in sections]
    growth = [0.0] + [areas[i] - areas[i - 1] for i in range(1, len(areas))]
    irr = [s["curvature_score"] for s in sections]

    payload = {
        "height_mm": height_mm,
        "volume_cm3": 1850.0,
        "surface_irregularity": 0.12,
        "taper_ratio": 1.35,
        "section_similarity": 0.91,
        "shape_profile": {
            "z_mm": [s["z_mm"] for s in sections],
            "areas_mm2": areas,
            "perimeters_mm": [s["perimeter_mm"] for s in sections],
            "area_growth_rate": [round(g, 3) for g in growth],
            "irregularity_index": irr,
        },
        "reconstruction_error": {
            "mean_error_mm": 1.45,
            "max_error_mm": 8.2,
            "p95_error_mm": 3.1,
        },
        "quality_gate": {
            "passed": True,
            "demo_eligible": True,
            "mean_error_mm": 1.45,
            "max_error_mm": 8.2,
            "section_similarity": 0.91,
            "volume_cm3": 1850.0,
            "volume_estimated": False,
            "messages": ["Apto para socket con revisión clínica estándar"],
        },
        "sections": sections,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(f"Escrito: {OUT}")


if __name__ == "__main__":
    main()
