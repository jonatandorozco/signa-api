"""
Digital twin geométrico de muñones: contornos reales por intersección plano-malla.

Pipeline: reparación → PCA robusto → orientación anatómica → trimesh.section →
RDP → suavizado longitudinal → métricas + error de reconstrucción.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

import numpy as np
import open3d as o3d
import trimesh
from trimesh import repair as trimesh_repair

from app.core.mesh_errors import EmptyMeshError, MeshFileNotFoundError, MeshReadError
from app.services.mesh_cleaner import SUPPORTED_EXTENSIONS, load_mesh, repair_mesh_basic

# --- Parámetros ---
_MIN_SECTIONS = 30
_MAX_SECTIONS = 60
_TARGET_SECTIONS_PER_MM = 1.0 / 2.5
_PCA_SAMPLE_SIZE = 10_000
_OUTLIER_NB_NEIGHBORS = 24
_OUTLIER_STD_RATIO = 2.0
_END_BAND_FRACTION = 0.12
_RDP_EPSILON_RATIO = 0.004  # ε relativo al perímetro del contorno
_LAPLACIAN_ITERATIONS = 4
_LONGITUDINAL_SMOOTH_WINDOW = 3
_CONTOUR_RESAMPLE_POINTS = 128
_RECON_VERTEX_SAMPLE = 25_000
_IRREGULARITY_TRIM_MULTIPLIER = 2.5
_MAX_END_TRIM_FRACTION = 0.12
_QUALITY_MEAN_MM_PRODUCTION = 2.0
_QUALITY_MEAN_MM_DEMO = 3.0
_QUALITY_MAX_MM_PRODUCTION = 12.0
# Muñón: m <5, cm 5–120, mm 120–800; >800 suele ser µm o mm×1000 (export CAD erróneo)
_MM_SCALE_THRESHOLD_M = 5.0
_MM_SCALE_THRESHOLD_CM = 120.0
_MM_MAX_PLAUSIBLE_EXTENT_MM = 800.0
_MM_PER_METER = 1000.0
_MM_PER_CM = 10.0
_MM_PER_MICRON = 0.001
_ANALYSIS_TARGET_TRIANGLES = 80_000
_RECON_END_BAND_FRACTION = 0.08
_QUALITY_P95_MM_PRODUCTION = 5.0


class SectionDict(TypedDict):
    z_mm: float
    area_mm2: float
    perimeter_mm: float
    curvature_score: float
    contour_point_count: int
    contour: list[list[float]]


AnalysisResult = dict[str, Any]


# ---------------------------------------------------------------------------
# Utilidades geométricas
# ---------------------------------------------------------------------------


def _planar_polygon_to_points(poly: Any) -> np.ndarray | None:
    """Convierte polígono 2D de trimesh (ndarray o shapely Polygon) a Nx2."""
    if poly is None:
        return None
    if hasattr(poly, "exterior"):
        coords = np.asarray(poly.exterior.coords, dtype=np.float64)
        if coords.shape[0] >= 2 and np.allclose(coords[0], coords[-1]):
            coords = coords[:-1]
        return coords if coords.shape[0] >= 3 else None
    pts = np.asarray(poly, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] < 2:
        return None
    pts = pts[:, :2]
    return pts if pts.shape[0] >= 3 else None


def _point_to_segment_distance(point: np.ndarray, seg_start: np.ndarray, seg_end: np.ndarray) -> float:
    seg = seg_end - seg_start
    seg_len_sq = float(np.dot(seg, seg))
    if seg_len_sq < 1e-12:
        return float(np.linalg.norm(point - seg_start))
    t = float(np.clip(np.dot(point - seg_start, seg) / seg_len_sq, 0.0, 1.0))
    closest = seg_start + t * seg
    return float(np.linalg.norm(point - closest))


def _point_to_closed_contour_xy(point_xy: np.ndarray, contour: np.ndarray) -> float:
    closed = np.vstack([contour, contour[0]])
    return min(
        _point_to_segment_distance(point_xy, closed[i], closed[i + 1])
        for i in range(len(contour))
    )


def _polygon_area_perimeter(contour: np.ndarray) -> tuple[float, float]:
    if contour.shape[0] < 3:
        if contour.shape[0] == 2:
            seg = float(np.linalg.norm(contour[1] - contour[0]))
            return 0.0, 2.0 * seg
        return 0.0, 0.0
    x = contour[:, 0]
    y = contour[:, 1]
    area = 0.5 * float(np.abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    perim = float(np.sum(np.linalg.norm(contour - np.roll(contour, -1, axis=0), axis=1)))
    return max(area, 0.0), max(perim, 0.0)


def _rdp(points: np.ndarray, epsilon: float) -> np.ndarray:
    """Ramer–Douglas–Peucker 2D."""
    if points.shape[0] <= 2 or epsilon <= 0:
        return points

    start, end = points[0], points[-1]
    segment = end - start
    seg_len = float(np.linalg.norm(segment))

    if seg_len < 1e-12:
        dists = np.linalg.norm(points - start, axis=1)
    else:
        dists = np.abs(np.cross(segment, points - start)) / seg_len

    idx = int(np.argmax(dists))
    max_dist = float(dists[idx])

    if max_dist <= epsilon:
        return np.vstack([start, end])

    left = _rdp(points[: idx + 1], epsilon)
    right = _rdp(points[idx:], epsilon)
    return np.vstack([left[:-1], right])


def _resample_closed_contour(contour: np.ndarray, n_points: int) -> np.ndarray:
    """Remuestrea un polígono cerrado a n_points por longitud de arco."""
    if contour.shape[0] < 3:
        return contour
    closed = np.vstack([contour, contour[0]])
    seg_lens = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total = cumulative[-1]
    if total < 1e-9:
        return contour
    targets = np.linspace(0.0, total, n_points, endpoint=False)
    resampled = np.zeros((n_points, 2), dtype=np.float64)
    for i, t in enumerate(targets):
        j = int(np.searchsorted(cumulative, t, side="right") - 1)
        j = min(max(j, 0), len(seg_lens) - 1)
        t0, t1 = cumulative[j], cumulative[j + 1]
        alpha = (t - t0) / (t1 - t0) if t1 - t0 > 1e-12 else 0.0
        resampled[i] = (1.0 - alpha) * closed[j] + alpha * closed[j + 1]
    return resampled


def _curvature_score(contour: np.ndarray) -> float:
    """Curvatura discreta media normalizada del contorno 2D."""
    n = contour.shape[0]
    if n < 4:
        return 0.0
    curvatures: list[float] = []
    for i in range(n):
        p_prev = contour[(i - 1) % n]
        p = contour[i]
        p_next = contour[(i + 1) % n]
        v1 = p - p_prev
        v2 = p_next - p
        l1 = float(np.linalg.norm(v1))
        l2 = float(np.linalg.norm(v2))
        if l1 < 1e-9 or l2 < 1e-9:
            continue
        v1 /= l1
        v2 /= l2
        angle = float(np.arccos(np.clip(np.dot(v1, v2), -1.0, 1.0)))
        curvatures.append(angle / max((l1 + l2) / 2.0, 1e-9))
    if not curvatures:
        return 0.0
    return float(np.mean(curvatures))


def _section_similarity(c1: np.ndarray, c2: np.ndarray) -> float:
    """Similitud [0,1] entre dos contornos (remuestreados + distancia promedio)."""
    if c1.shape[0] < 3 or c2.shape[0] < 3:
        return 0.0
    r1 = _resample_closed_contour(c1, _CONTOUR_RESAMPLE_POINTS)
    r2 = _resample_closed_contour(c2, _CONTOUR_RESAMPLE_POINTS)
    c1c = r1 - r1.mean(axis=0)
    c2c = r2 - r2.mean(axis=0)
    scale = max(float(np.max(np.linalg.norm(c1c, axis=1))), 1e-6)
    dist = float(np.mean(np.linalg.norm(c1c - c2c, axis=1)))
    return float(np.clip(1.0 - dist / scale, 0.0, 1.0))


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if values.size == 0:
        return values
    window = max(3, window | 1)
    if values.size < window:
        return values.copy()
    pad = window // 2
    padded = np.pad(values, (pad, pad), mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(padded, kernel, mode="valid")[: values.size]


# ---------------------------------------------------------------------------
# Open3D ↔ Trimesh
# ---------------------------------------------------------------------------


def _o3d_to_trimesh(mesh: o3d.geometry.TriangleMesh) -> trimesh.Trimesh:
    return trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float64),
        faces=np.asarray(mesh.triangles, dtype=np.int64),
        process=False,
    )


def _trimesh_to_o3d(mesh: trimesh.Trimesh) -> o3d.geometry.TriangleMesh:
    o3d_mesh = o3d.geometry.TriangleMesh()
    o3d_mesh.vertices = o3d.utility.Vector3dVector(np.asarray(mesh.vertices))
    o3d_mesh.triangles = o3d.utility.Vector3iVector(np.asarray(mesh.faces))
    o3d_mesh.compute_vertex_normals()
    return o3d_mesh


# ---------------------------------------------------------------------------
# Reparación y PCA
# ---------------------------------------------------------------------------


def repair_mesh_for_analysis(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    """Reparación previa al análisis (delegada a mesh_cleaner + decimación opcional)."""
    repaired = repair_mesh_basic(mesh)
    return _decimate_for_analysis(repaired)


def _decimate_for_analysis(
    mesh: o3d.geometry.TriangleMesh,
    target_triangles: int = _ANALYSIS_TARGET_TRIANGLES,
) -> o3d.geometry.TriangleMesh:
    """Reduce densidad en mallas muy pesadas sin perder la silueta."""
    n_tris = len(mesh.triangles)
    if n_tris <= target_triangles:
        return mesh
    try:
        simplified = mesh.simplify_quadric_decimation(target_number_of_triangles=target_triangles)
        simplified.remove_degenerate_triangles()
        simplified.remove_unreferenced_vertices()
        simplified.compute_vertex_normals()
        if len(simplified.vertices) >= 100 and len(simplified.triangles) >= 50:
            return simplified
    except Exception:
        pass
    return mesh


def normalize_mesh_to_mm(mesh: o3d.geometry.TriangleMesh) -> tuple[o3d.geometry.TriangleMesh, float]:
    """
    Escala la malla a milímetros según heurística de extent (bbox máximo):
    - < 5 → metros (×1000)
    - 5–120 → centímetros (×10)
    - 120–800 → ya en mm
    - > 800 → probable µm o mm mal etiquetados (÷1000 repetido hasta extent plausible)
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    if vertices.size == 0:
        return mesh, 1.0
    factor = 1.0
    while True:
        extent = float(np.max(vertices.max(axis=0) - vertices.min(axis=0)))
        if extent > _MM_MAX_PLAUSIBLE_EXTENT_MM:
            factor *= _MM_PER_MICRON
            vertices = vertices * _MM_PER_MICRON
            continue
        if extent >= _MM_SCALE_THRESHOLD_CM:
            break
        if extent >= _MM_SCALE_THRESHOLD_M:
            factor *= _MM_PER_CM
            vertices = vertices * _MM_PER_CM
            break
        factor *= _MM_PER_METER
        vertices = vertices * _MM_PER_METER
        break
    if abs(factor - 1.0) < 1e-12:
        return mesh, 1.0
    scaled = o3d.geometry.TriangleMesh(mesh)
    scaled.vertices = o3d.utility.Vector3dVector(vertices)
    scaled.compute_vertex_normals()
    return scaled, factor


def center_mesh_xy(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    """Centra la malla en XY (mantiene z distal en 0)."""
    centered = o3d.geometry.TriangleMesh(mesh)
    vertices = np.asarray(centered.vertices, dtype=np.float64).copy()
    if vertices.size == 0:
        return centered
    cx = float(np.mean(vertices[:, 0]))
    cy = float(np.mean(vertices[:, 1]))
    vertices[:, 0] -= cx
    vertices[:, 1] -= cy
    centered.vertices = o3d.utility.Vector3dVector(vertices)
    centered.compute_vertex_normals()
    return centered


def _center_sections_xy(sections: list[SectionDict]) -> list[SectionDict]:
    """Centra contornos en XY para loft CAD consistente en origen."""
    valid = [s for s in sections if s["contour_point_count"] >= 3]
    if not valid:
        return sections
    all_pts = np.vstack([np.array(s["contour"], dtype=np.float64) for s in valid])
    cx, cy = float(all_pts[:, 0].mean()), float(all_pts[:, 1].mean())
    centered: list[SectionDict] = []
    for sec in sections:
        if sec["contour_point_count"] < 3:
            centered.append(sec)
            continue
        pts = np.array(sec["contour"], dtype=np.float64)
        pts[:, 0] -= cx
        pts[:, 1] -= cy
        area, perim = _polygon_area_perimeter(pts)
        centered.append(
            SectionDict(
                z_mm=sec["z_mm"],
                area_mm2=round(area, 3),
                perimeter_mm=round(perim, 3),
                curvature_score=round(_curvature_score(pts), 5),
                contour_point_count=int(pts.shape[0]),
                contour=[[round(float(x), 4), round(float(y), 4)] for x, y in pts],
            )
        )
    return centered


def remove_statistical_outlier(
    vertices: np.ndarray,
    nb_neighbors: int = _OUTLIER_NB_NEIGHBORS,
    std_ratio: float = _OUTLIER_STD_RATIO,
) -> np.ndarray:
    if vertices.shape[0] < nb_neighbors + 1:
        return vertices
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(vertices.astype(np.float64))
    _, inlier_indices = pcd.remove_statistical_outlier(
        nb_neighbors=nb_neighbors,
        std_ratio=std_ratio,
    )
    indices = np.asarray(inlier_indices, dtype=np.int64)
    cleaned = vertices[indices]
    return cleaned if cleaned.shape[0] >= 3 else vertices


def uniform_sample_vertices(vertices: np.ndarray, target_size: int = _PCA_SAMPLE_SIZE) -> np.ndarray:
    if vertices.shape[0] <= target_size:
        return vertices
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(vertices.astype(np.float64))
    extent = float(np.max(pcd.get_axis_aligned_bounding_box().get_extent()))
    down = pcd.voxel_down_sample(max(extent / 80.0, 1e-3))
    sampled = np.asarray(down.points)
    if sampled.shape[0] > target_size:
        rng = np.random.default_rng(42)
        sampled = sampled[rng.choice(sampled.shape[0], target_size, replace=False)]
    return sampled if sampled.shape[0] >= 3 else vertices


def _rotation_matrix_from_vectors(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    src = source / np.linalg.norm(source)
    tgt = target / np.linalg.norm(target)
    dot = float(np.clip(np.dot(src, tgt), -1.0, 1.0))
    if dot > 1.0 - 1e-8:
        return np.eye(3)
    if dot < -1.0 + 1e-8:
        axis = np.array([1.0, 0.0, 0.0]) if abs(src[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = axis - np.dot(axis, src) * src
        axis /= np.linalg.norm(axis)
        skew = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]])
        return np.eye(3) + 2.0 * (skew @ skew)
    cross = np.cross(src, tgt)
    skew = np.array([[0, -cross[2], cross[1]], [cross[2], 0, -cross[0]], [-cross[1], cross[0], 0]])
    return np.eye(3) + skew + skew @ skew * (1.0 / (1.0 + dot))


def _principal_axis_pca(vertices: np.ndarray) -> np.ndarray:
    centered = vertices - vertices.mean(axis=0)
    cov = np.cov(centered, rowvar=False)
    evals, evecs = np.linalg.eigh(cov)
    principal = evecs[:, int(np.argmax(evals))]
    return principal if principal[2] >= 0 else -principal


def _contours_xy_from_path3d(path: trimesh.path.Path3D) -> list[np.ndarray]:
    """Contornos cerrados en coordenadas mundo XY desde Path3D de trimesh.section()."""
    contours: list[np.ndarray] = []
    for entity in path.entities:
        if not hasattr(entity, "points"):
            continue
        indices = np.asarray(entity.points, dtype=np.int64)
        if indices.size < 3:
            continue
        pts = np.asarray(path.vertices[indices], dtype=np.float64)
        if pts.shape[0] < 3:
            continue
        contours.append(pts[:, :2])
    return contours


def _largest_contour_xy_from_section(tm: trimesh.Trimesh, z_mm: float) -> np.ndarray | None:
    """Intersección plano Z = cte → polígono 2D más grande en XY mundo."""
    slice_3d = tm.section(plane_origin=[0.0, 0.0, z_mm], plane_normal=[0.0, 0.0, 1.0])
    if slice_3d is None:
        return None

    contours = _contours_xy_from_path3d(slice_3d)
    if not contours:
        planar, _ = slice_3d.to_planar()
        contours = _contours_xy_from_path3d(planar.to_3D())

    best: np.ndarray | None = None
    best_area = -1.0
    for contour in contours:
        area, _ = _polygon_area_perimeter(contour)
        if area > best_area:
            best_area = area
            best = contour
    return best


def align_mesh_pca(mesh: o3d.geometry.TriangleMesh) -> tuple[o3d.geometry.TriangleMesh, np.ndarray]:
    vertices = np.asarray(mesh.vertices)
    if vertices.size == 0:
        raise EmptyMeshError("La malla no tiene vértices para alinear")
    sampled = uniform_sample_vertices(remove_statistical_outlier(vertices))
    rotation = _rotation_matrix_from_vectors(
        _principal_axis_pca(sampled), np.array([0.0, 0.0, 1.0])
    )
    aligned = o3d.geometry.TriangleMesh(mesh)
    aligned.rotate(rotation, center=vertices.mean(axis=0))
    return aligned, rotation


def _end_section_radius(tm: trimesh.Trimesh, z: float) -> float:
    """Radio equivalente √(área/π) de la sección en un plano Z."""
    contour = _largest_contour_xy_from_section(tm, z)
    if contour is None:
        return 0.0
    area, _ = _polygon_area_perimeter(contour)
    if area <= 0:
        return 0.0
    return float(np.sqrt(area / np.pi))


def orient_mesh_anatomical(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    """z=0 distal (estrecho), z=altura proximal (ancho)."""
    oriented = o3d.geometry.TriangleMesh(mesh)
    tm = _o3d_to_trimesh(oriented)
    z_min, z_max = float(tm.vertices[:, 2].min()), float(tm.vertices[:, 2].max())
    height = z_max - z_min
    if height < 1e-6:
        return oriented

    band = height * _END_BAND_FRACTION
    r_low = _end_section_radius(tm, z_min + band / 2.0)
    r_high = _end_section_radius(tm, z_max - band / 2.0)

    vertices = np.asarray(oriented.vertices).copy()
    if r_low > r_high:
        vertices[:, 2] = z_max + z_min - vertices[:, 2]
    vertices[:, 2] -= vertices[:, 2].min()
    oriented.vertices = o3d.utility.Vector3dVector(vertices)
    oriented.compute_vertex_normals()
    return oriented


# ---------------------------------------------------------------------------
# Contornos reales
# ---------------------------------------------------------------------------


def _section_count(height_mm: float) -> int:
    if height_mm < 1e-6:
        return _MIN_SECTIONS
    estimated = int(height_mm * _TARGET_SECTIONS_PER_MM)
    return int(np.clip(estimated, _MIN_SECTIONS, _MAX_SECTIONS))


def _order_contour_ccw(contour: np.ndarray) -> np.ndarray:
    center = contour.mean(axis=0)
    angles = np.arctan2(contour[:, 1] - center[1], contour[:, 0] - center[0])
    return contour[np.argsort(angles)]


def extract_real_contours(mesh: o3d.geometry.TriangleMesh) -> list[SectionDict]:
    """
    Cortes horizontales reales mesh ∩ plane con trimesh.section().

    Cada sección incluye polígono ordenado, área, perímetro y curvatura.
    """
    tm = _o3d_to_trimesh(mesh)
    height = float(tm.vertices[:, 2].max() - tm.vertices[:, 2].min())
    n_sections = _section_count(height)
    z_levels = np.linspace(0.0, height, n_sections) if height > 1e-6 else np.zeros(n_sections)

    sections: list[SectionDict] = []
    for z_mm in z_levels:
        contour = _largest_contour_xy_from_section(tm, float(z_mm))
        if contour is None or contour.shape[0] < 3:
            sections.append(
                SectionDict(
                    z_mm=round(float(z_mm), 3),
                    area_mm2=0.0,
                    perimeter_mm=0.0,
                    curvature_score=0.0,
                    contour_point_count=0,
                    contour=[],
                )
            )
            continue

        contour = _order_contour_ccw(contour)
        _, perim = _polygon_area_perimeter(contour)
        epsilon = max(perim * _RDP_EPSILON_RATIO, 0.05)
        simplified = _rdp(contour, epsilon)
        if simplified.shape[0] >= 3:
            closed = np.vstack([simplified, simplified[0]])
            simplified = _rdp(closed, epsilon)
        if simplified.shape[0] < 3:
            simplified = contour

        area, perimeter = _polygon_area_perimeter(simplified)
        curvature = _curvature_score(simplified)
        contour_list = [[round(float(x), 4), round(float(y), 4)] for x, y in simplified]

        sections.append(
            SectionDict(
                z_mm=round(float(z_mm), 3),
                area_mm2=round(area, 3),
                perimeter_mm=round(perimeter, 3),
                curvature_score=round(curvature, 5),
                contour_point_count=len(contour_list),
                contour=contour_list,
            )
        )

    return _fill_empty_sections(sections)


def _fill_empty_sections(sections: list[SectionDict]) -> list[SectionDict]:
    """Rellena cortes fallidos interpolando contornos vecinos."""
    filled = [dict(s) for s in sections]
    valid_idx = [i for i, s in enumerate(filled) if s["contour_point_count"] >= 3]
    if not valid_idx:
        return filled
    for i, sec in enumerate(filled):
        if sec["contour_point_count"] >= 3:
            continue
        prev_i = max((j for j in valid_idx if j < i), default=valid_idx[0])
        next_i = min((j for j in valid_idx if j > i), default=valid_idx[-1])
        if prev_i == next_i:
            donor = filled[prev_i]
        else:
            alpha = (i - prev_i) / max(next_i - prev_i, 1)
            c_prev = np.array(filled[prev_i]["contour"])
            c_next = np.array(filled[next_i]["contour"])
            r_prev = _resample_closed_contour(c_prev, _CONTOUR_RESAMPLE_POINTS)
            r_next = _resample_closed_contour(c_next, _CONTOUR_RESAMPLE_POINTS)
            blended = (1.0 - alpha) * r_prev + alpha * r_next
            area, perim = _polygon_area_perimeter(blended)
            donor = {
                "z_mm": sec["z_mm"],
                "area_mm2": area,
                "perimeter_mm": perim,
                "curvature_score": _curvature_score(blended),
                "contour_point_count": blended.shape[0],
                "contour": [[round(float(x), 4), round(float(y), 4)] for x, y in blended],
            }
        filled[i] = {
            "z_mm": sec["z_mm"],
            "area_mm2": donor["area_mm2"],
            "perimeter_mm": donor["perimeter_mm"],
            "curvature_score": donor["curvature_score"],
            "contour_point_count": donor["contour_point_count"],
            "contour": donor["contour"],
        }
    return filled  # type: ignore[return-value]


def smooth_contours_longitudinally(
    sections: list[SectionDict], window: int = _LONGITUDINAL_SMOOTH_WINDOW
) -> list[SectionDict]:
    """
    Suaviza vértices correspondientes entre cortes (mismo índice de arco).

    Reduce ruido de escaneo sin cambiar el número de puntos por contorno.
    """
    valid = [s for s in sections if s["contour_point_count"] >= 3]
    if len(valid) < 3:
        return sections

    resampled = [
        _resample_closed_contour(np.array(s["contour"]), _CONTOUR_RESAMPLE_POINTS) for s in valid
    ]
    stack = np.stack(resampled, axis=0)
    smoothed_stack = np.zeros_like(stack)
    for j in range(_CONTOUR_RESAMPLE_POINTS):
        smoothed_stack[:, j, 0] = _moving_average(stack[:, j, 0], window)
        smoothed_stack[:, j, 1] = _moving_average(stack[:, j, 1], window)

    valid_idx = [i for i, s in enumerate(sections) if s["contour_point_count"] >= 3]
    result = [dict(s) for s in sections]
    for k, i in enumerate(valid_idx):
        contour = smoothed_stack[k]
        area, perim = _polygon_area_perimeter(contour)
        result[i] = SectionDict(
            z_mm=sections[i]["z_mm"],
            area_mm2=round(area, 3),
            perimeter_mm=round(perim, 3),
            curvature_score=round(_curvature_score(contour), 5),
            contour_point_count=int(contour.shape[0]),
            contour=[[round(float(x), 4), round(float(y), 4)] for x, y in contour],
        )
    return result  # type: ignore[return-value]


def _trim_irregular_end_sections(sections: list[SectionDict]) -> list[SectionDict]:
    """
    Recorta cortes en extremos con irregularidad anómala (tapas sucias del escáner).
    """
    valid_idx = [i for i, s in enumerate(sections) if s["contour_point_count"] >= 3]
    if len(valid_idx) < 12:
        return sections

    scores = [sections[i]["curvature_score"] for i in valid_idx]
    median_score = float(np.median(scores))
    threshold = max(median_score * _IRREGULARITY_TRIM_MULTIPLIER, median_score + 0.025)
    max_trim = max(1, int(len(valid_idx) * _MAX_END_TRIM_FRACTION))

    trim_low = 0
    for k in range(max_trim):
        if scores[k] > threshold:
            trim_low += 1
        else:
            break

    trim_high = 0
    for k in range(max_trim):
        if scores[-(k + 1)] > threshold:
            trim_high += 1
        else:
            break

    if trim_low == 0 and trim_high == 0:
        return sections

    keep_from = valid_idx[trim_low]
    keep_to = valid_idx[-(trim_high + 1)] if trim_high else valid_idx[-1]
    return [dict(s) for s in sections[keep_from : keep_to + 1]]


KNEE_MARGIN_BELOW_BREAK_FRACTION = 0.18
KNEE_SEARCH_LOWER_Z_FRACTION = 0.32
KNEE_STUMP_UPPER_FRACTION = 0.48
MIN_RELATIVE_AREA_JUMP = 1.15
KNEE_ONSET_AREA_RATIO = 1.18
MIN_SOCKET_SPAN_FRACTION = 0.35


def detect_knee_area_break(
    sections: list[SectionDict],
    *,
    margin_below_break_fraction: float = KNEE_MARGIN_BELOW_BREAK_FRACTION,
) -> dict[str, Any]:
    """
    Detecta salto brusco de área proximal (transición muñón → rodilla en el escaneo).

    El tope sugerido del socket queda `margin_below_break_fraction` por debajo del salto,
    medido desde z distal (hacia abajo = menos z).
    """
    valid = sorted(
        [s for s in sections if int(s.get("contour_point_count", 0) or 0) >= 3 and s.get("area_mm2", 0) > 0],
        key=lambda s: float(s["z_mm"]),
    )
    empty: dict[str, Any] = {
        "detected": False,
        "area_break_z_mm": None,
        "suggested_trim_height_mm": None,
        "margin_below_break_fraction": margin_below_break_fraction,
        "relative_area_jump": None,
    }
    if len(valid) < 6:
        return empty

    z_vals = np.array([float(s["z_mm"]) for s in valid], dtype=np.float64)
    areas = np.array([float(s["area_mm2"]) for s in valid], dtype=np.float64)
    z_min = float(z_vals[0])
    z_max = float(z_vals[-1])
    span = z_max - z_min
    if span < 50.0:
        return empty

    search_z_min = z_min + KNEE_SEARCH_LOWER_Z_FRACTION * span
    stump_mask = z_vals <= z_min + KNEE_STUMP_UPPER_FRACTION * span
    stump_ref = float(np.median(areas[stump_mask])) if np.any(stump_mask) else float(np.median(areas))

    first_onset_z: float | None = None
    for i in range(len(valid) - 1):
        z_mid = 0.5 * (z_vals[i] + z_vals[i + 1])
        if z_mid < search_z_min:
            continue
        if float(areas[i + 1]) >= stump_ref * KNEE_ONSET_AREA_RATIO and float(areas[i]) <= stump_ref * 1.08:
            first_onset_z = float(z_vals[i])
            break

    best_score = 0.0
    best_z_break: float | None = None
    best_jump = 1.0
    best_idx = -1

    for i in range(len(valid) - 1):
        z_mid = 0.5 * (z_vals[i] + z_vals[i + 1])
        if z_mid < search_z_min:
            continue
        dz = float(z_vals[i + 1] - z_vals[i])
        if dz < 1e-6:
            continue
        a0, a1 = float(areas[i]), float(areas[i + 1])
        if a1 <= a0:
            continue
        rel_jump = a1 / max(a0, 1e-6)
        if rel_jump < MIN_RELATIVE_AREA_JUMP:
            continue
        da_dz = (a1 - a0) / dz
        score = (rel_jump - 1.0) * da_dz
        if score > best_score:
            best_score = score
            best_z_break = float(z_vals[i + 1])
            best_jump = rel_jump
            best_idx = i + 1

    if best_z_break is None and first_onset_z is None:
        return empty

    if first_onset_z is not None:
        z_break = first_onset_z
        idx = min(int(np.searchsorted(z_vals, first_onset_z, side="right")), len(areas) - 1)
        prev_a = float(areas[idx - 1]) if idx > 0 else stump_ref
        best_jump = float(areas[idx] / max(prev_a, 1e-6))
    else:
        z_break = float(best_z_break)

    margin = margin_below_break_fraction * (z_break - z_min)
    z_trim = z_break - margin
    min_trim = z_min + max(MIN_SOCKET_SPAN_FRACTION * span, 60.0)
    z_trim = float(np.clip(z_trim, min_trim, z_max - 1.0))

    return {
        "detected": True,
        "area_break_z_mm": round(z_break, 3),
        "suggested_trim_height_mm": round(z_trim, 3),
        "margin_below_break_fraction": margin_below_break_fraction,
        "relative_area_jump": round(best_jump, 4) if best_jump else None,
        "first_onset_z_mm": round(first_onset_z, 3) if first_onset_z is not None else None,
        "peak_jump_z_mm": round(float(best_z_break), 3) if best_z_break is not None else None,
        "detection_score": round(best_score, 4),
        "stump_reference_area_mm2": round(stump_ref, 3),
        "z_min_mm": round(z_min, 3),
        "z_max_mm": round(z_max, 3),
    }


def resolve_socket_trim_from_geometry(
    geometry: dict[str, Any],
    height_mm: float,
    *,
    default_fraction: float,
    explicit_fraction: float | None = None,
    explicit_trim_mm: float | None = None,
    use_knee_detection: bool = True,
) -> tuple[float, float, str]:
    """
    Devuelve (trim_height_mm, socket_length_fraction, source).
    Prioridad: trim explícito > fracción explícita > rodilla detectada > default.
    """
    height_mm = max(float(height_mm), 1.0)
    if explicit_trim_mm is not None and explicit_trim_mm > 0:
        trim = min(float(explicit_trim_mm), height_mm)
        return trim, trim / height_mm, "explicit_trim_height_mm"

    if explicit_fraction is not None:
        frac = float(explicit_fraction)
        return round(height_mm * frac, 3), frac, "explicit_socket_length_fraction"

    landmark = geometry.get("knee_landmark") or {}
    if use_knee_detection and landmark.get("detected") and landmark.get("suggested_trim_height_mm"):
        trim = float(landmark["suggested_trim_height_mm"])
        trim = min(max(trim, 1.0), height_mm)
        return trim, trim / height_mm, "knee_area_break"

    frac = float(default_fraction)
    return round(height_mm * frac, 3), frac, "default_fraction"


def build_shape_profile(sections: list[SectionDict]) -> dict[str, list[float]]:
    """Mapa longitudinal de crecimiento e irregularidad."""
    valid = [s for s in sections if s["contour_point_count"] >= 3]
    if not valid:
        return {
            "z_mm": [],
            "areas_mm2": [],
            "perimeters_mm": [],
            "area_growth_rate": [],
            "irregularity_index": [],
        }

    z_vals = [s["z_mm"] for s in valid]
    areas = [s["area_mm2"] for s in valid]
    perims = [s["perimeter_mm"] for s in valid]
    growth = [0.0] + [areas[i] - areas[i - 1] for i in range(1, len(areas))]
    irregularity = [s["curvature_score"] for s in valid]

    return {
        "z_mm": z_vals,
        "areas_mm2": areas,
        "perimeters_mm": perims,
        "area_growth_rate": [round(g, 3) for g in growth],
        "irregularity_index": irregularity,
    }


def compute_section_similarity(sections: list[SectionDict]) -> float:
    """Similitud media entre cortes consecutivos (detección de artefactos)."""
    valid = [np.array(s["contour"]) for s in sections if s["contour_point_count"] >= 3]
    if len(valid) < 2:
        return 1.0
    scores = [_section_similarity(valid[i], valid[i + 1]) for i in range(len(valid) - 1)]
    return round(float(np.mean(scores)), 4)


def compute_surface_metrics(sections: list[SectionDict]) -> dict[str, float]:
    areas = np.array([s["area_mm2"] for s in sections if s["area_mm2"] > 0], dtype=np.float64)
    if areas.size == 0:
        return {"surface_irregularity": 0.0, "taper_ratio": 1.0}
    mean_area = float(np.mean(areas))
    surface_irregularity = float(np.std(areas) / max(mean_area, 1e-6))
    n = max(1, len(areas) // 10)
    proximal = float(np.mean(areas[-n:]))
    distal = float(np.mean(areas[:n]))
    taper_ratio = proximal / max(distal, 1e-6)
    return {
        "surface_irregularity": round(surface_irregularity, 4),
        "taper_ratio": round(taper_ratio, 4),
    }


def reconstruction_error(
    mesh: o3d.geometry.TriangleMesh, sections: list[SectionDict]
) -> dict[str, float]:
    """
    Distancia en XY de vértices al contorno del corte más cercano en Z.

    Excluye bandas distal/proximal (apertura del muñón y artefactos de borde).
    El quality gate usa p95; max_error se conserva solo como diagnóstico.
    """
    valid_sections = [
        (sec["z_mm"], np.array(sec["contour"], dtype=np.float64))
        for sec in sections
        if sec["contour_point_count"] >= 3
    ]
    if not valid_sections:
        return {"mean_error_mm": 0.0, "max_error_mm": 0.0, "p95_error_mm": 0.0}

    z_levels = np.array([z for z, _ in valid_sections], dtype=np.float64)
    contours = [c for _, c in valid_sections]
    z_min, z_max = float(z_levels.min()), float(z_levels.max())
    band = max((z_max - z_min) * _RECON_END_BAND_FRACTION, 1.0)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    in_band = (vertices[:, 2] >= z_min + band) & (vertices[:, 2] <= z_max - band)
    vertices = vertices[in_band]
    if vertices.shape[0] == 0:
        vertices = np.asarray(mesh.vertices, dtype=np.float64)

    if vertices.shape[0] > _RECON_VERTEX_SAMPLE:
        rng = np.random.default_rng(42)
        vertices = vertices[rng.choice(vertices.shape[0], _RECON_VERTEX_SAMPLE, replace=False)]

    distances = np.empty(vertices.shape[0], dtype=np.float64)
    for i, vertex in enumerate(vertices):
        sec_idx = int(np.argmin(np.abs(z_levels - vertex[2])))
        distances[i] = _point_to_closed_contour_xy(vertex[:2], contours[sec_idx])

    return {
        "mean_error_mm": round(float(np.mean(distances)), 4),
        "max_error_mm": round(float(np.max(distances)), 4),
        "p95_error_mm": round(float(np.percentile(distances, 95)), 4),
    }


def _volume_from_sections_cm3(sections: list[SectionDict]) -> float | None:
    """Estimación por integración de áreas (mm³ → cm³) cuando la malla no es cerrada."""
    valid = sorted(
        [s for s in sections if s["area_mm2"] > 0 and s["contour_point_count"] >= 3],
        key=lambda s: s["z_mm"],
    )
    if len(valid) < 2:
        return None
    vol_mm3 = 0.0
    for i in range(len(valid) - 1):
        dz = valid[i + 1]["z_mm"] - valid[i]["z_mm"]
        if dz <= 0:
            continue
        vol_mm3 += 0.5 * (valid[i]["area_mm2"] + valid[i + 1]["area_mm2"]) * dz
    if vol_mm3 <= 0 or not np.isfinite(vol_mm3):
        return None
    return round(vol_mm3 / 1000.0, 3)


def _compute_volume_cm3(
    mesh: o3d.geometry.TriangleMesh, sections: list[SectionDict]
) -> tuple[float | None, bool]:
    """Volumen en cm³: malla cerrada o estimación longitudinal por secciones."""
    if mesh.is_watertight():
        try:
            vol = float(mesh.get_volume())
            if vol > 0 and np.isfinite(vol):
                return round(vol / 1000.0, 3), False
        except RuntimeError:
            pass

    tm = _o3d_to_trimesh(mesh)
    if tm.is_watertight:
        vol = float(tm.volume)
        if vol > 0 and np.isfinite(vol):
            return round(vol / 1000.0, 3), False

    estimated = _volume_from_sections_cm3(sections)
    if estimated is not None:
        return estimated, True
    return None, False


def build_quality_gate(
    reconstruction: dict[str, float],
    volume_cm3: float | None,
    volume_estimated: bool,
    section_similarity: float,
) -> dict[str, Any]:
    mean_err = reconstruction["mean_error_mm"]
    max_err = reconstruction["max_error_mm"]
    p95_err = reconstruction["p95_error_mm"]
    messages: list[str] = []

    if mean_err > _QUALITY_MEAN_MM_PRODUCTION:
        messages.append(
            f"mean_error_mm {mean_err} > {_QUALITY_MEAN_MM_PRODUCTION} (umbral socket clínico)"
        )
    if p95_err > _QUALITY_P95_MM_PRODUCTION:
        messages.append(
            f"p95_error_mm {p95_err} > {_QUALITY_P95_MM_PRODUCTION} (revisar ruido o secciones)"
        )
    if max_err > _QUALITY_MAX_MM_PRODUCTION:
        messages.append(
            f"max_error_mm {max_err} > {_QUALITY_MAX_MM_PRODUCTION} (outliers en extremos; informativo)"
        )
    if volume_cm3 is None:
        messages.append("volume_cm3 no disponible")
    elif volume_estimated:
        messages.append("volume_cm3 estimado por integración de secciones (muñón abierto en proximal)")

    if section_similarity < 0.85:
        messages.append(f"section_similarity {section_similarity} < 0.85")

    production_ok = (
        mean_err <= _QUALITY_MEAN_MM_PRODUCTION
        and p95_err <= _QUALITY_P95_MM_PRODUCTION
        and volume_cm3 is not None
        and section_similarity >= 0.85
    )
    demo_ok = mean_err <= _QUALITY_MEAN_MM_DEMO and section_similarity >= 0.80

    if production_ok:
        messages.insert(0, "Apto para socket con revisión clínica estándar")
    elif demo_ok:
        messages.insert(0, "Modo demo: revisión manual obligatoria antes de fabricar")

    return {
        "passed": production_ok,
        "demo_eligible": demo_ok,
        "mean_error_mm": mean_err,
        "max_error_mm": max_err,
        "p95_error_mm": p95_err,
        "section_similarity": section_similarity,
        "volume_cm3": volume_cm3,
        "volume_estimated": volume_estimated,
        "messages": messages,
    }


def _ensure_geometry_dependencies() -> None:
    """Trimesh.section() requiere scipy, shapely y networkx en runtime."""
    missing: list[str] = []
    for module in ("scipy", "shapely", "networkx"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        packages = " ".join(missing)
        raise MeshReadError(
            "Dependencias faltantes para análisis geométrico: "
            f"{', '.join(missing)}. "
            f"Instala con: py -3.12 -m pip install {packages} "
            "y reinicia uvicorn (Ctrl+C y volver a arrancar)."
        )


def analyze_mesh(path: str) -> AnalysisResult:
    """Pipeline digital twin: reparación → PCA → contornos reales → métricas."""
    _ensure_geometry_dependencies()
    mesh_path = Path(path)
    if not mesh_path.is_file():
        raise MeshFileNotFoundError(f"Archivo no encontrado: {path}")
    if mesh_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MeshReadError(f"Extensión no soportada: {mesh_path.suffix.lower()}")

    try:
        mesh = load_mesh(path)
    except (MeshFileNotFoundError, EmptyMeshError, MeshReadError):
        raise
    except Exception as exc:
        raise MeshReadError(f"Error al cargar la malla: {exc}") from exc

    if not mesh.has_triangles() or len(mesh.triangles) == 0:
        raise EmptyMeshError("La malla no contiene triángulos")

    mesh, scale_to_mm = normalize_mesh_to_mm(mesh)

    try:
        repaired = repair_mesh_for_analysis(mesh)
        aligned, _ = align_mesh_pca(repaired)
        oriented = orient_mesh_anatomical(aligned)
        oriented = center_mesh_xy(oriented)
        raw_sections = extract_real_contours(oriented)
        sections = smooth_contours_longitudinally(raw_sections)
        sections = _trim_irregular_end_sections(sections)
        sections = _center_sections_xy(sections)
        shape_profile = build_shape_profile(sections)
        surface_metrics = compute_surface_metrics(sections)
        section_sim = compute_section_similarity(sections)
        recon_err = reconstruction_error(oriented, sections)
        valid_z = [s["z_mm"] for s in sections if s["contour_point_count"] >= 3]
        height_mm = float(max(valid_z)) if valid_z else float(np.asarray(oriented.vertices)[:, 2].max())
        volume_cm3, volume_estimated = _compute_volume_cm3(oriented, sections)
        quality_gate = build_quality_gate(recon_err, volume_cm3, volume_estimated, section_sim)
        knee_landmark = detect_knee_area_break(sections)
    except EmptyMeshError:
        raise
    except Exception as exc:
        raise MeshReadError(f"Error durante el análisis geométrico: {exc}") from exc

    return {
        "height_mm": round(height_mm, 3),
        "mesh_scale_to_mm_factor": round(scale_to_mm, 6),
        "volume_cm3": volume_cm3,
        "sections": sections,
        "shape_profile": shape_profile,
        "knee_landmark": knee_landmark,
        "section_similarity": section_sim,
        "reconstruction_error": recon_err,
        "quality_gate": quality_gate,
        "analysis_summary": _build_analysis_summary(
            height_mm,
            volume_cm3,
            volume_estimated,
            recon_err,
            quality_gate,
            section_sim,
            knee_landmark,
        ),
        **surface_metrics,
    }


def _build_analysis_summary(
    height_mm: float,
    volume_cm3: float | None,
    volume_estimated: bool,
    recon: dict[str, float],
    quality_gate: dict[str, Any],
    section_similarity: float,
    knee_landmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resumen compacto para agente/LLM (sin contornos)."""
    summary: dict[str, Any] = {
        "height_mm": round(height_mm, 3),
        "volume_cm3": volume_cm3,
        "volume_estimated": volume_estimated,
        "section_similarity": section_similarity,
        "mean_error_mm": recon.get("mean_error_mm"),
        "p95_error_mm": recon.get("p95_error_mm"),
        "max_error_mm": recon.get("max_error_mm"),
        "quality_passed": quality_gate.get("passed"),
        "demo_eligible": quality_gate.get("demo_eligible"),
        "messages": quality_gate.get("messages", []),
    }
    if knee_landmark and knee_landmark.get("detected"):
        summary["knee_landmark"] = {
            "area_break_z_mm": knee_landmark.get("area_break_z_mm"),
            "suggested_trim_height_mm": knee_landmark.get("suggested_trim_height_mm"),
            "relative_area_jump": knee_landmark.get("relative_area_jump"),
        }
    return summary
