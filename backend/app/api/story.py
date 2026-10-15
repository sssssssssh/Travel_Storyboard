"""Story / AI endpoints: review-request import, round-2 proxies, direct model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import begin_write
from ..models import Asset, EditPlanShot, Job, Project, ReviewRequest, Segment, UserAnnotation, utcnow
from ..schemas import (
    ImportResult,
    JobCreate,
    JobOut,
    ReviewRequestOut,
    ReviewRequestsImport,
    ReviewDisposition,
    StoryAnalyzeRequest,
)
from ..services.job_service import (
    InvalidJobType,
    JobConflict,
    JobRequest,
    ProjectNotFound,
    create_project_job,
)
from ..services.runner import runner
from ..services.story_planner import direct_analyze, import_review_requests
from ..services.vision.authorization import authorize_upload
from ..services.vision.errors import ModelError
from ..services.review_validation import (ReviewValidationError, validate_review, verified_source,
                                          INVALID_SEGMENT_STATUSES)
from .deps import get_db

router = APIRouter(tags=["story"])


@router.post("/projects/{project_id}/review-requests/import", response_model=ImportResult)
def import_review_requests_ep(
    project_id: str, payload: ReviewRequestsImport, db: Session = Depends(get_db)
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return import_review_requests(project_id, payload.model_dump())


def _segment_context(segment: Segment, notes: dict, assets: dict) -> dict:
    asset = assets.get(segment.asset_id)
    return {'id': segment.id, 'asset_id': segment.asset_id,
            'display_name': asset.display_name if asset else '',
            'start_sec': segment.start_sec, 'end_sec': segment.end_sec,
            'detected_start_sec': segment.detected_start_sec,
            'detected_end_sec': segment.detected_end_sec,
            'user_rating': segment.user_rating, 'story_role': segment.story_role,
            'note': notes.get(segment.id, ''), 'status': segment.status,
            'change_reason': (segment.quality_metrics or {}).get('analysis_warning', ''),
            'thumbnail': (segment.frame_refs or [None])[0]}


@router.get("/projects/{project_id}/review-requests", response_model=list[ReviewRequestOut])
def list_review_requests(project_id: str, db: Session = Depends(get_db)):
    rows = db.scalars(select(ReviewRequest).where(ReviewRequest.project_id == project_id)
                      .order_by(ReviewRequest.created_at.desc())).all()
    segments = {g.id: g for g in db.scalars(select(Segment).where(Segment.project_id == project_id))}
    assets = {a.id: a for a in db.scalars(select(Asset).where(Asset.project_id == project_id))}
    notes = dict(db.execute(select(UserAnnotation.segment_id, UserAnnotation.note)
                           .where(UserAnnotation.segment_id.in_(segments))).all())
    plans: dict[str, list[str]] = {}
    for shot in db.scalars(select(EditPlanShot).where(EditPlanShot.project_id == project_id)):
        plans.setdefault(shot.segment_id, []).append(shot.id)
    source_errors = {}
    for asset in assets.values():
        try:
            verified_source(asset)
        except ReviewValidationError as exc:
            source_errors[asset.id] = {'error_code': exc.code, 'message': str(exc)}
    candidates: dict[str, list[dict]] = {}
    for seg in segments.values():
        if seg.status in INVALID_SEGMENT_STATUSES or seg.asset_id in source_errors:
            continue
        try:
            validate_review(db, project_id, seg.asset_id, seg.id, seg.start_sec, seg.end_sec)
        except ReviewValidationError:
            continue
        candidates.setdefault(seg.asset_id, []).append(_segment_context(seg, notes, assets))
    result = []
    for row in rows:
        error = source_errors.get(row.asset_id)
        try:
            validate_review(db, project_id, row.asset_id, row.segment_id, row.start_sec, row.end_sec)
        except ReviewValidationError as exc:
            error = {'error_code': exc.code, 'message': str(exc)}
        segment = segments.get(row.segment_id)
        result.append(ReviewRequestOut.model_validate(row).model_copy(update={
            'validation_error': error,
            'segment_context': _segment_context(segment, notes, assets) if segment else None,
            'current_segments': candidates.get(row.asset_id, []),
            'plan_shot_ids': plans.get(row.segment_id, []),
        }))
    return result


@router.post('/projects/{project_id}/review-requests/{request_id}/disposition',
             response_model=ReviewRequestOut)
def dispose_review_request(project_id: str, request_id: str, payload: ReviewDisposition,
                           db: Session = Depends(get_db)):
    begin_write(db)
    old = db.get(ReviewRequest, request_id)
    if old is None or old.project_id != project_id:
        raise HTTPException(status_code=404, detail='复核请求不存在')
    if old.status in ('dismissed', 'superseded'):
        raise HTTPException(status_code=409, detail='请求已关闭；历史数据与预览已保留')
    try:
        if payload.action == 'replace':
            asset, segment, start, end = validate_review(
                db, project_id, payload.asset_id, payload.segment_id, payload.start_sec, payload.end_sec)
            verified_source(asset)
            if segment.id == old.segment_id and start == old.start_sec and end == old.end_sec:
                raise ReviewValidationError('UNCHANGED_REQUEST', '请选择新的镜头或区间；重试原请求请使用重试操作')
            new = ReviewRequest(project_id=project_id, asset_id=asset.id, segment_id=segment.id,
                                start_sec=start, end_sec=end, priority=old.priority,
                                reason=old.reason, source='human', supersedes_request_id=old.id)
            db.add(new); db.flush()
            old.status, old.superseded_by_request_id = 'superseded', new.id
            old.disposition_at = utcnow()
            result = new
        elif payload.action == 'retry':
            asset, _, _, _ = validate_review(db, project_id, old.asset_id, old.segment_id,
                                             old.start_sec, old.end_sec)
            verified_source(asset)
            old.status, old.disposition_at = 'pending', utcnow()
            result = old
        else:
            old.status = 'needs_review' if payload.action == 'defer' else 'dismissed'
            old.disposition_at = utcnow()
            result = old
    except ReviewValidationError as exc:
        raise HTTPException(status_code=409, detail={'error_code': exc.code, 'message': str(exc)}) from exc
    db.commit(); db.refresh(result)
    return result


@router.post("/projects/{project_id}/review-requests/proxies", response_model=JobOut, status_code=202)
def build_proxies(
    project_id: str, body: JobCreate | None = None, db: Session = Depends(get_db)
):
    """Enqueue a round-2 proxy job.

    Goes through the shared job service (TSB-242) so it honours the same
    per-project exclusivity as every other heavy job. Previously this endpoint
    inserted a Job directly, so rapid clicks could queue several of them.

    Request/response shape is unchanged: 202 with the created JobOut.
    """
    options = (body.options if body else None) or {}
    try:
        return create_project_job(
            db,
            project_id,
            JobRequest(job_type="proxies", options=options, force=False),
            config_builder=_proxy_job_config,
        )
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc
    except InvalidJobType as exc:  # pragma: no cover - fixed job_type
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _proxy_job_config(project: Project, request: JobRequest) -> dict:
    """Config snapshot for the proxy job (unchanged from the previous inline dict)."""
    return {"max_total_mb": request.options.get("max_total_mb", 300)}


@router.post("/projects/{project_id}/story/analyze")
def analyze_story(
    project_id: str,
    body: StoryAnalyzeRequest | None = None,
    db: Session = Depends(get_db),
):
    """Legacy direct-model analysis (M3).

    TSB-312: this endpoint used to upload frames as soon as a base URL and key
    were configured — it only checked ``model_client.is_configured()``, so merely
    configuring a key *silently* authorised uploads. It now goes through the same
    shared authorization boundary as the newer ``vision_analyze`` job, so it
    requires ``TRAVELSB_MODEL_MODE=on`` **and** an explicit
    ``"upload_acknowledged": true`` in the body.

    A caller that sends no body gets a stable ``MODEL_UPLOAD_DENIED`` plus a
    migration hint instead of an upload. The newer entry point is
    ``POST /api/projects/{id}/jobs/vision_analyze``.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    acknowledged = bool(body.upload_acknowledged) if body else False
    try:
        context = authorize_upload(
            project_id=project_id,
            upload_acknowledged=acknowledged,
            purpose="story_analyze",
        )
    except ModelError as exc:
        raise HTTPException(
            status_code=403 if exc.code == "MODEL_UPLOAD_DENIED" else 422,
            detail={
                "error_code": exc.code,
                "message": str(exc),
                "migration_hint": (
                    "旧接口 /story/analyze 需要请求体 "
                    '{"upload_acknowledged": true}。'
                    "推荐迁移到 POST /api/projects/{id}/jobs/vision_analyze，"
                    "它提供逐镜头进度、可取消任务与覆盖率统计。"
                ),
            },
        ) from exc

    return direct_analyze(project_id, authorization=context)
