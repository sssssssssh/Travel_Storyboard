"""Pydantic request / response schemas for the REST API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Projects -----------------------------------------------------------

class ProjectCreate(BaseModel):
    name: str
    source_roots: list[str] = Field(default_factory=list)
    output_dir: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdate(BaseModel):
    name: str | None = None
    source_roots: list[str] | None = None
    output_dir: str | None = None
    settings: dict[str, Any] | None = None


class ProjectOut(ORMModel):
    id: str
    name: str
    source_roots: list
    output_dir: str
    settings: dict
    created_at: datetime
    updated_at: datetime


class ProjectStats(BaseModel):
    asset_count: int = 0
    asset_succeeded: int = 0
    asset_failed: int = 0
    segment_count: int = 0
    total_duration_sec: float = 0.0
    total_size_bytes: int = 0


class ProjectDetail(ProjectOut):
    stats: ProjectStats = Field(default_factory=ProjectStats)
    #: Where derived artifacts actually land (audit P1-02). Equals
    #: `<output_dir>/<project_id>` when an output dir is configured, otherwise
    #: `<data_dir>/projects/<project_id>`.
    cache_dir: str = ""


# --- Assets -------------------------------------------------------------

class AssetOut(ORMModel):
    id: str
    project_id: str
    source_path: str
    display_name: str
    file_size_bytes: int
    mtime_ns: int
    duration_sec: float
    fps_num: int
    fps_den: int
    vfr_flag: bool
    width: int
    height: int
    rotation: int
    codec: str
    pixel_format: str
    color_primaries: str
    color_transfer: str
    color_space: str
    has_audio: bool
    audio_codec: str
    scan_status: str
    error_code: str
    error_message: str
    analysis_version: str
    scanned_at: datetime | None


class AssetListItem(BaseModel):
    id: str
    display_name: str
    source_path: str
    file_size_bytes: int
    duration_sec: float
    width: int
    height: int
    scan_status: str
    error_code: str
    segment_count: int = 0


class AssetListOut(BaseModel):
    items: list[AssetListItem]
    total: int
    offset: int
    limit: int


# --- Segments -----------------------------------------------------------

class SegmentOut(ORMModel):
    note: str = ""
    id: str
    asset_id: str
    project_id: str
    start_sec: float
    end_sec: float
    detected_start_sec: float = 0.0
    detected_end_sec: float = 0.0
    is_trimmed: bool = False
    boundary_reason: str
    quality_metrics: dict
    content_tags: dict
    story_role: str
    user_rating: str
    status: str
    frame_refs: list
    proxy_ref: str
    algorithm_version: str
    created_at: datetime


class SegmentListItem(BaseModel):
    note: str = ""
    status: str = "candidate"
    id: str
    asset_id: str
    start_sec: float
    end_sec: float
    detected_start_sec: float = 0.0
    detected_end_sec: float = 0.0
    is_trimmed: bool = False
    duration_sec: float
    boundary_reason: str
    user_rating: str
    story_role: str
    quality_metrics: dict
    content_tags: dict
    thumbnail: str | None = None
    asset_display_name: str = ""


class SegmentListOut(BaseModel):
    items: list[SegmentListItem]
    total: int
    offset: int
    limit: int


class AnnotateRequest(BaseModel):
    rating: str | None = None  # like / favorite / exclude / pending
    note: str | None = None
    in_sec: float | None = None
    out_sec: float | None = None
    story_role: str | None = None


# --- Jobs ---------------------------------------------------------------

class JobCreate(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class JobOut(ORMModel):
    id: str
    project_id: str
    job_type: str
    status: str
    stats: dict
    failures: dict = Field(default_factory=dict)
    resume_token: dict
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


# --- System -------------------------------------------------------------

class HealthOut(BaseModel):
    status: str
    version: str
    ffmpeg: str | None
    ffprobe: str | None
    ffmpeg_available: bool


class SystemConfigOut(BaseModel):
    bind_host: str
    bind_port: int
    #: Folder NAME only — this endpoint is token-exempt, so it must not disclose
    #: the absolute data directory (audit P1-07 class of leak).
    data_dir_name: str
    max_concurrency: int
    thumb_width: int
    proxy_width: int
    model_name: str
    model_configured: bool


# --- Review packages (M3) ------------------------------------------------

class PackageBuildOptions(BaseModel):
    # Restrict the package to segments with this rating ('' = all except 'exclude').
    rating: str = ""
    contact_sheet_cols: int = 5
    contact_sheet_rows: int = 4


class ReviewPackageOut(ORMModel):
    id: str
    project_id: str
    version: str
    package_dir: str
    status: str
    file_count: int
    total_size_bytes: int
    created_at: datetime


# --- Review requests (M3) ------------------------------------------------

class ReviewRequestItem(BaseModel):
    asset_id: str
    segment_id: str
    start_sec: float
    end_sec: float
    priority: str = "medium"
    reason: str = ""


class ReviewRequestsImport(BaseModel):
    schema_version: str = "2.0"
    project_id: str = ""
    requests: list[ReviewRequestItem] = Field(default_factory=list)


class ImportErrorItem(BaseModel):
    index: int
    message: str


class ImportResult(BaseModel):
    created: int
    errors: list[ImportErrorItem] = Field(default_factory=list)


class ReviewRequestOut(ORMModel):
    id: str
    project_id: str
    asset_id: str
    segment_id: str
    start_sec: float
    end_sec: float
    priority: str
    reason: str
    source: str
    status: str
    proxy_ref: str
    created_at: datetime


# --- Edit plan (M4) ------------------------------------------------------

class EditPlanShotIn(BaseModel):
    #: Ignored for ordering — the position in ``shots`` is authoritative
    #: (audit P1-05). Kept optional so existing clients still validate.
    order: int | None = None
    asset_id: str
    segment_id: str
    source_in_sec: float
    source_out_sec: float
    target_duration_sec: float = 0.0
    story_role: str = ""
    transition_hint: str = ""
    music_cue: str = ""
    audio_hint: str = ""
    color_hint: str = ""
    evidence: str = ""
    confidence: float = 0.0
    review_status: str = "pending"


class EditPlanBuild(BaseModel):
    shots: list[EditPlanShotIn] = Field(default_factory=list)


class EditPlanShotOut(ORMModel):
    id: str
    project_id: str
    order: int
    asset_id: str
    segment_id: str
    source_in_sec: float
    source_out_sec: float
    target_duration_sec: float
    timeline_start_sec: float
    story_role: str
    transition_hint: str
    music_cue: str
    audio_hint: str
    color_hint: str
    evidence: str
    confidence: float
    review_status: str
