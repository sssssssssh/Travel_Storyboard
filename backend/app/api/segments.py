"""Segment listing / detail / annotation endpoints.

Trimming is stored separately from detection output (audit P0-03):
``detected_start_sec``/``detected_end_sec`` stay immutable and identify the
segment across re-analysis, while ``start_sec``/``end_sec`` hold the effective
(user-trimmed) range. ``is_trimmed`` records that the user overrode the bounds.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import begin_write
from ..models import Asset, Segment, UserAnnotation
from ..schemas import AnnotateRequest, SegmentListItem, SegmentListOut, SegmentOut
from ..services.review_validation import EPSILON_SEC, MIN_REVIEW_SEC
from .deps import get_db

router = APIRouter(tags=["segments"])

RATING_VALUES = {"like", "favorite", "exclude", "pending"}

# A trimmed range shorter than this is rejected as degenerate.
MIN_TRIM_SEC = MIN_REVIEW_SEC


def _thumbnail(segment: Segment) -> str | None:
    refs = segment.frame_refs or []
    return refs[0] if refs else None


def _validate_trim(segment: Segment, in_sec: float | None, out_sec: float | None) -> None:
    """Reject non-finite / inverted / out-of-detected-window trims."""
    for label, value in (("in_sec", in_sec), ("out_sec", out_sec)):
        if value is not None and not math.isfinite(value):
            raise HTTPException(status_code=422, detail=f"{label} must be a finite number")

    low = in_sec if in_sec is not None else segment.start_sec
    high = out_sec if out_sec is not None else segment.end_sec
    if low < 0 or high - low < MIN_TRIM_SEC - EPSILON_SEC:
        raise HTTPException(
            status_code=422,
            detail=f"Selected range must be at least {MIN_TRIM_SEC}s (got {high - low:.3f}s)",
        )

    # `is None` (not truthiness): a detected start of 0.0 is legitimate.
    det_start = segment.detected_start_sec
    det_end = segment.detected_end_sec
    if det_start is None:
        det_start = segment.start_sec
    if det_end is None:
        det_end = segment.end_sec
    tolerance = EPSILON_SEC
    if low < det_start - tolerance or high > det_end + tolerance:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Trim {low:.3f}-{high:.3f}s must stay inside the detected segment "
                f"{det_start:.3f}-{det_end:.3f}s"
            ),
        )


def _apply_annotation(segment: Segment, payload: AnnotateRequest) -> None:
    if payload.rating is not None:
        if payload.rating not in RATING_VALUES:
            raise HTTPException(
                status_code=422, detail=f"rating must be one of {sorted(RATING_VALUES)}"
            )
        segment.user_rating = payload.rating
    if payload.story_role is not None:
        segment.story_role = payload.story_role

    if payload.in_sec is not None or payload.out_sec is not None:
        _validate_trim(segment, payload.in_sec, payload.out_sec)
        if payload.in_sec is not None:
            segment.start_sec = payload.in_sec
        if payload.out_sec is not None:
            segment.end_sec = payload.out_sec
        segment.is_trimmed = True


def _save_annotation(db: Session, segment: Segment, payload: AnnotateRequest) -> None:
    annotation = db.scalars(select(UserAnnotation).where(UserAnnotation.segment_id == segment.id)).first()
    if annotation is None:
        annotation = UserAnnotation(segment_id=segment.id)
        db.add(annotation)
    for payload_field, field in (("rating", "rating"), ("note", "note"), ("in_sec", "in_sec"), ("out_sec", "out_sec")):
        value = getattr(payload, payload_field)
        if value is not None:
            setattr(annotation, field, value)


def _segment_out(db: Session, segment: Segment) -> SegmentOut:
    note = db.scalar(select(UserAnnotation.note).where(UserAnnotation.segment_id == segment.id)) or ''
    return SegmentOut.model_validate(segment).model_copy(update={'note': note})


@router.get("/projects/{project_id}/segments", response_model=SegmentListOut)
def list_segments(
    project_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=500),
    rating: str | None = None,
    story_role: str | None = None,
    boundary_reason: str | None = None,
    asset_id: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Segment).where(Segment.project_id == project_id)
    if rating:
        stmt = stmt.where(Segment.user_rating == rating)
    if story_role:
        stmt = stmt.where(Segment.story_role == story_role)
    if boundary_reason:
        stmt = stmt.where(Segment.boundary_reason == boundary_reason)
    if asset_id:
        stmt = stmt.where(Segment.asset_id == asset_id)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Segment.created_at.desc()).offset(offset).limit(limit)
    ).all()

    asset_names: dict[str, str] = {}
    ids = {r.asset_id for r in rows}
    if ids:
        asset_names = {
            a.id: a.display_name
            for a in db.scalars(select(Asset).where(Asset.id.in_(ids))).all()
        }

    notes = dict(db.execute(select(UserAnnotation.segment_id, UserAnnotation.note).where(UserAnnotation.segment_id.in_([r.id for r in rows]))).all())
    items = [
        SegmentListItem(
            id=r.id,
            status=r.status,
            note=notes.get(r.id, ""),
            asset_id=r.asset_id,
            start_sec=r.start_sec,
            end_sec=r.end_sec,
            detected_start_sec=r.detected_start_sec if r.detected_start_sec is not None else r.start_sec,
            detected_end_sec=r.detected_end_sec if r.detected_end_sec is not None else r.end_sec,
            is_trimmed=bool(r.is_trimmed),
            duration_sec=round(max(0.0, r.end_sec - r.start_sec), 3),
            boundary_reason=r.boundary_reason,
            user_rating=r.user_rating,
            story_role=r.story_role,
            quality_metrics=r.quality_metrics or {},
            content_tags=r.content_tags or {},
            thumbnail=_thumbnail(r),
            asset_display_name=asset_names.get(r.asset_id, ""),
        )
        for r in rows
    ]
    return SegmentListOut(items=items, total=total, offset=offset, limit=limit)


@router.get("/segments/{segment_id}", response_model=SegmentOut)
def get_segment(segment_id: str, db: Session = Depends(get_db)):
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    return _segment_out(db, segment)


@router.patch("/segments/{segment_id}", response_model=SegmentOut)
def update_segment(segment_id: str, payload: AnnotateRequest, db: Session = Depends(get_db)):
    begin_write(db)
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    _apply_annotation(segment, payload)
    _save_annotation(db, segment, payload)
    db.commit()
    db.refresh(segment)
    return _segment_out(db, segment)


@router.post("/segments/{segment_id}/annotate")
def annotate_segment(segment_id: str, payload: AnnotateRequest, db: Session = Depends(get_db)):
    begin_write(db)
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")

    _apply_annotation(segment, payload)

    _save_annotation(db, segment, payload)

    db.commit()
    return {"ok": True}


@router.post("/segments/{segment_id}/reset-trim", response_model=SegmentOut)
def reset_trim(segment_id: str, db: Session = Depends(get_db)):
    """Restore the effective bounds to the detected ones."""
    begin_write(db)
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    segment.start_sec = segment.detected_start_sec if segment.detected_start_sec is not None else segment.start_sec
    segment.end_sec = segment.detected_end_sec if segment.detected_end_sec is not None else segment.end_sec
    segment.is_trimmed = False
    db.commit()
    db.refresh(segment)
    return _segment_out(db, segment)
