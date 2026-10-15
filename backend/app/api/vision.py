"""M5 vision API (TSB-304, TSB-306).

Endpoints are deliberately thin: they validate inputs, delegate to the services
and never expose a filesystem path, a credential or a raw model response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Asset, Job, Project, Segment, SegmentSemanticResult
from ..schemas import (
    JobOut,
    MotionCandidateOut,
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
)
from ..services.runner import ACTIVE_STATUSES
from ..services.vision import capabilities as caps_mod
from ..services.vision import is_configured
from ..services.vision import semantic
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
