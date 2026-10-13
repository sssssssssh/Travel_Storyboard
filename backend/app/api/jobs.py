"""Job creation and status endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Job, Project
from ..schemas import JobCreate, JobOut
from ..services.runner import runner
from .deps import get_db

router = APIRouter(tags=["jobs"])

VALID_JOB_TYPES = {"scan", "detect", "package", "proxies", "roughcut"}


def _job_config(project: Project, job_type: str, options: dict) -> dict:
    settings = get_settings()
    if job_type == "scan":
        return {"source_roots": list(project.source_roots or [])}
    if job_type == "detect":
        return {
            "sample_fps": settings.detect_sample_fps,
            "threshold": settings.detect_threshold,
            "max_segment_sec": settings.detect_max_segment_sec,
            "thumb_width": settings.thumb_width,
        }
    if job_type == "package":
        return {
            "options": {
                "rating": options.get("rating", ""),
                "contact_sheet_cols": options.get("contact_sheet_cols", 5),
                "contact_sheet_rows": options.get("contact_sheet_rows", 4),
            }
        }
    if job_type == "proxies":
        return {"max_total_mb": options.get("max_total_mb", 300)}
    # roughcut
    return {}


@router.post("/projects/{project_id}/jobs/{job_type}", response_model=JobOut, status_code=202)
def create_job(
    project_id: str,
    job_type: str,
    body: JobCreate | None = None,
    db: Session = Depends(get_db),
):
    if job_type not in VALID_JOB_TYPES:
        raise HTTPException(
            status_code=422, detail=f"job_type must be one of {sorted(VALID_JOB_TYPES)}"
        )
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    options = (body.options if body else None) or {}
    job = Job(
        project_id=project_id,
        job_type=job_type,
        config_snapshot=_job_config(project, job_type, options),
        stats={"total": 0, "done": 0, "failed": 0},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    runner.submit(job.id)
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
    ).all()
