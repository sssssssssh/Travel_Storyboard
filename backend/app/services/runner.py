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
from threading import Lock, RLock
from collections import deque

from sqlalchemy import select, update

from ..config import get_settings
from ..database import session_scope
from ..models import Job

logger = logging.getLogger(__name__)

TASK_REGISTRY: dict[str, Callable[[str], None]] = {}

# Statuses of a job row (TSB-261).
#
#   pending -> running -> {succeeded | partial_failed | failed}
#   {pending, running} -> cancelling -> cancelled
#
# ``cancelling`` exists because a cancel request is not the same thing as a
# cancelled job: the worker may still be inside an encode, still holding
# ``project_lock``, with its staging directory uncollected. Writing the terminal
# state at request time made the project look idle and let a new exclusive job
# be accepted while the old work was still stopping.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"
STATUS_SUCCEEDED = "succeeded"
STATUS_PARTIAL = "partial_failed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: Statuses that may still transition to ``running``. Notably *not* cancelling:
#: a worker must never "resume" a job the user asked to stop.
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

#: A cancel has been requested but the worker may still be stopping.
#: ``is_cancelled()`` treats both as "stop what you are doing".
CANCEL_REQUESTED_STATES = (STATUS_CANCELLING, STATUS_CANCELLED)

#: Statuses that keep a project's exclusivity claim: the runnable ones plus
#: ``cancelling``, because the previous worker has not released the project yet.
EXCLUSIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_CANCELLING)

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED)

# Jobs that mutate a project's cache must not run concurrently within a project.
EXCLUSIVE_JOB_TYPES = {"scan", "detect", "package", "roughcut", "proxies"}
_PROJECT_LOCKS: dict[str, RLock] = {}
_LOCKS_GUARD = Lock()


def project_lock(project_id: str) -> RLock:
    with _LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(project_id, RLock())


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
        with session_scope() as s:
            job = s.get(Job, job_id)
            saved = dict(job.failures or {}) if job else {}
        self.items = list(saved.get("items", []))
        self.total = int(saved.get("count", len(self.items)))

    def record(self, *, kind: str, ref: str, message: str,
               error_code: str | None = None) -> None:
        """Record (or replace) a per-item failure.

        ``error_code`` is carried through so the job's failure list can be
        reconciled by code — the UI and the TSB-254 acceptance criteria both need
        to distinguish "source unavailable" from "probe failed" without parsing
        prose. It is omitted when unknown so the shape stays backwards
        compatible.
        """
        item = {"kind": kind, "ref": ref, "message": message[:500]}
        if error_code:
            item["error_code"] = error_code
        prior = next((i for i, value in enumerate(self.items)
                      if value["kind"] == kind and value["ref"] == ref), None)
        if prior is not None:
            self.items[prior] = item
        else:
            self.total += 1
            if len(self.items) < self._limit:
                self.items.append(item)
        self.publish()

    def resolve(self, *, kind: str, ref: str) -> None:
        kept = [item for item in self.items if not (item["kind"] == kind and item["ref"] == ref)]
        self.total -= len(self.items) - len(kept)
        self.items = kept
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


def update_job(job_id: str, *, expected_statuses=None, **fields) -> bool:
    """Atomic compare-and-update; terminal states are immutable.

    Progress/checkpoints may still be persisted while an in-flight item stops.
    """
    with session_scope() as s:
        stmt = update(Job).where(Job.id == job_id)
        status = fields.get("status")
        if status is not None:
            allowed = expected_statuses or ACTIVE_STATUSES
            stmt = stmt.where(Job.status.in_(allowed))
        result = s.execute(stmt.values(**fields))
        changed = result.rowcount == 1
    if status is not None and not changed:
        logger.info("Job %s transition to %s rejected (state changed)", job_id, status)
    return changed


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


def job_status(job_id: str) -> str | None:
    """Current status of a job row, or ``None`` when it does not exist."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        return job.status if job is not None else None


def is_cancelled(job_id: str) -> bool:
    """Whether a cancel has been requested for *job_id* (TSB-261).

    True for ``cancelling`` *and* ``cancelled``: the media layer must stop as
    soon as the request lands, not only after the worker settles the row.
    """
    with session_scope() as s:
        job = s.get(Job, job_id)
        return bool(job is not None and job.status in CANCEL_REQUESTED_STATES)


def raise_if_cancelled(job_id: str) -> None:
    if is_cancelled(job_id):
        raise JobCancelled("Job cancelled by user")


def cancel_check_for(job_id: str | None):
    """Build a pollable cancel callback for a job, or ``None`` without one.

    Passed into the media layer (``ffmpeg.run(..., cancel_check=...)``) so a
    cancel stops the *external process* instead of waiting for the current
    encode to finish (TSB-253).

    Returns ``None`` when there is no job, which keeps the append-only,
    one-shot detectors used outside a job on their original code path.
    """
    if not job_id:
        return None
    return lambda: is_cancelled(job_id)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRunner:
    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._futures: dict[str, object] = {}
        self._submit_lock = RLock()
        self._recovery_queues: dict[str, deque] = {}

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
        with self._submit_lock:
            future = self._futures.get(job_id)
            if future is not None and not future.done():
                return
            with session_scope() as s:
                job = s.get(Job, job_id)
                if job is None or job.status not in ACTIVE_STATUSES:
                    return
                job_type = job.job_type
            task = TASK_REGISTRY.get(job_type)
            if task is None:
                update_job(job_id, status=STATUS_FAILED, finished_at=_now(),
                           error=f"Unknown job type: {job_type}")
                return
            self._futures[job_id] = self._pool().submit(self._run, job_id, task)

    def resume_queue(self, project_id: str, job_ids: list[str]) -> None:
        """Dispatch one recovered mutator at a time in persisted creation order."""
        with self._submit_lock:
            self._recovery_queues[project_id] = deque(job_ids)
            self._resume_next(project_id)

    def _resume_next(self, project_id: str) -> None:
        queue = self._recovery_queues.get(project_id)
        while queue:
            jid = queue.popleft()
            self.submit(jid)
            future = self._futures.get(jid)
            if future is not None:
                future.add_done_callback(lambda _: self._recovered_done(project_id))
                return
        self._recovery_queues.pop(project_id, None)

    def _recovered_done(self, project_id: str) -> None:
        with self._submit_lock:
            self._resume_next(project_id)

    def _run(self, job_id: str, task: Callable[[str], None]) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            pid = job.project_id
        # Held across the whole run *and* the post-cancel settling: until this
        # worker leaves, no other exclusive job may start for the project.
        with project_lock(pid):
            # A job cancelled while queued must not start executing at all.
            if is_cancelled(job_id):
                self._settle_cancelled(job_id, started=False)
                clear_active_job_type(job_id)
                return
            if not update_job(job_id, status=STATUS_RUNNING, started_at=_now(),
                              error="", finished_at=None):
                return
            cancelled = False
            try:
                task(job_id)
            except JobCancelled:
                cancelled = True
            except Exception as exc:
                # A media process killed by its own cancel callback is a
                # cancellation, not a failure (TSB-253).
                if getattr(exc, "code", None) == "CANCELLED":
                    cancelled = True
                else:
                    logger.exception("Job %s failed", job_id)
                    # A genuine failure must not overwrite a cancel the user
                    # asked for; only settle `failed` if no cancel is pending.
                    if not is_cancelled(job_id):
                        update_job(job_id, status=STATUS_FAILED, error=str(exc),
                                   finished_at=_now())
                    else:
                        cancelled = True
            finally:
                # Any cancel requested *during* the run is settled here, after the
                # task body has returned — i.e. after its staging cleanup ran.
                if not cancelled and is_cancelled(job_id):
                    cancelled = True
                if cancelled:
                    self._settle_cancelled(job_id, started=True)
                else:
                    with session_scope() as s:
                        job = s.get(Job, job_id)
                        count = int((job.failures or {}).get("count", 0)) if job else 0
                    # A cancel that landed while we were computing the outcome
                    # must still win: re-check before publishing success.
                    if is_cancelled(job_id):
                        self._settle_cancelled(job_id, started=True)
                    else:
                        update_job(
                            job_id,
                            status=STATUS_PARTIAL if count else STATUS_SUCCEEDED,
                            finished_at=_now(),
                            error=f"{count} item(s) failed; see job.failures" if count else "",
                        )
                clear_active_job_type(job_id)

    def _settle_cancelled(self, job_id: str, *, started: bool) -> None:
        """Move ``cancelling`` -> ``cancelled`` and stamp ``finished_at``.

        Called only once the worker has left the task body, so the timestamp
        means "the process actually stopped and cleanup finished" rather than
        "the user clicked cancel". CAS-guarded: never rewrites a terminal state
        that was already settled (for example a success that won the race).
        """
        if update_job(job_id, status=STATUS_CANCELLED, finished_at=_now(),
                      expected_statuses=(STATUS_CANCELLING,)):
            return
        # No `cancelling` row: either it is already cancelled, or the run ended
        # as a legitimate terminal state before the request was recorded.
        status = job_status(job_id)
        if status == STATUS_CANCELLED:
            return
        if status in TERMINAL_STATUSES:
            logger.info(
                "Job %s cancel ignored: already settled as %s", job_id, status
            )
            return
        # Still pending/running (e.g. cancel raced the status write). The user
        # asked for this job to stop, so settle it as cancelled from running.
        if not update_job(job_id, status=STATUS_CANCELLED, finished_at=_now(),
                          expected_statuses=ACTIVE_STATUSES):
            logger.warning("Job %s could not be settled as cancelled", job_id)

    def shutdown(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=False)
            self._executor = None


runner = JobRunner()


def converge_pending_cancellations() -> list[str]:
    """Settle leftover ``cancelling`` rows that no worker will finish (TSB-261).

    A job cancelled *before* it was ever dispatched has no worker to run its
    cleanup, so nothing would ever move it to ``cancelled`` and the project
    would stay locked forever. This is called on startup (where the previous
    process is provably gone, so its staging is orphaned) and returns the ids
    it settled.

    Only rows still in ``cancelling`` are touched: a job that is legitimately
    ``running`` is somebody else's responsibility, and a terminal row is
    immutable.
    """
    with session_scope() as s:
        rows = s.scalars(
            select(Job)
            .where(Job.status == STATUS_CANCELLING)
            .order_by(Job.created_at, Job.id)
        ).all()
        candidates = [j.id for j in rows]

    settled: list[str] = []
    for job_id in candidates:
        # No worker is attached to a queued cancelling row, so settling it here
        # is the whole cleanup: nothing was encoded and no staging was created.
        if update_job(job_id, status=STATUS_CANCELLED, finished_at=_now(),
                      expected_statuses=(STATUS_CANCELLING,)):
            settled.append(job_id)
            logger.info("Settled orphaned cancelling job %s as cancelled", job_id)
    return settled


def resume_interrupted_jobs() -> list[str]:
    """Re-dispatch jobs left pending/running by a previous process.

    Each task resumes from its persisted ``resume_token`` and skips work it has
    already completed, so re-dispatch is idempotent (audit P0-04).

    ``cancelling`` rows are deliberately **not** resumed (TSB-261): the user
    asked them to stop, so re-running them would be exactly the wrong thing.
    They are settled by :func:`converge_pending_cancellations` instead.
    """
    with session_scope() as s:
        rows = s.scalars(
            select(Job)
            .where(Job.status.in_(ACTIVE_STATUSES))
            .order_by(Job.created_at, Job.id)
        ).all()
        candidates = [(j.id, j.job_type, j.project_id) for j in rows]

    resumed: list[str] = []
    queues: dict[str, list[str]] = {}
    for job_id, job_type, project_id in candidates:
        if job_type not in TASK_REGISTRY:
            update_job(job_id, status=STATUS_FAILED, finished_at=_now(),
                       error=f"Unknown job type: {job_type}")
            continue
        if not update_job(job_id, status=STATUS_PENDING, error="", started_at=None,
                          finished_at=None):
            continue
        if job_type in EXCLUSIVE_JOB_TYPES:
            queues.setdefault(project_id, []).append(job_id)
        else:
            runner.submit(job_id)
        resumed.append(job_id)
    for pid, ids in queues.items():
        runner.resume_queue(pid, ids)
    return resumed
