"""Segment listing / detail / annotation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Asset, Segment, UserAnnotation
from ..schemas import AnnotateRequest, SegmentListItem, SegmentListOut, SegmentOut
from .deps import get_db

router = APIRouter(tags=["segments"])

RATING_VALUES = {"like", "favorite", "exclude", "pending"}


def _thumbnail(segment: Segment) -> str | None:
    refs = segment.frame_refs or []
    return refs[0] if refs else None


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

    items = [
        SegmentListItem(
            id=r.id,
            asset_id=r.asset_id,
            start_sec=r.start_sec,
            end_sec=r.end_sec,
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
    return segment


@router.patch("/segments/{segment_id}", response_model=SegmentOut)
def update_segment(segment_id: str, payload: AnnotateRequest, db: Session = Depends(get_db)):
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    if payload.rating is not None:
        if payload.rating not in RATING_VALUES:
            raise HTTPException(status_code=422, detail=f"rating must be one of {sorted(RATING_VALUES)}")
        segment.user_rating = payload.rating
    if payload.story_role is not None:
        segment.story_role = payload.story_role
    if payload.in_sec is not None:
        segment.start_sec = payload.in_sec
    if payload.out_sec is not None:
        segment.end_sec = payload.out_sec
    db.commit()
    db.refresh(segment)
    return segment


@router.post("/segments/{segment_id}/annotate")
def annotate_segment(segment_id: str, payload: AnnotateRequest, db: Session = Depends(get_db)):
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise HTTPException(status_code=404, detail="Segment not found")

    if payload.rating is not None:
        if payload.rating not in RATING_VALUES:
            raise HTTPException(status_code=422, detail=f"rating must be one of {sorted(RATING_VALUES)}")
        segment.user_rating = payload.rating
    if payload.story_role is not None:
        segment.story_role = payload.story_role
    if payload.in_sec is not None:
        segment.start_sec = payload.in_sec
    if payload.out_sec is not None:
        segment.end_sec = payload.out_sec

    annotation = db.scalars(
        select(UserAnnotation).where(UserAnnotation.segment_id == segment_id)
    ).first()
    if annotation is None:
        annotation = UserAnnotation(segment_id=segment_id)
        db.add(annotation)
    if payload.rating is not None:
        annotation.rating = payload.rating
    if payload.note is not None:
        annotation.note = payload.note
    if payload.in_sec is not None:
        annotation.in_sec = payload.in_sec
    if payload.out_sec is not None:
        annotation.out_sec = payload.out_sec

    db.commit()
    return {"ok": True}
