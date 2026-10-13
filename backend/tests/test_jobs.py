"""Audit P0-04 / P1-08 — job exclusivity, cancellation, partial-failure reporting.

Job rows are always created through the API (the real path); the heavy task
body is temporarily replaced in ``TASK_REGISTRY`` so the lifecycle can be driven
without invoking FFmpeg. The original callables are restored in a fixture.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import session_scope
from app.models import Asset, Job
from app.services.runner import (
    EXCLUSIVE_JOB_TYPES,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_SUCCEEDED,
    JobCancelled,
    JobFailures,
    TASK_REGISTRY,
    is_cancelled,
    update_job,
)


@pytest.fixture()
def swap_task():
    """Temporarily replace registered tasks, restoring them afterwards."""
    original = dict(TASK_REGISTRY)

    def _install(job_type: str, fn):
        TASK_REGISTRY[job_type] = fn

    yield _install
    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "job-test", "source_roots": []})
    assert res.status_code in (200, 201, 202), res.text
    return res.json()


def _wait_terminal(job_id: str, timeout: float = 15.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(Job, job_id)
            status = job.status if job else None
        if status not in ("pending", "running"):
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _wait_running(job_id: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job and job.status == "running":
                return True
        time.sleep(0.05)
    return False


def test_expected_tasks_are_registered():
    for name in ("scan", "detect", "package", "proxies", "roughcut"):
        assert name in TASK_REGISTRY, f"{name} task not registered"


def test_exclusive_types_cover_heavy_jobs():
    assert {"scan", "detect", "package", "roughcut", "proxies"} <= EXCLUSIVE_JOB_TYPES


def test_second_exclusive_job_is_rejected(client, project, swap_task):
    """A project must not run two heavy jobs at once (audit P0-04)."""
    gate = threading.Event()
    swap_task("detect", lambda job_id: gate.wait(timeout=15))
    job_id = None

    try:
        first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert first.status_code in (200, 201, 202), first.text
        job_id = first.json()["id"]
        assert _wait_running(job_id), "first job never started"

        second = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert second.status_code == 409, second.text
    finally:
        gate.set()
        if job_id:
            _wait_terminal(job_id)


def test_cancel_endpoint_marks_job_cancelled(client, project, swap_task):
    gate = threading.Event()
    swap_task("package", lambda job_id: gate.wait(timeout=15))
    job_id = None

    try:
        res = client.post(f"/api/projects/{project['id']}/jobs/package", json={})
        assert res.status_code in (200, 201, 202), res.text
        job_id = res.json()["id"]
        assert _wait_running(job_id), "job never started"

        cancel = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel.status_code in (200, 202), cancel.text
        assert is_cancelled(job_id) is True
    finally:
        gate.set()
        if job_id:
            _wait_terminal(job_id)


def test_new_job_rows_start_with_empty_failures(client, project, swap_task):
    swap_task("roughcut", lambda job_id: update_job(job_id, stats={"total": 0, "done": 0}))

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_SUCCEEDED

    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.failures["count"] == 0
        assert job.failures["items"] == []


def test_failures_make_the_job_partial(client, project, swap_task):
    """A task that records failures must end as `partial_failed` (audit P1-08)."""

    def _partially_failing(job_id: str) -> None:
        f = JobFailures(job_id)
        f.record(kind="asset", ref="asset-1", message="probe failed")
        f.publish()

    swap_task("detect", _partially_failing)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_PARTIAL

    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.failures["count"] == 1
        assert job.failures["items"][0]["message"] == "probe failed"


def test_raising_task_is_marked_failed(client, project, swap_task):
    def _exploding(job_id: str) -> None:  # noqa: ARG001
        raise RuntimeError("boom")

    swap_task("package", _exploding)

    res = client.post(f"/api/projects/{project['id']}/jobs/package", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_FAILED

    with session_scope() as s:
        assert "boom" in (s.get(Job, job_id).error or "")


def test_cancelled_task_is_marked_cancelled(client, project, swap_task):
    def _cancel_me(job_id: str) -> None:  # noqa: ARG001
        raise JobCancelled("cancelled during run")

    swap_task("roughcut", _cancel_me)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_CANCELLED


def test_unknown_job_type_is_rejected(client, project):
    res = client.post(f"/api/projects/{project['id']}/jobs/not-a-real-task", json={})
    assert res.status_code in (400, 404, 422), res.text


def test_asset_model_has_unique_path_per_project(client, project):
    """The duplicate-asset guard is enforced at the DB layer (audit P0-02)."""
    path = "C:/media/same.mp4"
    with session_scope() as s:
        s.add(
            Asset(
                id=str(uuid.uuid4()),
                project_id=project["id"],
                source_path=path,
                display_name="same.mp4",
            )
        )

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(
                Asset(
                    id=str(uuid.uuid4()),
                    project_id=project["id"],
                    source_path=path,
                    display_name="same.mp4",
                )
            )

    with session_scope() as s:
        rows = s.scalars(select(Asset).where(Asset.project_id == project["id"])).all()
    assert len(rows) == 1
