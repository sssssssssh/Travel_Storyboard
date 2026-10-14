"""Job creation, status and control endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Job, Project, utcnow
from ..schemas import JobCreate, JobOut
from ..services.job_service import (
    VALID_JOB_TYPES,
    InvalidJobType,
    JobConflict,
    JobRequest,
    ProjectNotFound,
    create_project_job,
)
from ..services.runner import (
    ACTIVE_STATUSES,
    STATUS_CANCELLING,
    runner,
)
from .deps import get_db

router = APIRouter(tags=["jobs"])


def _job_config(project: Project, request: JobRequest) -> dict:
    """Build the type-specific config snapshot for a job."""
    settings = get_settings()
    options = request.options
    job_type = request.job_type
    force = request.force
    if job_type == "scan":
        return {"source_roots": list(project.source_roots or []), "force": force,
                "full_hash": bool(options.get("full_hash", False) or options.get("strict_full_hash", False)),
                "strict_full_hash": bool(options.get("strict_full_hash", False))}
    if job_type == "detect":
        return {
            "sample_fps": settings.detect_sample_fps,
            "threshold": settings.detect_threshold,
            "max_segment_sec": settings.detect_max_segment_sec,
            "thumb_width": settings.thumb_width,
            "force": force,
            "full_hash": bool(options.get("full_hash", False) or options.get("strict_full_hash", False)),
            "strict_full_hash": bool(options.get("strict_full_hash", False)),
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
    """Create and enqueue a project job.

    Exclusivity, validation and the insert are handled by the shared
    ``create_project_job`` service (TSB-242) so this endpoint and the
    review-request proxy endpoint behave identically.
    """
    options = (body.options if body else None) or {}
    force = bool(body.force) if body else False
    try:
        return create_project_job(
            db,
            project_id,
            JobRequest(job_type=job_type, options=options, force=force),
            config_builder=_job_config,
        )
    except InvalidJobType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found") from None
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail()) from exc


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db)):
    """Request cancellation; the worker settles the terminal state (TSB-261).

    This writes the **non-terminal** ``cancelling`` state, never ``cancelled``:
    the worker may still be inside an encode, holding the project lock, with
    staging left to collect. Writing the terminal state here is what let a new
    exclusive job be accepted while the old work had not actually stopped.

    Idempotent: cancelling an already-``cancelling`` job returns 200 with the
    current row, so a double click or a retry is harmless. ``finished_at`` is
    only written once the process has really stopped.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == STATUS_CANCELLING:
        # Already requested: report the in-progress cancellation as-is.
        return job
    if job.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"Job already finished with status {job.status}"
        )

    # Atomic CAS: only a runnable job may move to `cancelling`. `finished_at` is
    # deliberately left untouched.
    changed = db.execute(update(Job).where(
        Job.id == job_id, Job.status.in_(ACTIVE_STATUSES)
    ).values(status=STATUS_CANCELLING)).rowcount
    if changed != 1:
        db.rollback()
        # Lost the race to a concurrent cancel or a natural finish.
        current = db.get(Job, job_id)
        if current is not None and current.status == STATUS_CANCELLING:
            return current
        status = current.status if current is not None else "unknown"
        raise HTTPException(status_code=409, detail=f"Job already finished with status {status}")
    db.commit()
    db.refresh(job)
    return job


@router.get("/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
    ).all()
