from pathlib import Path

import numpy as np
import open3d as o3d

from app.core.mesh_errors import EmptyMeshError, MeshFileNotFoundError, MeshReadError

SUPPORTED_EXTENSIONS = {".ply", ".stl", ".obj"}


def load_mesh(path: str) -> o3d.geometry.TriangleMesh:
    mesh_path = Path(path)
    if not mesh_path.is_file():
        raise MeshFileNotFoundError(f"Archivo no encontrado: {path}")

    suffix = mesh_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise MeshReadError(f"Extensión no soportada: {suffix}")

    try:
        mesh = o3d.io.read_triangle_mesh(str(mesh_path))
    except Exception as exc:
        raise MeshReadError(f"No se pudo leer la malla: {exc}") from exc

    if mesh.is_empty() or len(mesh.vertices) == 0:
        raise EmptyMeshError("La malla está vacía o no contiene vértices")

    return mesh


def _o3d_to_trimesh(mesh: o3d.geometry.TriangleMesh):
    import trimesh

    return trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float64),
        faces=np.asarray(mesh.triangles, dtype=np.int64),
        process=False,
    )


def _trimesh_to_o3d(mesh) -> o3d.geometry.TriangleMesh:
    o3d_mesh = o3d.geometry.TriangleMesh()
    o3d_mesh.vertices = o3d.utility.Vector3dVector(np.asarray(mesh.vertices))
    o3d_mesh.triangles = o3d.utility.Vector3iVector(np.asarray(mesh.faces))
    o3d_mesh.compute_vertex_normals()
    return o3d_mesh


def repair_mesh_basic(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    """Limpieza previa al análisis: deduplicación, huecos pequeños y suavizado ligero."""
    repaired = o3d.geometry.TriangleMesh(mesh)
    repaired.remove_duplicated_vertices()
    repaired.remove_duplicated_triangles()
    repaired.remove_degenerate_triangles()
    repaired.remove_non_manifold_edges()
    repaired.remove_unreferenced_vertices()
    try:
        repaired.orient_triangles()
    except (AttributeError, RuntimeError):
        pass

    try:
        import trimesh
        from trimesh import repair as trimesh_repair

        tm = _o3d_to_trimesh(repaired)
        trimesh_repair.fill_holes(tm)
        trimesh_repair.fix_normals(tm)
        tm.merge_vertices(merge_tex=True, merge_norm=True)
        repaired = _trimesh_to_o3d(tm)
    except Exception:
        pass

    try:
        repaired = repaired.filter_smooth_laplacian(number_of_iterations=3)
    except Exception:
        pass

    repaired.remove_degenerate_triangles()
    repaired.remove_unreferenced_vertices()
    repaired.compute_vertex_normals()
    return repaired


def clean_mesh(path: str) -> o3d.geometry.TriangleMesh:
    mesh = load_mesh(path)
    mesh = repair_mesh_basic(mesh)

    if mesh.is_empty() or len(mesh.vertices) == 0:
        raise EmptyMeshError("La malla quedó vacía después de la limpieza")

    o3d.io.write_triangle_mesh(path, mesh)
    return mesh
