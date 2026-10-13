"""In-process job runner with a bounded thread pool and checkpoint updates.

Jobs are persisted rows; each task function receives `job_id`, loads what it
needs, and calls `update_job(...)` to report progress/resume state.

Reliability model (audit P0-04 / P1-08):
- A task may report per-item failures through :class:`JobFailures`. The job is
  then finished as ``partial_failed`` instead of a misleading ``succeeded``.
- Jobs left ``pending``/``running`` by a crash or restart are re-dispatched on
  startup by :func:`resume_interrupted_jobs`, resuming from the persisted
  ``resume_token`` (each task skips already-completed items).
- A cooperative cancel flag lets long tasks stop between items.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from ..config import get_settings
from ..database import session_scope
from ..models import Job

logger = logging.getLogger(__name__)

TASK_REGISTRY: dict[str, Callable[[str], None]] = {}

# Terminal + non-terminal statuses.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial_failed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED)

# Jobs that mutate a project's cache must not run concurrently within a project.
EXCLUSIVE_JOB_TYPES = {"scan", "detect", "package", "roughcut", "proxies"}


class JobCancelled(RuntimeError):
    """Raised inside a task when the job has been asked to stop."""


class JobFailures:
    """Collects per-item failures so a task can report a partial outcome.

    Tasks call :meth:`record` instead of only bumping a counter; the runner
    promotes the job to ``partial_failed`` when any failure was recorded.
    """

    def __init__(self, job_id: str, limit: int = 500) -> None:
        self.job_id = job_id
        self._limit = limit
        self.items: list[dict] = []
        self.total = 0

    def record(self, *, kind: str, ref: str, message: str) -> None:
        self.total += 1
        if len(self.items) < self._limit:
            self.items.append({"kind": kind, "ref": ref, "message": message[:500]})
        self.publish()

    def extend(self, items: list[dict]) -> None:
        for item in items:
            self.record(
                kind=str(item.get("kind", "item")),
                ref=str(item.get("ref", "")),
                message=str(item.get("message", "")),
            )

    def publish(self) -> None:
        """Mirror the failure list onto the job row for the UI."""
        update_job(
            self.job_id,
            failures={"count": self.total, "items": list(self.items)},
        )

    def __len__(self) -> int:
        return self.total

    def __bool__(self) -> bool:
        return self.total > 0


def register_task(job_type: str) -> Callable:
    def decorator(fn: Callable[[str], None]) -> Callable[[str], None]:
        TASK_REGISTRY[job_type] = fn
        return fn

    return decorator


def update_job(job_id: str, **fields) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)


def task_finished_cleanly(job_id: str) -> bool:  # pragma: no cover - reserved
    """Placeholder for future staged-completion checks."""
    return True


def clear_active_job_type(job_id: str) -> None:
    """Drop the in-memory exclusivity claim held for this project."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        token = dict(job.resume_token or {})
        token.pop("__exclusive", None)
        job.resume_token = token


def is_cancelled(job_id: str) -> bool:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return bool(job is not None and job.status == STATUS_CANCELLED)


def raise_if_cancelled(job_id: str) -> None:
    if is_cancelled(job_id):
        raise JobCancelled("Job cancelled by user")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRunner:
    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, object] = {}

    def _pool(self) -> ThreadPoolExecutor:
        """Return a live executor, recreating it after a shutdown.

        The executor is torn down on app shutdown; recreating lazily keeps the
        runner usable if a job is submitted again (e.g. in tests, or if the
        lifespan runs more than once).
        """
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max(1, get_settings().max_concurrency),
                thread_name_prefix="tsb-job",
            )
        return self._executor

    def submit(self, job_id: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job_type = job.job_type

        task = TASK_REGISTRY.get(job_type)
        if task is None:
            update_job(job_id, status=STATUS_FAILED, error=f"Unknown job type: {job_type}")
            return
        self._futures[job_id] = self._pool().submit(self._run, job_id, task)

    def _run(self, job_id: str, task: Callable[[str], None]) -> None:
        try:
            update_job(
                job_id, status=STATUS_RUNNING, started_at=_now(), error="", finished_at=None
            )
            task(job_id)
        except JobCancelled:
            logger.info("Job %s cancelled", job_id)
            update_job(job_id, status=STATUS_CANCELLED, finished_at=_now())
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            logger.exception("Job %s failed", job_id)
            update_job(job_id, status=STATUS_FAILED, error=str(exc), finished_at=_now())
        else:
            # A task that recorded per-item failures is NOT a clean success.
            with session_scope() as s:
                job = s.get(Job, job_id)
                failures = dict(job.failures or {}) if job is not None else {}
            count = int(failures.get("count", 0))
            if count:
                update_job(
                    job_id,
                    status=STATUS_PARTIAL,
                    finished_at=_now(),
                    error=f"{count} item(s) failed; see job.failures",
                )
            else:
                update_job(job_id, status=STATUS_SUCCEEDED, finished_at=_now())
        finally:
            clear_active_job_type(job_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None


runner = JobRunner()


def resume_interrupted_jobs() -> list[str]:
    """Re-dispatch jobs left pending/running by a previous process.

    Each task resumes from its persisted ``resume_token`` and skips work it has
    already completed, so re-dispatch is idempotent (audit P0-04).
    """
    with session_scope() as s:
        rows = s.scalars(
            select(Job)
            .where(Job.status.in_(ACTIVE_STATUSES))
            .order_by(Job.created_at)
        ).all()
        candidates = [(j.id, j.job_type, j.project_id) for j in rows]
        for job_id, _job_type, _project_id in candidates:
            job = s.get(Job, job_id)
            if job is not None:
                job.status = STATUS_PENDING
                job.error = ""
                job.started_at = None
                job.finished_at = None

    resumed: list[str] = []
    for job_id, job_type, _project_id in candidates:
        if job_type not in TASK_REGISTRY:
            update_job(
                job_id, status=STATUS_FAILED, error=f"Unknown job type: {job_type}"
            )
            continue
        logger.info("Resuming interrupted job %s (%s)", job_id, job_type)
        runner.submit(job_id)
        resumed.append(job_id)
    return resumed
