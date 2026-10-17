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
from .runner import EXCLUSIVE_JOB_TYPES, EXCLUSIVE_STATUSES, project_lock, runner

VALID_JOB_TYPES = {
    "scan", "detect", "package", "proxies", "roughcut",
    "vision_analyze", "motion_review", "video_analyze",
}


def locking_job_for(project_id: str) -> Job | None:
    """The job whose worker currently holds *project_id*'s in-process lock.

    Used to name the blocker in a 409 when the lock itself is unavailable, so
    the response still carries a job type and id (the book forbids a bare
    conflict). Returns ``None`` if no row can be found — the caller then reports
    a conflict without details rather than inventing one.
    """
    from ..database import session_scope

    with session_scope() as s:
        return s.scalars(
            select(Job)
            .where(
                Job.project_id == project_id,
                Job.status.in_(EXCLUSIVE_STATUSES),
                Job.job_type.in_(tuple(EXCLUSIVE_JOB_TYPES)),
            )
            .order_by(Job.created_at.desc())
        ).first()


class JobConflict(Exception):
    """Another job already blocks this one. Carries the blocking job details."""

    def __init__(self, blocking: Job | None, *, job_type: str = "",
                 job_id: str = "") -> None:
        # The in-process lock can be held while the row is between states, so the
        # blocker is not always resolvable. Fall back to generic wording rather
        # than dropping the conflict.
        self.job_type = blocking.job_type if blocking is not None else job_type
        self.job_id = blocking.id if blocking is not None else job_id
        if blocking is not None:
            message = (f"项目已有正在运行的互斥任务（{self.job_type} {self.job_id}），"
                       f"请等待其完成")
        else:
            message = "项目已有正在运行的互斥任务（正在停止或清理中），请等待其完成"
        super().__init__(message)

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

    Exclusivity is enforced in **two layers** (TSB-261):

    1. the same in-process ``project_lock`` the worker holds, taken
       non-blocking — so a job whose worker is still stopping cannot be
       overtaken even if its row already reads ``cancelled``;
    2. the database check+insert inside ``BEGIN IMMEDIATE``, counting
       ``cancelling`` as active — which is what protects against another
       process.

    Lock order is fixed as **project lock, then DB write transaction**. Every
    caller must keep that order; taking them the other way round is what would
    deadlock the runner.
    """
    if request.job_type not in VALID_JOB_TYPES:
        raise InvalidJobType(
            f"job_type must be one of {sorted(VALID_JOB_TYPES)}"
        )

    lock = project_lock(project_id)
    # Non-blocking: if a worker still holds this project, report a conflict
    # rather than queueing behind it. Blocking here would let a request hang for
    # the length of an encode, and the book requires a 409 instead.
    if not lock.acquire(blocking=False):
        raise JobConflict(locking_job_for(project_id))
    try:
        return _create_locked(db, project_id, request, config_builder)
    finally:
        lock.release()


def _create_locked(
    db: Session,
    project_id: str,
    request: JobRequest,
    config_builder,
    *,
    allow_own_job_id: str | None = None,
) -> Job:
    """The check-and-insert body; callers must already hold the project lock.

    ``allow_own_job_id`` names a job that is allowed to be the active exclusive
    job without causing a conflict. It exists solely for TSB-348: the
    ``video_analyze`` worker queues its own fallback while its row still reads
    ``running``, and that predecessor must not be mistaken for a foreign blocker.
    Passing it removes only that single job id from consideration; any other live
    exclusive job still raises ``JobConflict`` as before.
    """
    # Serialize the whole check-and-insert at the database level.
    _begin_immediate(db)
    try:
        project = db.get(Project, project_id)
        if project is None:
            raise ProjectNotFound(project_id)

        # Cache-mutating jobs are exclusive per project: two detect runs over the
        # same footage would race on frames/ and the analysis rows. A job in
        # `cancelling` still counts — its worker has not released the project.
        active = db.scalars(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_(EXCLUSIVE_STATUSES),
            )
        ).all()
        blocking = [j for j in active if j.job_type in EXCLUSIVE_JOB_TYPES]
        # TSB-348: a *fallback* is queued by the job it falls back from, so the
        # only active exclusive job at that moment is its own predecessor. It must
        # not be read as a conflict -- otherwise "accept the fallback" could never
        # schedule anything. Only a job that is genuinely a different, still-live
        # predecessor blocks; the caller vouches for its own job id here.
        if allow_own_job_id:
            blocking = [j for j in blocking if j.id != allow_own_job_id]
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
    # Deliberately done *after* the project lock is released (in
    # `create_project_job`'s finally) is NOT possible here without losing
    # atomicity; submitting is non-blocking, so it is safe under the lock.
    runner.submit(job.id)
    return job
