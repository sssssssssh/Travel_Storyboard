"""Shared project-job creation (TSB-242).

Both the generic ``POST /projects/{id}/jobs/{job_type}`` endpoint and the
review-specific proxy entry point must go through here, so a project can never
end up with two competing heavy jobs.

Race safety: the "is another job active?" check and the INSERT happen inside one
``BEGIN IMMEDIATE`` transaction. A per-process lock alone is not enough — two
requests can interleave between the check and the commit — and the frontend
disabling a button is not a guarantee at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..models import Job, Project
from .runner import ACTIVE_STATUSES, EXCLUSIVE_JOB_TYPES, runner

VALID_JOB_TYPES = {"scan", "detect", "package", "proxies", "roughcut"}


class JobConflict(Exception):
    """Another job already blocks this one. Carries the blocking job details."""

    def __init__(self, blocking: Job) -> None:
        self.job_type = blocking.job_type
        self.job_id = blocking.id
        super().__init__(
            f"项目已有正在运行的互斥任务（{blocking.job_type} {blocking.id}），请等待其完成"
        )

    def detail(self) -> dict:
        return {
            "error_code": "JOB_CONFLICT",
            "message": str(self),
            "active_job_type": self.job_type,
            "active_job_id": self.job_id,
        }


class ProjectNotFound(Exception):
    """The project id does not exist."""


class InvalidJobType(Exception):
    """The requested job type is not one this API supports."""


@dataclass
class JobRequest:
    """Inputs needed to build a job's config snapshot."""

    job_type: str
    options: dict
    force: bool = False


def _initial_stats(config: dict) -> dict:
    return {
        "total": 0,
        "done": 0,
        "failed": 0,
        "phase": "queued",
        "signature_mode": "full" if config.get("full_hash") else "quick",
        "bytes_read": 0,
        "total_bytes": 0,
        "processed_files": 0,
        "total_files": 0,
    }


def _begin_immediate(session: Session) -> None:
    """Open a write transaction, tolerating an already-open one.

    ``BEGIN IMMEDIATE`` errors if SQLAlchemy already started a transaction (which
    happens as soon as a SELECT runs). SQLite will not nest them, so the existing
    transaction is rolled back and reopened as a write transaction.

    Callers must therefore not hold uncommitted work in *session* when calling
    :func:`create_project_job` — the rollback would discard it. Both current
    entry points only read before calling; this note exists so a future caller
    does not silently lose writes.
    """
    try:
        session.execute(text("BEGIN IMMEDIATE"))
    except OperationalError:
        # "cannot start a transaction within a transaction" — already open.
        session.rollback()
        session.execute(text("BEGIN IMMEDIATE"))


def create_project_job(
    db: Session,
    project_id: str,
    request: JobRequest,
    *,
    config_builder,
) -> Job:
    """Validate, serialize and enqueue one project job.

    ``config_builder(project, request) -> dict`` supplies the type-specific
    config snapshot. Raises ``InvalidJobType``, ``ProjectNotFound`` or
    ``JobConflict``; the caller maps those to HTTP responses.

    ``db`` must not carry uncommitted work (see :func:`_begin_immediate`).
    """
    if request.job_type not in VALID_JOB_TYPES:
        raise InvalidJobType(
            f"job_type must be one of {sorted(VALID_JOB_TYPES)}"
        )

    # Serialize the whole check-and-insert at the database level.
    _begin_immediate(db)
    try:
        project = db.get(Project, project_id)
        if project is None:
            raise ProjectNotFound(project_id)

        # Cache-mutating jobs are exclusive per project: two detect runs over the
        # same footage would race on frames/ and the analysis rows.
        active = db.scalars(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_(ACTIVE_STATUSES),
            )
        ).all()
        blocking = [j for j in active if j.job_type in EXCLUSIVE_JOB_TYPES]
        if blocking and request.job_type in EXCLUSIVE_JOB_TYPES:
            raise JobConflict(blocking[0])

        config = config_builder(project, request)
        job = Job(
            project_id=project_id,
            job_type=request.job_type,
            config_snapshot=config,
            stats=_initial_stats(config),
            failures={"count": 0, "items": []},
        )
        db.add(job)
        db.commit()
        db.refresh(job)
    except Exception:
        db.rollback()
        raise

    # Submit after the row is durable so the worker cannot observe a half-written
    # job, and so a submit failure leaves a recoverable `pending` row.
    runner.submit(job.id)
    return job
