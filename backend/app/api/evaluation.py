"""TSB-371 — evaluation API: samples, protected preview clips, human ratings, A/B.

Every route here is **model-free**: browsing, rating, exporting and A/B comparison
must never trigger an external request. That is asserted, not assumed.

All routes are scoped to a project and refuse anything that does not belong to it
with 404 — including "does the file exist" probes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .deps import get_db
from ..database import begin_write
from ..models import Project
from ..services import evaluation_clips as clips
from ..services import evaluation_ab as ab
from ..services import evaluation_missing as missing_sources
from ..services import evaluation_report as report_mod
from ..services import evaluation_retire as retire
from ..services import evaluation_ratings as ratings
from ..services import evaluation_samples as samples

router = APIRouter()


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _clip_error(exc: "clips.ClipError") -> HTTPException:
    # The error carries its own status where the mapping is known at the source;
    # the table below stays as the fallback for the legacy codes.
    status = getattr(exc, "status", 400)
    if exc.code in (clips.ERROR_NO_FFMPEG,):
        status = 503
    elif exc.code in (clips.ERROR_NO_SOURCE, clips.ERROR_TOO_SHORT):
        status = 409
    elif exc.code in (clips.ERROR_COMMAND_FAILED, clips.ERROR_OUTPUT_NOT_CREATED):
        status = 500
    elif exc.code in ("EVAL_CLIP_NOT_FOUND", "EVAL_CLIP_NOT_GENERATED",
                      "EVAL_CLIP_ESCAPE", "EVAL_CLIP_BAD_ID"):
        status = 404
    return HTTPException(status_code=status,
                         detail={"code": exc.code, "message": exc.message})


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/evaluation/eligible-samples")
def eligible_samples(
    project_id: str,
    seed: str = Query(default="default", max_length=64),
    limit: int = Query(default=samples.DEFAULT_LIMIT, ge=1, le=samples.MAX_LIMIT),
    db: Session = Depends(get_db),
):
    """Which shots a human should look at, with their persisted evidence."""
    _project_or_404(db, project_id)
    return samples.collect_samples(db, project_id, seed=seed, limit=limit)


# ---------------------------------------------------------------------------
# Preview clips
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/evaluation/clips")
def create_clip(project_id: str, body: dict, db: Session = Depends(get_db)):
    """Generate (or reuse) a short silent preview clip for one shot.

    An optional ``start_sec``/``end_sec`` selects a sub-window — an A/B plan cuts
    pieces of a shot, and reviewing those from timecodes alone means judging the
    prose instead of the cut. Returns identifiers and the time mapping, never a
    filesystem path.
    """
    _project_or_404(db, project_id)
    segment_id = str(body.get("segment_id") or "")
    if not segment_id:
        raise HTTPException(status_code=422, detail="segment_id is required")

    start = body.get("start_sec")
    end = body.get("end_sec")
    try:
        record = clips.ensure_clip(
            db, project_id, segment_id,
            start_sec=None if start is None else float(start),
            end_sec=None if end is None else float(end),
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={
            "code": "EVAL_CLIP_BAD_WINDOW",
            "message": "start_sec/end_sec must be numbers"})
    except clips.ClipError as exc:
        raise _clip_error(exc)
    return clips.clip_status(db, project_id, record.clip_id)


@router.get("/projects/{project_id}/evaluation/clips/{clip_id}")
def clip_detail(project_id: str, clip_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return clips.clip_status(db, project_id, clip_id)
    except clips.ClipError as exc:
        raise _clip_error(exc)


@router.api_route("/projects/{project_id}/evaluation/clips/{clip_id}/media",
                  methods=["GET", "HEAD"])
def clip_media(project_id: str, clip_id: str, request: Request,
               db: Session = Depends(get_db)):
    """Stream the derived clip. Range is supported so the timeline is scrubbable."""
    _project_or_404(db, project_id)
    try:
        # Pass the session: the media route enforces freshness, not just
        # containment. A clip whose source changed must not stream as if current.
        path = clips.clip_media_path(project_id, clip_id, session=db)
    except clips.ClipError as exc:
        raise _clip_error(exc)

    from .media import _serve

    return _serve(path, media_type="video/mp4")


# ---------------------------------------------------------------------------
# Rating sessions
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/evaluation/sessions", status_code=201)
def create_rating_session(project_id: str, body: dict | None = None,
                          db: Session = Depends(get_db)):
    """Freeze the items a human will be asked to judge. Writes only evaluation rows."""
    _project_or_404(db, project_id)
    body = body or {}
    row = ratings.create_session(
        db, project_id,
        seed=str(body.get("seed") or "default"),
        limit=int(body.get("limit") or samples.DEFAULT_LIMIT),
        reviewer=str(body.get("reviewer") or "")[:64],
    )
    db.commit()
    return ratings.session_detail(db, project_id, row.id)


@router.get("/projects/{project_id}/evaluation/sessions/{session_id}")
def get_rating_session(project_id: str, session_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    try:
        return ratings.session_detail(db, project_id, session_id)
    except ratings.RatingError as exc:
        raise HTTPException(status_code=exc.status,
                            detail={"code": exc.code, "message": exc.message})


@router.patch("/projects/{project_id}/evaluation/sessions/{session_id}/ratings/{item_id}")
def patch_rating(project_id: str, session_id: str, item_id: str, body: dict,
                 db: Session = Depends(get_db)):
    """Save one human judgement. Idempotent; 409 if another tab already changed it."""
    _project_or_404(db, project_id)
    try:
        ratings.save_rating(db, project_id, session_id, item_id, body or {})
    except ratings.RatingError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status,
                            detail={"code": exc.code, "message": exc.message})
    db.commit()
    return ratings.session_detail(db, project_id, session_id)


@router.get("/projects/{project_id}/evaluation/sessions/{session_id}/summary")
def rating_summary(project_id: str, session_id: str, db: Session = Depends(get_db)):
    """Metrics from human ratings only. ``null`` when nobody has rated anything."""
    _project_or_404(db, project_id)
    try:
        return ratings.summarise(db, project_id, session_id)
    except ratings.RatingError as exc:
        raise HTTPException(status_code=exc.status,
                            detail={"code": exc.code, "message": exc.message})


# ---------------------------------------------------------------------------
# A/B blind comparison
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/evaluation/ab-sessions", status_code=201)
def create_ab(project_id: str, body: dict | None = None,
              db: Session = Depends(get_db)):
    """Pair an offline plan with a real model plan and fix the blind on the server.

    Two drafts must be supplied by the caller (they come from the preview endpoint,
    which the reviewer runs once per planner). The pairing rules refuse anything
    that is not a fair comparison — including a "model" plan with no call receipt.
    """
    _project_or_404(db, project_id)
    body = body or {}
    from ..services.director_plan_input import (
        DEFAULT_MAX_CANDIDATES, HARD_MAX_CANDIDATES, build_planning_input,
    )
    from ..services.director_plan_schema import PlanDraft, PlanningBrief
    from ..services.director_plan_rules import plan_offline

    try:
        offline = PlanDraft.model_validate(body.get("offline_plan") or {})
        model = PlanDraft.model_validate(body.get("model_plan") or {})
    except Exception as exc:
        raise HTTPException(status_code=422,
                            detail={"code": "AB_PLAN_INVALID", "message": str(exc)[:200]})

    snapshots = [p.request_summary.get("input_snapshot") for p in (offline, model)]
    if any(snapshot is not None for snapshot in snapshots):
        limit = snapshots[0].get("max_candidates") if isinstance(snapshots[0], dict) else None
        if (snapshots[0] != snapshots[1] or type(limit) is not int
                or not 1 <= limit <= HARD_MAX_CANDIDATES
                or offline.request_summary.get("brief") != model.request_summary.get("brief")):
            raise HTTPException(409, detail={"code": "AB_INPUT_DIGEST_MISMATCH",
                                            "message": "策划条件不同，请重新生成两份方案。"})
    else:
        limit = DEFAULT_MAX_CANDIDATES
    begin_write(db)
    planning_input = build_planning_input(project_id, max_candidates=limit, session=db)
    if snapshots[0] is not None and snapshots[0] != planning_input.audit_snapshot():
        raise HTTPException(409, detail={"code": "DIRECTOR_INPUT_CHANGED",
                                        "message": "素材或评价已变化，请重新生成两份方案。"})
    if snapshots[0] is not None:
        # Rebuild the deterministic arm instead of trusting a client-edited plan.
        brief_summary = offline.request_summary.get("brief")
        if not isinstance(brief_summary, dict):
            raise HTTPException(422, detail={"code": "AB_PLAN_INVALID"})
        brief_fields = dict(brief_summary)
        brief_fields.pop("theme_digest", None)  # Offline rules don't consume theme text.
        try:
            brief = PlanningBrief.model_validate(brief_fields)
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "AB_PLAN_INVALID"}) from exc
        expected = plan_offline(planning_input, brief)
        if expected.model_dump(exclude={"request_summary"}) != offline.model_dump(
                exclude={"request_summary"}):
            raise HTTPException(409, detail={"code": "AB_OFFLINE_PLAN_CHANGED",
                                            "message": "离线方案已修改，请重新生成。"})
    try:
        pair = ab.create_ab_session(db, project_id, offline, model, planning_input,
                                    reviewer=str(body.get("reviewer") or "")[:64])
    except ab.AbError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status,
                            detail={"code": exc.code, "message": exc.message})
    db.commit()
    return ab.current_view(db.get(type(pair.session), pair.session.id))


@router.get("/projects/{project_id}/evaluation/ab-results")
def ab_results(project_id: str, db: Session = Depends(get_db)):
    _project_or_404(db, project_id)
    return ab.experiment_results(db, project_id)


@router.get("/projects/{project_id}/evaluation/ab-sessions/{session_id}")
def get_ab(project_id: str, session_id: str, db: Session = Depends(get_db)):
    """The blind view. Never contains which side is which."""
    _project_or_404(db, project_id)
    row = ab.find_ab_session(db, project_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="evaluation session not found")
    # The server decides: a submitted session comes back revealed, so a refresh
    # restores what the reviewer already earned.
    return ab.current_view(row)


@router.post("/projects/{project_id}/evaluation/ab-sessions/{session_id}/submit")
def submit_ab(project_id: str, session_id: str, body: dict,
              db: Session = Depends(get_db)):
    """Record the blind preference, then return the revealed view."""
    _project_or_404(db, project_id)
    begin_write(db)
    row = ab.find_ab_session(db, project_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="evaluation session not found")
    try:
        ab.submit_preference(row, body or {})
        db.commit()
        return ab.revealed_view(row)
    except ab.AbError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status,
                            detail={"code": exc.code, "message": exc.message})


# ---------------------------------------------------------------------------
# Report export
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/evaluation/reports/{session_id}")
def export_report(project_id: str, session_id: str,
                  format: str = Query(default="json", pattern="^(json|csv|md)$"),
                  db: Session = Depends(get_db)):
    """Export the evaluation report. Read-only; no model call, no writes."""
    from fastapi.responses import PlainTextResponse

    _project_or_404(db, project_id)
    row = ratings.get_session(db, project_id, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="evaluation session not found")

    report = report_mod.build_report(db, project_id, session_id, row)
    if format == "md":
        return PlainTextResponse(report_mod.to_markdown(report),
                                 media_type="text/markdown; charset=utf-8")
    if format == "csv":
        return PlainTextResponse(report_mod.to_csv(report),
                                 media_type="text/csv; charset=utf-8")
    return report


# ---------------------------------------------------------------------------
# Which source files are no longer where we recorded them
# ---------------------------------------------------------------------------

@router.get("/projects/{project_id}/evaluation/missing-sources")
def list_missing_sources(project_id: str, db: Session = Depends(get_db)):
    """Name the footage that is not at its recorded location.

    Says "not found at the recorded location", not "deleted": a rename or an
    unmounted drive looks the same from here. Returns folder names only, never a
    full path.
    """
    _project_or_404(db, project_id)
    return missing_sources.missing_sources(db, project_id)


# ---------------------------------------------------------------------------
# Retire a source the user no longer has
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/evaluation/assets/{asset_id}/retire")
def retire_asset(project_id: str, asset_id: str, body: dict | None = None,
                 db: Session = Depends(get_db)):
    """Mark a source as gone for good.

    Nothing is deleted: the asset, its segments, its model readings and any
    annotation stay exactly as they are. Only the planning classification changes,
    with the distinct reason ``source_retired_by_user`` — these shots were never
    judged, the footage simply is not there.
    """
    _project_or_404(db, project_id)
    try:
        result = retire.retire_asset(db, project_id, asset_id,
                                     note=str((body or {}).get("note") or ""))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return result


@router.delete("/projects/{project_id}/evaluation/assets/{asset_id}/retire")
def unretire_asset(project_id: str, asset_id: str, db: Session = Depends(get_db)):
    """Undo a retirement, for when the file turns up again."""
    _project_or_404(db, project_id)
    try:
        result = retire.unretire_asset(db, project_id, asset_id)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc))
    db.commit()
    return result
