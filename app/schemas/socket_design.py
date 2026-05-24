from typing import Any, Literal

from pydantic import BaseModel, Field

AgentEngine = Literal["openai", "rules"]


class CadStrategy(BaseModel):
    inner_surface: str = "loft_from_sections"
    section_source_field: str = "geometry_analysis.sections"
    offset_mode: str = "normal_2d"
    outer_surface: str = "offset_wall"


class OffsetSample(BaseModel):
    z_mm: float
    offset_mm: float


class BaseOffsets(BaseModel):
    interpolation: str = "linear"
    samples: list[OffsetSample]


class LocalModification(BaseModel):
    kind: str
    z_min_mm: float
    z_max_mm: float
    angle_start_deg: float
    angle_end_deg: float
    depth_mm: float
    clinical_reason: str


class WallThickness(BaseModel):
    proximal: float
    distal: float


class Ventilation(BaseModel):
    enabled: bool
    pattern: str
    count: int


class SocketStructure(BaseModel):
    wall_thickness_mm: WallThickness
    trim_height_mm: float
    socket_length_fraction: float
    ventilation: Ventilation


class SocketDesignSpec(BaseModel):
    type: str = "transtibial_custom_socket"
    cad_strategy: CadStrategy
    base_offsets: BaseOffsets
    local_modifications: list[LocalModification]
    structure: SocketStructure
    recommended_material: str
    fit_confidence: float = Field(..., ge=0, le=1)


class GeometryReference(BaseModel):
    height_mm: float
    section_count: int
    coordinate_system: dict[str, str]


class AgentQualityGate(BaseModel):
    passed: bool
    demo_eligible: bool
    mean_error_mm: float
    max_error_mm: float
    section_similarity: float
    volume_cm3: float | None = None
    volume_estimated: bool = False
    messages: list[str] = Field(default_factory=list)


class ClinicalReasoning(BaseModel):
    pain_consideration: str
    activity_adaptation: str
    skin_safety_notes: str
    contraindications: list[str]


class CadqueryHandoff(BaseModel):
    steps: list[str]
    target_fit_tolerance_mm: dict[str, float]
    design_mode: str


class SocketDesignAgentResponse(BaseModel):
    """Salida interna del agente (guardada en agent_response.json)."""

    quality_gate: AgentQualityGate
    geometry_reference: GeometryReference
    socket_design: SocketDesignSpec | None = None
    clinical_reasoning: ClinicalReasoning
    cadquery_handoff: CadqueryHandoff
    design_parameters: dict[str, Any] = Field(default_factory=dict)
    cad_geometry: dict[str, Any] | None = None


class SocketRunResponse(BaseModel):
    """Respuesta de POST /socket (pipeline completo)."""

    job_id: str
    case_id: str
    scan_file_path: str
    status: str
    quality_gate: dict[str, Any]
    download_urls: dict[str, str | None]
    artifacts: dict[str, str]
    cad_report: dict[str, Any] | None = None
