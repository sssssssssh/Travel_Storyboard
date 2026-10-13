"""Job creation, status and control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Job, Project, utcnow
from ..schemas import JobCreate, JobOut
from ..services.runner import (
    ACTIVE_STATUSES,
    EXCLUSIVE_JOB_TYPES,
    STATUS_CANCELLED,
    STATUS_PENDING,
    runner,
)
from .deps import get_db

router = APIRouter(tags=["jobs"])

VALID_JOB_TYPES = {"scan", "detect", "package", "proxies", "roughcut"}


def _job_config(project: Project, job_type: str, options: dict, force: bool) -> dict:
    settings = get_settings()
    if job_type == "scan":
        return {"source_roots": list(project.source_roots or []), "force": force}
    if job_type == "detect":
        return {
            "sample_fps": settings.detect_sample_fps,
            "threshold": settings.detect_threshold,
            "max_segment_sec": settings.detect_max_segment_sec,
            "thumb_width": settings.thumb_width,
            "force": force,
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

    # Cache-mutating jobs are exclusive per project (audit P0-04): two detect
    # runs over the same asset folder would race on frames/ and the DB.
    if job_type in EXCLUSIVE_JOB_TYPES:
        active = db.scalars(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_(ACTIVE_STATUSES),
            )
        ).all()
        blocking = [j for j in active if j.job_type in EXCLUSIVE_JOB_TYPES]
        if blocking:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Project already has an active job "
                    f"({blocking[0].job_type} {blocking[0].id}); wait for it to finish"
                ),
            )

    options = (body.options if body else None) or {}
    force = bool(body.force) if body else False
    job = Job(
        project_id=project_id,
        job_type=job_type,
        config_snapshot=_job_config(project, job_type, options, force),
        stats={"total": 0, "done": 0, "failed": 0},
        failures={"count": 0, "items": []},
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


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """Cooperative cancel: the task stops between items and keeps its checkpoint."""
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"Job already finished with status {job.status}"
        )
    job.status = STATUS_CANCELLED
    job.finished_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
    ).all()
