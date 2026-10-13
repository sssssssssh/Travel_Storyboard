"""Story / AI endpoints: review-request import, round-2 proxies, direct model."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, Project, ReviewRequest
from ..schemas import ImportResult, JobCreate, JobOut, ReviewRequestOut, ReviewRequestsImport
from ..services.runner import runner
from ..services.story_planner import direct_analyze, import_review_requests
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


@router.get("/projects/{project_id}/review-requests", response_model=list[ReviewRequestOut])
def list_review_requests(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(ReviewRequest)
        .where(ReviewRequest.project_id == project_id)
        .order_by(ReviewRequest.created_at.desc())
    ).all()


@router.post("/projects/{project_id}/review-requests/proxies", response_model=JobOut, status_code=202)
def build_proxies(
    project_id: str, body: JobCreate | None = None, db: Session = Depends(get_db)
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    options = (body.options if body else None) or {}
    job = Job(
        project_id=project_id,
        job_type="proxies",
        config_snapshot={"max_total_mb": options.get("max_total_mb", 300)},
        stats={"total": 0, "done": 0, "failed": 0},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    runner.submit(job.id)
    return job


@router.post("/projects/{project_id}/story/analyze")
def analyze_story(project_id: str, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return direct_analyze(project_id)
