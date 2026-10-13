"""In-process job runner with a bounded thread pool and checkpoint updates.

Jobs are persisted rows; each task function receives `job_id`, loads what it
needs, and calls `update_job(...)` to report progress/resume state. On restart,
pending jobs are re-submitted from the persisted resume token.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from ..config import get_settings
from ..database import session_scope
from ..models import Job

logger = logging.getLogger(__name__)

TASK_REGISTRY: dict[str, Callable[[str], None]] = {}


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobRunner:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, get_settings().max_concurrency),
            thread_name_prefix="tsb-job",
        )
        self._futures: dict[str, object] = {}

    def submit(self, job_id: str) -> None:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job is None:
                return
            job_type = job.job_type

        task = TASK_REGISTRY.get(job_type)
        if task is None:
            update_job(job_id, status="failed", error=f"Unknown job type: {job_type}")
            return
        self._futures[job_id] = self._executor.submit(self._run, job_id, task)

    def _run(self, job_id: str, task: Callable[[str], None]) -> None:
        try:
            update_job(job_id, status="running", started_at=_now(), error="")
            task(job_id)
            update_job(job_id, status="succeeded", finished_at=_now())
        except Exception as exc:  # noqa: BLE001 - recorded on the job row
            logger.exception("Job %s failed", job_id)
            update_job(job_id, status="failed", error=str(exc), finished_at=_now())

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)


runner = JobRunner()
