"""SQLAlchemy ORM models.

Design notes:
- asset_id / segment_id are stable UUIDs (never derived from path order).
- Times are stored as float seconds (source timeline), never rounded HH:MM:SS.
- JSON columns hold structured dicts/lists (quality metrics, tags, refs).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    source_roots: Mapped[list] = mapped_column(JSON, default=list)
    output_dir: Mapped[str] = mapped_column(Text, default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    source_path: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(512), default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, default=0)
    quick_fingerprint: Mapped[str] = mapped_column(String(128), default="")

    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    fps_num: Mapped[int] = mapped_column(Integer, default=0)
    fps_den: Mapped[int] = mapped_column(Integer, default=0)
    vfr_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    codec: Mapped[str] = mapped_column(String(64), default="")
    pixel_format: Mapped[str] = mapped_column(String(64), default="")
    color_primaries: Mapped[str] = mapped_column(String(64), default="")
    color_transfer: Mapped[str] = mapped_column(String(64), default="")
    color_space: Mapped[str] = mapped_column(String(64), default="")
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    audio_codec: Mapped[str] = mapped_column(String(64), default="")

    scan_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    analysis_version: Mapped[str] = mapped_column(String(32), default="")
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Segment(Base):
    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    boundary_reason: Mapped[str] = mapped_column(String(32), default="")
    quality_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    content_tags: Mapped[dict] = mapped_column(JSON, default=dict)
    story_role: Mapped[str] = mapped_column(String(32), default="")
    user_rating: Mapped[str] = mapped_column(String(32), default="pending")
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    frame_refs: Mapped[list] = mapped_column(JSON, default=list)
    proxy_ref: Mapped[str] = mapped_column(Text, default="")
    parent_segment_id: Mapped[str] = mapped_column(String(36), default="")
    algorithm_version: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Frame(Base):
    __tablename__ = "frames"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("segments.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )

    time_sec: Mapped[float] = mapped_column(Float, default=0.0)
    image_path: Mapped[str] = mapped_column(Text, default="")
    phash: Mapped[str] = mapped_column(String(64), default="")
    is_representative: Mapped[bool] = mapped_column(Boolean, default=False)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    resume_token: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserAnnotation(Base):
    __tablename__ = "user_annotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("segments.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[str] = mapped_column(String(32), default="pending")
    note: Mapped[str] = mapped_column(Text, default="")
    in_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    out_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
