"""M5 vision API (TSB-304, TSB-306).

Endpoints are deliberately thin: they validate inputs, delegate to the services
and never expose a filesystem path, a credential or a raw model response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Asset,
    Job,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticAttempt,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from ..schemas import (
    JobOut,
    MotionCandidateOut,
    MotionReviewOut,
    MotionReviewRequest,
    MotionVerdictRequest,
    VideoAnalyzeRequest,
    VideoCoverageOut,
    VideoObservationOut,
    VisionAnalyzeRequest,
    VisionPreviewOut,
    VisionResultOut,
    VisionStatusOut,
)
from ..services.job_service import (
    InvalidJobType,
    JobConflict,
    JobRequest,
    ProjectNotFound,
    create_project_job,
    locking_job_for,
)
from ..services.runner import ACTIVE_STATUSES
from ..services.vision import capabilities as caps_mod
from ..services.vision import is_configured
from ..services.vision import semantic
from ..services.vision.video_analysis import coverage_report, observation_payload
from ..services.vision.video_job import video_capability_refusal
from ..services.vision.video_schema import VIDEO_PROMPT_VERSION, VIDEO_SCHEMA_VERSION
from .deps import get_db


router = APIRouter(tags=["vision"])


def _vision_config(project: Project, request: JobRequest) -> dict:
    """Config snapshot for a ``vision_analyze`` job.

    The consent flag is recorded in the snapshot so a later review can tell
    whether the upload was actually authorised at the time it ran.
    """
    options = request.options or {}
    return {
        "limit": int(options.get("limit") or get_settings().vision_max_segments_per_job),
        "segment_ids": list(options.get("segment_ids") or []),
        "force": bool(request.force),
        "upload_acknowledged": bool(options.get("upload_acknowledged")),
        "prompt_version": get_settings().vision_prompt_version,
    }


@router.get("/vision/status", response_model=VisionStatusOut)
def vision_status() -> VisionStatusOut:
    """Capability and configuration summary.

    Returns booleans and limits only: never the base URL, key or any local path.
    """
    settings = get_settings()
    caps = caps_mod.declared_capabilities()
    return VisionStatusOut(
        configured=is_configured(),
        mode=settings.model_mode,
        model_name=settings.model_name or "",
        prompt_version=settings.vision_prompt_version,
        schema_version=settings.vision_schema_version,
        supports_images=caps.supports_images,
        supports_multi_image=caps.supports_multi_image,
        supports_json_schema=caps.supports_json_schema,
        supports_native_video=caps.supports_native_video,
        max_images=caps.max_images,
        max_calls_per_job=caps.max_calls_per_job,
        allow_insecure_http=settings.model_allow_insecure_http,
    )


@router.post("/projects/{project_id}/vision/preview", response_model=VisionPreviewOut)
def vision_preview(
    project_id: str,
    body: VisionAnalyzeRequest | None = None,
    db: Session = Depends(get_db),
):
    """What an upload *would* send, before anything leaves the machine.

    Implements the task book's "show the number of images, request count and
    purpose before uploading" requirement. Performs no model call.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    options = body or VisionAnalyzeRequest()
    candidates = semantic.select_candidates(
        project_id,
        limit=int(options.limit or get_settings().vision_max_segments_per_job),
        segment_ids=list(options.segment_ids or []) or None,
    )
    images = sum(min(len(c.frame_ids), get_settings().model_max_images) for c in candidates)
    coverage = semantic.coverage_report(project_id, candidates, images)
    return VisionPreviewOut(
        model_name=get_settings().model_name or "",
        configured=is_configured(),
        segments_to_analyze=len(candidates),
        estimated_images=images,
        estimated_requests=len(candidates),
        coverage=coverage,
        sends_source_video=False,
        strips_metadata=True,
    )


@router.post("/projects/{project_id}/jobs/vision_analyze", response_model=JobOut, status_code=202)
def start_vision_analyze(
    project_id: str,
    body: VisionAnalyzeRequest | None = None,
    db: Session = Depends(get_db),
):
    """Queue a semantic-analysis job (202).

    Reuses ``create_project_job`` so project mutex, cancellation and the
    ``cancelling`` state machine behave exactly like every other job type.
    """
    options = (body.model_dump() if body else {}) or {}
    try:
        return create_project_job(
            db,
            project_id,
            JobRequest(job_type="vision_analyze", options=options,
                       force=bool(options.get("force"))),
            config_builder=_vision_config,
        )
    except InvalidJobType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc


@router.get("/projects/{project_id}/vision-results")
def list_vision_results(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_only: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Paged semantic results for a project."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    stmt = select(SegmentSemanticResult).where(
        SegmentSemanticResult.project_id == project_id
    )
    if current_only:
        stmt = stmt.where(SegmentSemanticResult.is_current.is_(True))
    rows = db.scalars(
        stmt.order_by(SegmentSemanticResult.created_at.desc()).limit(limit).offset(offset)
    ).all()

    return {
        "limit": limit,
        "offset": offset,
        "count": len(rows),
        "items": [_result_out(r) for r in rows],
    }


@router.get("/segments/{segment_id}/vision-result", response_model=VisionResultOut)
def get_vision_result(segment_id: str, db: Session = Depends(get_db)):
    """The latest semantic reading of one shot, with provenance."""
    row = db.scalars(
        select(SegmentSemanticResult)
        .where(
            SegmentSemanticResult.segment_id == segment_id,
            SegmentSemanticResult.is_current.is_(True),
        )
        .order_by(SegmentSemanticResult.created_at.desc())
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No vision result for this segment")
    return _result_out(row)


@router.get("/projects/{project_id}/vision-candidates", response_model=list[MotionCandidateOut])
def list_motion_candidates(project_id: str, db: Session = Depends(get_db)):
    """TSB-306 — shots worth a dynamic (multi-frame) review.

    Sources are combined and labelled: a semantic ``needs_motion_review`` flag,
    a rule-based frame-difference hint, or a shot with too few frames to judge
    motion at all. A rule hint is presented as "suspected change, needs
    verification" — never as a proven shake (the previous tool over-claimed
    exactly that).
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    from ..services.vision.review_candidates import build_candidates

    return build_candidates(db, project_id)


@router.get("/segments/{segment_id}/vision-result/attempts")
def semantic_attempt_history(segment_id: str, limit: int = Query(20, ge=1, le=100),
                             offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    if db.get(Segment, segment_id) is None:
        raise HTTPException(404, "Segment not found")
    rows = db.scalars(select(SegmentSemanticAttempt).where(
        SegmentSemanticAttempt.segment_id == segment_id,
    ).order_by(SegmentSemanticAttempt.created_at.desc()).limit(limit).offset(offset)).all()
    items = [{"id": r.id, "job_id": r.job_id, "result_id": r.result_id,
              "source_signature": r.source_signature, "frame_manifest_hash": r.frame_manifest_hash,
              "analysis": r.analysis_json, "evidence": r.evidence_json,
              "usage": r.request_usage, "created_at": r.created_at} for r in rows]
    return {"limit": limit, "offset": offset, "count": len(items), "items": items}


# --- TSB-307: timestamped multi-frame motion review -----------------------

def _motion_config(project: Project, request: JobRequest) -> dict:
    options = request.options or {}
    return {
        "segment_ids": list(options.get("segment_ids") or []),
        "frame_count": int(options.get("frame_count") or get_settings().motion_frame_count),
        "limit": int(options.get("limit") or get_settings().vision_max_segments_per_job),
        "upload_acknowledged": bool(options.get("upload_acknowledged")),
    }


@router.post("/projects/{project_id}/jobs/motion_review", response_model=JobOut, status_code=202)
def start_motion_review(
    project_id: str,
    body: MotionReviewRequest | None = None,
    db: Session = Depends(get_db),
):
    """Queue a multi-frame dynamic review (202).

    Deliberately a *job*, not a long synchronous HTTP call: it must be
    cancellable and must not block the request thread while frames are decoded.
    """
    options = (body.model_dump() if body else {}) or {}
    try:
        return create_project_job(
            db,
            project_id,
            JobRequest(job_type="motion_review", options=options, force=True),
            config_builder=_motion_config,
        )
    except InvalidJobType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc


@router.get("/projects/{project_id}/motion-reviews", response_model=list[MotionReviewOut])
def list_motion_reviews(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    rows = db.scalars(
        select(SegmentMotionReview)
        .where(
            SegmentMotionReview.project_id == project_id,
            SegmentMotionReview.is_current.is_(True),
        )
        .order_by(SegmentMotionReview.created_at.desc())
        .limit(limit)
    ).all()
    return [_motion_out(r, db) for r in rows]


@router.get("/segments/{segment_id}/motion-review", response_model=MotionReviewOut)
def get_motion_review(segment_id: str, db: Session = Depends(get_db)):
    row = db.scalars(
        select(SegmentMotionReview)
        .where(
            SegmentMotionReview.segment_id == segment_id,
            SegmentMotionReview.is_current.is_(True),
        )
        .order_by(SegmentMotionReview.created_at.desc())
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No motion review for this segment")
    return _motion_out(row, db)


@router.get("/segments/{segment_id}/motion-review/verdicts")
def motion_verdict_history(segment_id: str, limit: int = Query(20, ge=1, le=100),
                           offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    from ..services.vision.verdicts import human_projection
    row = db.scalars(select(SegmentMotionReview).where(
        SegmentMotionReview.segment_id == segment_id,
        SegmentMotionReview.is_current.is_(True),
    )).first()
    if row is None:
        raise HTTPException(404, "No motion review for this segment")
    items = human_projection(db, row, limit=limit, offset=offset)["human_history"]
    return {"limit": limit, "offset": offset, "count": len(items), "items": items}


@router.post("/segments/{segment_id}/motion-review/verdict", response_model=MotionReviewOut)
def set_motion_verdict(
    segment_id: str,
    body: MotionVerdictRequest,
    db: Session = Depends(get_db),
):
    """Record a human verdict on a motion reading (C4).

    The model's own conclusion stays in the row untouched; a re-run never clears
    this field, which is what keeps "AI proposes, a person decides" structurally
    true rather than merely intended.
    """
    from ..services.vision.motion import record_human_verdict
    from ..services.vision.verdicts import VerdictConflict
    try:
        found = record_human_verdict(segment_id, body.verdict, body.note or "",
                                     motion_review_id=body.motion_review_id,
                                     input_signature=body.input_signature)
        if not found:
            raise HTTPException(404, "No motion review for this segment")
    except VerdictConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.rollback()
    refreshed = db.scalars(
        select(SegmentMotionReview).where(
            SegmentMotionReview.segment_id == segment_id,
            SegmentMotionReview.is_current.is_(True),
        )
    ).first()
    if refreshed is None:
        raise HTTPException(409, "Motion review changed; refresh before confirming")
    return _motion_out(refreshed, db)


def _motion_out(row: SegmentMotionReview, db: Session) -> MotionReviewOut:
    from ..services.vision.verdicts import human_projection
    return MotionReviewOut(
        id=row.id,
        segment_id=row.segment_id,
        asset_id=row.asset_id,
        method=row.method,
        status=row.status,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        start_sec=row.start_sec,
        end_sec=row.end_sec,
        motion_type=row.motion_type,
        stability_risk=row.stability_risk,
        action_summary=row.action_summary,
        analysis=dict(row.analysis_json or {}),
        evidence=dict(row.evidence_json or {}),
        request_usage=dict(row.request_usage or {}),
        is_current=row.is_current,
        input_signature=row.input_signature,
        **human_projection(db, row),
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_out(row: SegmentSemanticResult) -> VisionResultOut:
    evidence = dict(row.evidence_json or {})
    return VisionResultOut(
        id=row.id,
        segment_id=row.segment_id,
        asset_id=row.asset_id,
        status=row.status,
        model_provider=row.model_provider,
        model_id=row.model_id,
        prompt_version=row.prompt_version,
        schema_version=row.schema_version,
        source_signature=row.source_signature,
        visual=dict(row.analysis_json or {}),
        evidence=evidence,
        request_usage=dict(row.request_usage or {}),
        is_current=row.is_current,
        error_code=row.error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# TSB-328/329 — native-video observations
# ---------------------------------------------------------------------------

def _video_config(project: Project, request: JobRequest) -> dict:
    """Config snapshot for a ``video_analyze`` job.

    Everything the task later needs is frozen here, including the consent flag and
    the fallback permission. Freezing them means a settings change between request
    and execution cannot widen what the job is allowed to send.
    """
    options = request.options or {}
    settings = get_settings()
    return {
        "mode": str(options.get("mode") or "video_native"),
        "limit": int(options.get("limit") or settings.vision_max_segments_per_job),
        "segment_ids": list(options.get("segment_ids") or []),
        "force": bool(request.force),
        "upload_acknowledged": bool(options.get("upload_acknowledged")),
        "allow_fallback": bool(options.get("allow_fallback")),
        "max_clip_sec": float(options.get("max_clip_sec") or 15.0),
        "max_segments": int(options.get("max_segments") or 20),
        "max_total_sec": float(options.get("max_total_sec") or 300.0),
        "max_total_bytes": int(options.get("max_total_bytes") or 120_000_000),
        # TSB-353: freeze the per-call HTTP limits in the snapshot. Without these
        # the job's ``VideoBudget`` fell back to process-wide settings, so what the
        # snapshot recorded and what the provider actually did could differ.
        # ``None`` (absent) means "inherit the ceiling".
        "max_calls": options.get("max_calls"),
        "max_retries": options.get("max_retries"),
        "timeout_sec": options.get("timeout_sec"),
        "prompt_version": VIDEO_PROMPT_VERSION,
        "schema_version": VIDEO_SCHEMA_VERSION,
    }


@router.post("/projects/{project_id}/jobs/video_analyze", response_model=JobOut, status_code=202)
def start_video_analyze(
    project_id: str,
    body: VideoAnalyzeRequest | None = None,
    db: Session = Depends(get_db),
):
    """Queue a native-video analysis job (202).

    A job rather than a synchronous call because clip derivation is real FFmpeg
    work and the model call can take minutes: it must be cancellable and must hold
    the project's exclusivity like every other heavy task.

    Capability is checked **here**, before the job is enqueued. Letting an
    unavailable channel through and failing later inside the worker would be worse
    than useless: the UI would show a queued job, the project lock would be taken,
    and the user would learn only afterwards that video was never possible. The
    task book requires the button to be disabled and the request refused (V03).

    Order matters: an already-running job is reported as a conflict **before** the
    capability verdict. Both are true in that situation, but "wait for the current
    job" is the immediately actionable one, and answering 422 would hide the fact
    that the project is busy at all.
    """
    options = (body.model_dump() if body else {}) or {}

    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if (blocking := locking_job_for(project_id)) is not None:
        raise HTTPException(status_code=409, detail=JobConflict(blocking).detail())

    refusal = video_capability_refusal(
        mode=str(options.get("mode") or "video_native"),
        allow_fallback=bool(options.get("allow_fallback")),
    )
    if refusal is not None:
        raise HTTPException(status_code=422, detail=refusal)

    try:
        return create_project_job(
            db,
            project_id,
            JobRequest(job_type="video_analyze", options=options,
                       force=bool(options.get("force"))),
            config_builder=_video_config,
        )
    except InvalidJobType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc


@router.get("/projects/{project_id}/video-observations",
            response_model=list[VideoObservationOut])
def list_video_observations(
    project_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    include_history: bool = Query(default=False),
    db: Session = Depends(get_db),
):
    """Current video observations for a project.

    ``include_history`` exposes superseded versions for audit; the default is the
    live reading per shot, which is what the three-way comparison view needs.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    statement = select(SegmentVideoObservation).where(
        SegmentVideoObservation.project_id == project_id
    )
    if not include_history:
        statement = statement.where(SegmentVideoObservation.is_current.is_(True))
    rows = db.scalars(
        statement.order_by(SegmentVideoObservation.created_at.desc()).limit(limit)
    ).all()
    return [VideoObservationOut(**observation_payload(row)) for row in rows]


@router.get("/segments/{segment_id}/video-observation",
            response_model=VideoObservationOut)
def get_video_observation(segment_id: str, db: Session = Depends(get_db)):
    """The live video reading for one shot, or 404."""
    row = db.scalars(
        select(SegmentVideoObservation)
        .where(
            SegmentVideoObservation.segment_id == segment_id,
            SegmentVideoObservation.is_current.is_(True),
        )
        .order_by(SegmentVideoObservation.created_at.desc())
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No video observation for this segment")
    return VideoObservationOut(**observation_payload(row))


@router.get("/segments/{segment_id}/video-observation/history",
            response_model=list[VideoObservationOut])
def video_observation_history(
    segment_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Every retained version for one shot, newest first (audit trail)."""
    rows = db.scalars(
        select(SegmentVideoObservation)
        .where(SegmentVideoObservation.segment_id == segment_id)
        .order_by(SegmentVideoObservation.created_at.desc())
        .limit(limit)
    ).all()
    return [VideoObservationOut(**observation_payload(row)) for row in rows]


@router.get("/projects/{project_id}/video-coverage", response_model=VideoCoverageOut)
def video_coverage(project_id: str, db: Session = Depends(get_db)):
    """Coverage of the video channel, reported as a subset of the library.

    Deliberately not a "success" summary: it states how many shots have a reading
    and how many do not, so a partial analysis cannot be read as a full one.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return VideoCoverageOut(**coverage_report(project_id))

