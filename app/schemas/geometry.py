from pydantic import BaseModel, Field


class SectionProfile(BaseModel):
    """Contorno real 2D en altura Z (distal → proximal), listo para loft CAD."""

    z_mm: float
    area_mm2: float = Field(..., ge=0)
    perimeter_mm: float = Field(..., ge=0)
    curvature_score: float = Field(..., ge=0)
    contour_point_count: int = Field(..., ge=0)
    contour: list[list[float]]


class ReconstructionError(BaseModel):
    mean_error_mm: float = Field(..., ge=0)
    max_error_mm: float = Field(..., ge=0)
    p95_error_mm: float = Field(..., ge=0)


class QualityGate(BaseModel):
    """Validación para socket CadQuery / flujo clínico."""

    passed: bool
    demo_eligible: bool
    mean_error_mm: float = Field(..., ge=0)
    max_error_mm: float = Field(..., ge=0)
    p95_error_mm: float = Field(default=0.0, ge=0)
    section_similarity: float = Field(..., ge=0, le=1.0)
    volume_cm3: float | None = None
    volume_estimated: bool = False
    messages: list[str] = Field(default_factory=list)


class ShapeProfile(BaseModel):
    """Mapa longitudinal: evolución de área, perímetro e irregularidad."""

    z_mm: list[float]
    areas_mm2: list[float]
    perimeters_mm: list[float]
    area_growth_rate: list[float]
    irregularity_index: list[float]


class GeometryResponse(BaseModel):
    """Digital twin geométrico del muñón para IA y CadQuery."""

    height_mm: float = Field(..., ge=0)
    volume_cm3: float | None = None
    surface_irregularity: float = Field(..., ge=0)
    taper_ratio: float = Field(..., ge=0)
    section_similarity: float = Field(..., ge=0, le=1.0)
    shape_profile: ShapeProfile
    reconstruction_error: ReconstructionError
    quality_gate: QualityGate
    sections: list[SectionProfile]


class AnalyzeRequest(BaseModel):
    file_path: str


class UploadResponse(BaseModel):
    case_id: str
    filename: str
    file_path: str
