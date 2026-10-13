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
    id: str
    asset_id: str
    project_id: str
    start_sec: float
    end_sec: float
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
    id: str
    asset_id: str
    start_sec: float
    end_sec: float
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

class JobOut(ORMModel):
    id: str
    project_id: str
    job_type: str
    status: str
    stats: dict
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
    data_dir: str
    max_concurrency: int
    thumb_width: int
    proxy_width: int
    model_name: str
    model_configured: bool
