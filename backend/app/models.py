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
    UniqueConstraint,
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
    # One row per file per project; guarantees a failed probe cannot create a
    # duplicate of an already-flushed record (audit P0-02).
    __table_args__ = (
        UniqueConstraint("project_id", "source_path", name="uq_asset_project_path"),
    )

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
    # Fingerprint/config actually used for the last successful detect run, so an
    # unchanged file is not re-analysed (audit P1-04).
    analyzed_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    # Lightweight identity of the detected source, independent of hash mode.
    analyzed_source_signature: Mapped[str] = mapped_column(String(128), default='')
    analyzed_detect_config: Mapped[str] = mapped_column(String(255), default="")
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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

    # `detected_*` are immutable algorithm output and are the identity used to
    # match segments across re-analysis. `start_sec`/`end_sec` are the effective
    # bounds: they start equal to detected_* and may be trimmed by the user.
    # Keeping them separate is what stops a re-run from deleting a segment whose
    # in/out was edited (audit P0-03).
    detected_start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    detected_end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    is_trimmed: Mapped[bool] = mapped_column(Boolean, default=False)
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
    # Per-item failures recorded by a task; drives partial_failed (audit P1-08).
    failures: Mapped[dict] = mapped_column(JSON, default=dict)
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


class ReviewPackage(Base):
    """A generated, privacy-safe upload package (round-1 contact sheets etc.)."""

    __tablename__ = "review_packages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(32), default="")
    package_dir: Mapped[str] = mapped_column(Text, default="")  # relative to data dir
    status: Mapped[str] = mapped_column(String(32), default="ready")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewRequest(Base):
    """AI round-2 dynamic review request (imported from review_requests.json)."""

    __tablename__ = "review_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(String(36), index=True)
    segment_id: Mapped[str] = mapped_column(String(36), index=True)
    start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    end_sec: Mapped[float] = mapped_column(Float, default=0.0)
    priority: Mapped[str] = mapped_column(String(16), default="medium")
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="ai")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    proxy_ref: Mapped[str] = mapped_column(Text, default="")
    supersedes_request_id: Mapped[str] = mapped_column(String(36), default='')
    superseded_by_request_id: Mapped[str] = mapped_column(String(36), default='')
    disposition_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EditPlanShot(Base):
    """One confirmed shot in the final edit plan (M4)."""

    __tablename__ = "edit_plan_shots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    order: Mapped[int] = mapped_column(Integer, default=0)
    asset_id: Mapped[str] = mapped_column(String(36), default="")
    segment_id: Mapped[str] = mapped_column(String(36), default="")
    source_in_sec: Mapped[float] = mapped_column(Float, default=0.0)
    source_out_sec: Mapped[float] = mapped_column(Float, default=0.0)
    target_duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    timeline_start_sec: Mapped[float] = mapped_column(Float, default=0.0)
    story_role: Mapped[str] = mapped_column(String(32), default="")
    transition_hint: Mapped[str] = mapped_column(String(64), default="")
    music_cue: Mapped[str] = mapped_column(String(128), default="")
    audio_hint: Mapped[str] = mapped_column(String(128), default="")
    color_hint: Mapped[str] = mapped_column(String(128), default="")
    evidence: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    review_status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
