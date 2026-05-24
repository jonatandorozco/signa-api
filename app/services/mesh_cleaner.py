from pathlib import Path

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


def remove_duplicated_vertices(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    mesh.remove_duplicated_vertices()
    return mesh


def remove_duplicated_triangles(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    mesh.remove_duplicated_triangles()
    return mesh


def remove_degenerate_triangles(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    mesh.remove_degenerate_triangles()
    return mesh


def compute_vertex_normals(mesh: o3d.geometry.TriangleMesh) -> o3d.geometry.TriangleMesh:
    mesh.compute_vertex_normals()
    return mesh


def clean_mesh(path: str) -> o3d.geometry.TriangleMesh:
    mesh = load_mesh(path)
    mesh = remove_duplicated_vertices(mesh)
    mesh = remove_duplicated_triangles(mesh)
    mesh = remove_degenerate_triangles(mesh)
    mesh = compute_vertex_normals(mesh)

    if mesh.is_empty() or len(mesh.vertices) == 0:
        raise EmptyMeshError("La malla quedó vacía después de la limpieza")

    o3d.io.write_triangle_mesh(path, mesh)
    return mesh
