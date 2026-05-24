class MeshError(Exception):
    """Base error for mesh processing."""


class MeshFileNotFoundError(MeshError):
    """Raised when the mesh file path does not exist."""


class MeshReadError(MeshError):
    """Raised when Open3D cannot read the mesh file."""


class EmptyMeshError(MeshError):
    """Raised when the mesh has no vertices or triangles."""
