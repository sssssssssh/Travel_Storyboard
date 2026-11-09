"""TSB-368-B — read-only director endpoints.

Every handler here is a pure read: no Job is created, no model is called, no
file is uploaded and no storyboard row is written. The services underneath are
shared by both endpoints so the two can never disagree about what a candidate is.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .deps import get_db
from ..models import Project
from ..services import director_candidates as dc
from ..services import director_query as dq
from ..services.director_schemas import CandidatesOut, ContextOut

router = APIRouter()

_ELIGIBILITY = Literal["eligible", "needs_review", "excluded", "all"]
_SORT = Literal["editorial_priority", "timeline"]


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/director/candidates", response_model=CandidatesOut)
def list_director_candidates(
    project_id: str,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    eligibility: _ELIGIBILITY = Query(default="eligible"),
    rating: str | None = Query(default=None, max_length=32),
    story_role: str | None = Query(default=None, max_length=64),
    asset_id: str | None = Query(default=None, max_length=64),
    scene_tag: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=120),
    sort: _SORT = Query(default="editorial_priority"),
    diversify: bool = Query(default=True),
    verify_freshness: bool = Query(
        default=False,
        description="Re-read a bounded sample of each source to detect changes. "
        "Off by default so a plain browse never touches the filesystem.",
    ),
    db: Session = Depends(get_db),
):
    """Paged director candidates with project-wide coverage.

    ``total`` counts matches before paging; ``coverage`` always describes the
    whole project, never the current filter or page.
    """
    _project_or_404(db, project_id)

    candidates = dc.collect_candidates(db, project_id, verify_freshness=verify_freshness)
    items, total = dq.select(
        candidates,
        eligibility=eligibility, rating=rating, story_role=story_role,
        asset_id=asset_id, scene_tag=scene_tag, q=q, sort=sort,
        diversify=diversify, offset=offset, limit=limit,
    )
    cov = dc.coverage(candidates)

    limitations: list[str] = []
    if not cov["coverage_complete"]:
        limitations.append("partial_analysis_coverage")
    if any(
        ch.state == dc.STATE_UNVERIFIED
        for c in candidates
        for ch in (c.static, c.multiframe, c.video_native)
    ):
        limitations.append("freshness_unverified_for_some_readings")

    return {
        "project_id": project_id,
        "query_version": "1.0",
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [c.as_dict() for c in items],
        "coverage": cov,
        "limitations": limitations,
    }


@router.get("/projects/{project_id}/director/context", response_model=ContextOut)
def director_context(
    project_id: str,
    max_candidates: int = Query(default=40, ge=1, le=200),
    theme: str = Query(default="", max_length=200),
    target_duration_sec: float | None = Query(default=None, ge=1, le=36000),
    db: Session = Depends(get_db),
):
    """Normalized planning input for TSB-369.

    This endpoint **does not generate a story**. It returns the candidate set,
    the human labels that must be respected, honest coverage numbers and an
    ``input_digest`` that changes when the underlying input changes.
    """
    _project_or_404(db, project_id)

    candidates = dc.collect_candidates(db, project_id, verify_freshness=False)
    cov = dc.coverage(candidates)
    brief = {"theme": theme, "target_duration_sec": target_duration_sec}
    return dq.build_context(
        project_id=project_id,
        candidates=candidates,
        coverage=cov,
        brief=brief,
        max_candidates=max_candidates,
    )
