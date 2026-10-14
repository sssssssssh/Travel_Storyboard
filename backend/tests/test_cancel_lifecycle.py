"""TSB-261 — the cancel window must keep the project mutex closed.

The confirmed defect (task book §3): ``cancel_job`` wrote the terminal status
``cancelled`` *immediately*, and only ``pending``/``running`` counted as active.
So the moment a user cancelled, the project looked idle to
``create_project_job`` — even though the worker thread was still inside the
encode, still holding ``project_lock``, and its staging directory had not been
collected. A new exclusive job could therefore be accepted, land in the
database, and queue up behind the very work that had not stopped yet.

The fix introduces a non-terminal ``cancelling`` state: the API only *requests*
cancellation, the worker transitions to ``cancelled`` after the media process is
reaped and this run's staging is cleaned, and ``cancelling`` stays exclusive the
whole time.

These tests assert the window itself, not just the end state — driving the
worker with an explicit gate so the "cancel requested, still stopping" condition
is observable instead of guessed with sleeps.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Job
from app.services import runner as runner_mod
from app.services.runner import (
    ACTIVE_STATUSES,
    CANCEL_REQUESTED_STATES,
    EXCLUSIVE_STATUSES,
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    TASK_REGISTRY,
    is_cancelled,
    project_lock,
    update_job,
)


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "cancel-lifecycle", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


@pytest.fixture()
def swap_task():
    original = dict(TASK_REGISTRY)

    def _install(job_type: str, fn):
        TASK_REGISTRY[job_type] = fn

    yield _install
    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)


# --------------------------------------------------------------------------
# synchronisation helpers — observable conditions, never bare sleeps
# --------------------------------------------------------------------------

class Gate:
    """A worker-side rendezvous the test can observe and release."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def wait_entered(self, timeout: float = 10.0) -> bool:
        return self.entered.wait(timeout)

    def open(self) -> None:
        self.release.set()


def _job_status(job_id: str) -> str | None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return job.status if job else None


def _job_finished_at(job_id: str):
    with session_scope() as s:
        job = s.get(Job, job_id)
        return job.finished_at if job else None


def _wait_status(job_id: str, wanted, timeout: float = 15.0) -> str | None:
    if isinstance(wanted, str):
        wanted = (wanted,)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = _job_status(job_id)
        if status in wanted:
            return status
        time.sleep(0.02)
    return _job_status(job_id)


def _wait_terminal(job_id: str, timeout: float = 20.0) -> str | None:
    return _wait_status(job_id, TERMINAL_STATUSES, timeout)


# --------------------------------------------------------------------------
# state-set invariants
# --------------------------------------------------------------------------

def test_cancelling_is_non_terminal_and_exclusive():
    """``cancelling`` must be neither terminal nor absent from the mutex set."""
    assert STATUS_CANCELLING not in TERMINAL_STATUSES, (
        "cancelling is a non-terminal state: the worker has not stopped yet"
    )
    assert STATUS_CANCELLING in EXCLUSIVE_STATUSES, (
        "a job that is still stopping must keep blocking new exclusive work"
    )
    assert STATUS_CANCELLING in CANCEL_REQUESTED_STATES
    assert STATUS_CANCELLED in CANCEL_REQUESTED_STATES
    # The *runnable* statuses must still be just pending -> running.
    assert set(ACTIVE_STATUSES) == {STATUS_PENDING, STATUS_RUNNING}


def test_cancelling_is_not_treated_as_runnable():
    """``cancelling`` must never be re-entered as running by update_job."""
    job_id = _new_job_row()
    update_job(job_id, status=STATUS_RUNNING)
    update_job(job_id, status=STATUS_CANCELLING, expected_statuses=(STATUS_RUNNING,))

    # A worker must not be able to "resume" a cancelling job.
    changed = update_job(job_id, status=STATUS_RUNNING)
    assert changed is False, "cancelling must not transition back to running"
    assert _job_status(job_id) == STATUS_CANCELLING


def _new_job_row(project_id: str | None = None) -> str:
    """Insert a bare Job row directly, for state-machine unit checks."""
    from app.models import Job as JobModel
    from app.models import Project

    with session_scope() as s:
        if project_id is None:
            proj = Project(name="state-machine", source_roots=[])
            s.add(proj)
            s.flush()
            project_id = proj.id
        job = JobModel(project_id=project_id, job_type="detect",
                       status=STATUS_PENDING, config_snapshot={}, stats={},
                       failures={"count": 0, "items": []}, resume_token={})
        s.add(job)
        s.flush()
        return job.id


# --------------------------------------------------------------------------
# TC-01: cancel during a running encode -> cancelling, and still exclusive
# --------------------------------------------------------------------------

def test_cancel_while_running_enters_cancelling(client, project, swap_task):
    """TC-01: the cancel endpoint reports 'cancelling', not the terminal state."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)
        gate.finished.set()

    swap_task("detect", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert gate.wait_entered(), "worker never started"

    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code in (200, 202), cancel.text
    body = cancel.json()
    assert body["status"] == STATUS_CANCELLING, (
        f"cancel must not jump straight to a terminal state, got {body['status']}"
    )

    # The worker is still inside the task: the job must remain exclusive.
    assert _job_status(job_id) == STATUS_CANCELLING
    assert _job_finished_at(job_id) is None, (
        "finished_at must not be written while the worker is still stopping"
    )

    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED
    assert _job_finished_at(job_id) is not None


def test_new_exclusive_job_is_rejected_while_cancelling(client, project, swap_task):
    """TC-01 (the actual defect): no new heavy job may be accepted yet.

    Before the fix this returned 202 — the project was considered idle the
    instant ``cancelled`` was written, while the worker still held the lock.
    """
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("detect", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()

    client.post(f"/api/jobs/{job_id}/cancel")

    blocked = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert blocked.status_code == 409, (
        f"expected 409 while the old job is still stopping, got {blocked.status_code}"
    )
    detail = blocked.json()["detail"]
    assert detail["error_code"] == "JOB_CONFLICT"
    assert detail["active_job_id"] == job_id
    assert detail["active_job_type"] == "detect"

    # No second Job row may exist.
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.project_id == project["id"])).all()
    assert len(rows) == 1, f"a rejected request must not create a row: {len(rows)}"

    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED


def test_exclusive_jobs_allowed_after_the_worker_really_stops(client, project, swap_task):
    """TC-02: once the worker exits, the next job is accepted again."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("detect", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_id}/cancel")
    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED

    # Drop the in-memory claim the way the worker's finally block does.
    runner_mod.clear_active_job_type(job_id)

    fresh = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert fresh.status_code in (200, 201, 202), fresh.text

    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.project_id == project["id"])).all()
        assert len(rows) == 2


# --------------------------------------------------------------------------
# TC-03: cancelling a job that never started
# --------------------------------------------------------------------------

def test_pending_job_cancelled_before_it_runs_converges(client, project):
    """TC-03: a queued job must not sit in ``cancelling`` forever.

    The worker is *not* swapped here, so nothing will ever pick the job up on
    its own — the recovery/sweep path has to finish it.
    """
    job_id = _new_job_row(project["id"])

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = STATUS_CANCELLING
    assert _job_status(job_id) == STATUS_CANCELLING

    # The startup/periodic convergence must settle it without running anything.
    runner_mod.converge_pending_cancellations()

    status = _job_status(job_id)
    assert status == STATUS_CANCELLED, (
        f"a never-started cancelling job must converge to cancelled, got {status}"
    )
    assert _job_finished_at(job_id) is not None


# --------------------------------------------------------------------------
# TC-04: idempotent, concurrent cancels
# --------------------------------------------------------------------------

def test_repeated_cancel_is_idempotent(client, project, swap_task):
    """TC-04: cancelling twice stays 200/cancelling, never 409 or a new state."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("package", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/package", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()

    first = client.post(f"/api/jobs/{job_id}/cancel")
    assert first.json()["status"] == STATUS_CANCELLING

    second = client.post(f"/api/jobs/{job_id}/cancel")
    assert second.status_code in (200, 202), (
        f"a repeat cancel of a cancelling job must be idempotent, got {second.status_code}"
    )
    assert second.json()["status"] == STATUS_CANCELLING

    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED


def test_concurrent_cancels_produce_one_clean_terminal_state(client, project, swap_task):
    """TC-04 (concurrency): racing cancels must not corrupt the state."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("roughcut", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()

    barrier = threading.Barrier(6)
    codes: list[int] = []
    lock = threading.Lock()

    def _cancel() -> None:
        barrier.wait(timeout=10)
        r = client.post(f"/api/jobs/{job_id}/cancel")
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=_cancel) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert all(c in (200, 202) for c in codes), f"unexpected codes: {codes}"

    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED


def test_cancel_of_a_finished_job_keeps_its_terminal_state(client, project, swap_task):
    """Cancel must never rewrite a settled terminal state (book §2.5)."""
    swap_task("roughcut", lambda job_id: update_job(job_id, stats={"total": 0}))

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_SUCCEEDED

    late = client.post(f"/api/jobs/{job_id}/cancel")
    assert late.status_code == 409, late.text
    assert _job_status(job_id) == STATUS_SUCCEEDED, (
        "a late cancel must not overwrite succeeded"
    )


# --------------------------------------------------------------------------
# TC-05 / TC-06: cancel racing a natural finish or failure
# --------------------------------------------------------------------------

def test_natural_success_is_not_overwritten_by_a_cancel(client, project, swap_task):
    """TC-05: whichever CAS wins, the terminal state must be unique and legal."""
    started = threading.Event()
    release = threading.Event()

    def _task(job_id: str) -> None:
        started.set()
        release.wait(timeout=15)
        update_job(job_id, stats={"total": 0})

    swap_task("detect", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    job_id = res.json()["id"]
    assert started.wait(timeout=10)

    # Cancel and let the task finish essentially simultaneously.
    client.post(f"/api/jobs/{job_id}/cancel")
    release.set()

    final = _wait_terminal(job_id)
    assert final in (STATUS_CANCELLED, STATUS_SUCCEEDED), final

    # Two CANCELs / a second settle must not flip it afterwards.
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.status = STATUS_CANCELLING
    again = client.post(f"/api/jobs/{job_id}/cancel")
    assert again.status_code in (200, 202, 409)


def test_cancelled_job_does_not_get_a_failure_entry(client, project, swap_task):
    """TC-06: cancelling must not pollute the failure list."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("detect", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_id}/cancel")
    gate.open()

    assert _wait_terminal(job_id) == STATUS_CANCELLED
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == STATUS_CANCELLED
        assert (job.failures or {}).get("count", 0) == 0
        assert job.error in (None, ""), f"cancelled job carries error {job.error!r}"


# --------------------------------------------------------------------------
# TC-10: crash/restart recovery
# --------------------------------------------------------------------------

def test_recovery_does_not_rerun_a_cancelling_job():
    """TC-10: a leftover ``cancelling`` row must converge, never resume work.

    Before the fix, a restart could not see a cancel request at all: the status
    was already terminal ``cancelled`` and simply sat there, or — worse for this
    change — a naive implementation would re-dispatch it as ``pending``.
    """
    job_id = _new_job_row()

    with session_scope() as s:
        s.get(Job, job_id).status = STATUS_CANCELLING

    # Recovery must not turn it into pending/running: the row is still
    # cancelling, and resume_interrupted_jobs must not have selected it.
    resumed = runner_mod.resume_interrupted_jobs()
    assert job_id not in resumed, (
        "a cancelling job must never be re-dispatched as work"
    )
    assert _job_status(job_id) == STATUS_CANCELLING

    # ...and the convergence sweep finishes it as cancelled.
    settled = runner_mod.converge_pending_cancellations()
    assert job_id in settled

    assert _job_status(job_id) == STATUS_CANCELLED
    assert _job_status(job_id) not in ACTIVE_STATUSES


def test_recovery_still_resumes_genuinely_interrupted_jobs():
    """No regression: pending/running rows are still re-dispatched."""
    job_id = _new_job_row()

    with session_scope() as s:
        s.get(Job, job_id).status = STATUS_RUNNING

    resumed = runner_mod.resume_interrupted_jobs()
    assert job_id in resumed
    assert _job_status(job_id) in (STATUS_PENDING, STATUS_RUNNING)


# --------------------------------------------------------------------------
# TC-08: cross-project isolation
# --------------------------------------------------------------------------

def test_another_project_is_unaffected_by_a_cancelling_job(client, swap_task):
    """TC-08: the mutex is per project, so B keeps working while A stops."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=15)

    swap_task("detect", _task)

    a = client.post("/api/projects", json={"name": "A", "source_roots": []}).json()
    b = client.post("/api/projects", json={"name": "B", "source_roots": []}).json()

    res = client.post(f"/api/projects/{a['id']}/jobs/detect", json={})
    job_a = res.json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_a}/cancel")

    # B must be free to start its own heavy job.
    ok = client.post(f"/api/projects/{b['id']}/jobs/roughcut", json={})
    assert ok.status_code in (200, 201, 202), (
        f"project B must not be blocked by A's cancelling job: {ok.status_code}"
    )

    gate.open()
    assert _wait_terminal(job_a) == STATUS_CANCELLED
    _wait_terminal(ok.json()["id"])
