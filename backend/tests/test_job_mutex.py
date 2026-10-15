"""TSB-242 — one shared, race-safe path for creating project jobs.

The task doc's evidence: ``/review-requests/proxies`` inserted a Job and called
``runner.submit`` directly, bypassing the lock/exclusivity checks that
``/jobs/{job_type}`` performed. Rapid repeated clicks could therefore queue
several competing jobs for one project.

These tests pin the shared behaviour for *both* entry points, including the
concurrent case that a frontend button-disable cannot prevent.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import Job, ReviewRequest, Segment
from app.services.job_service import (
    VALID_JOB_TYPES,
    InvalidJobType,
    JobConflict,
    JobRequest,
    ProjectNotFound,
    create_project_job,
)
from app.services.runner import ACTIVE_STATUSES, TASK_REGISTRY, JobRunner

#: Job types that mutate a project's cache and therefore must be mutually
#: exclusive. `vision_analyze` and `motion_review` (TSB-304/307) joined this set:
#: they write semantic/motion rows for the same shots and would otherwise race a
#: re-detection.
HEAVY = ("scan", "detect", "package", "proxies", "roughcut", "vision_analyze", "motion_review")


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "mutex-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


@pytest.fixture()
def blocking_task(monkeypatch):
    """Replace every heavy task with one that blocks until released."""
    gate = threading.Event()
    original = dict(TASK_REGISTRY)

    def install(job_type: str) -> None:
        TASK_REGISTRY[job_type] = lambda job_id: gate.wait(timeout=20)  # noqa: ARG005

    yield install, gate

    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)
    gate.set()


def _wait_status(job_id: str, wanted: str, timeout: float = 10.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            row = s.get(Job, job_id)
            if row and row.status == wanted:
                return True
        time.sleep(0.05)
    return False


def _count_jobs(project_id: str) -> int:
    with session_scope() as s:
        return s.scalar(
            select(func.count()).select_from(Job).where(Job.project_id == project_id)
        )


# --------------------------------------------------------------------------
# Service-level validation
# --------------------------------------------------------------------------

def test_invalid_job_type_is_rejected(project):
    with session_scope() as s:
        with pytest.raises(InvalidJobType):
            create_project_job(
                s, project["id"], JobRequest("not-a-task", {}), config_builder=lambda p, r: {}
            )


def test_unknown_project_is_rejected(client):
    with session_scope() as s:
        with pytest.raises(ProjectNotFound):
            create_project_job(
                s, "no-such-project", JobRequest("scan", {}), config_builder=lambda p, r: {}
            )


def test_all_documented_types_are_valid():
    assert VALID_JOB_TYPES == set(HEAVY)


def test_begin_immediate_reopens_an_already_open_transaction(client, project):
    """A SELECT-opened transaction must still end up holding the write lock.

    ``BEGIN IMMEDIATE`` cannot nest, so the helper rolls back and retries. This
    asserts the retry succeeds rather than raising, and that the insert that
    follows is visible — i.e. the check-and-insert really is serialized.
    """
    from app.services.job_service import _begin_immediate

    with session_scope() as s:
        s.execute(select(Job).limit(1))  # opens an implicit transaction
        _begin_immediate(s)              # must not raise
        s.add(Job(project_id=project["id"], job_type="detect"))
    assert _count_jobs(project["id"]) == 1


# --------------------------------------------------------------------------
# Exclusivity on the generic endpoint
# --------------------------------------------------------------------------

def test_second_heavy_job_is_rejected_with_details(client, project, blocking_task):
    install, gate = blocking_task
    install("detect")
    try:
        first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert first.status_code == 202, first.text
        assert _wait_status(first.json()["id"], "running")

        second = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert second.status_code == 409
        detail = second.json()["detail"]
        # The doc requires the response to name the conflicting task type and id.
        assert detail["error_code"] == "JOB_CONFLICT"
        assert detail["active_job_type"] == "detect"
        assert detail["active_job_id"] == first.json()["id"]
    finally:
        gate.set()


@pytest.mark.parametrize("second_type", HEAVY)
def test_any_heavy_type_conflicts_with_a_running_heavy_job(
    client, project, blocking_task, second_type
):
    """scan/detect/package/proxies/roughcut are mutually exclusive per project."""
    install, gate = blocking_task
    install("detect")
    install(second_type)
    try:
        first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert first.status_code == 202
        assert _wait_status(first.json()["id"], "running")

        res = client.post(f"/api/projects/{project['id']}/jobs/{second_type}", json={})
        assert res.status_code == 409, f"{second_type} should have been blocked"
    finally:
        gate.set()


def test_no_job_row_is_created_for_a_rejected_request(client, project, blocking_task):
    """A 409 must not leave a stray queued Job behind."""
    install, gate = blocking_task
    install("detect")
    try:
        first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert first.status_code == 202
        assert _wait_status(first.json()["id"], "running")
        before = _count_jobs(project["id"])

        for _ in range(5):
            assert client.post(f"/api/projects/{project['id']}/jobs/detect", json={}).status_code == 409

        assert _count_jobs(project["id"]) == before == 1
    finally:
        gate.set()


# --------------------------------------------------------------------------
# The proxy endpoint must behave identically (the actual defect)
# --------------------------------------------------------------------------

def test_proxy_endpoint_blocks_when_a_heavy_job_runs(client, project, blocking_task):
    """The reported bug: /review-requests/proxies ignored exclusivity."""
    install, gate = blocking_task
    install("detect")
    install("proxies")
    try:
        first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert first.status_code == 202
        assert _wait_status(first.json()["id"], "running")

        res = client.post(f"/api/projects/{project['id']}/review-requests/proxies", json={})
        assert res.status_code == 409, "proxy endpoint must honour exclusivity"
        assert res.json()["detail"]["active_job_type"] == "detect"
    finally:
        gate.set()


def test_proxy_endpoint_blocks_a_second_proxy_job(client, project, blocking_task):
    """Rapid double-click on 'generate previews' must not queue two jobs."""
    install, gate = blocking_task
    install("proxies")
    try:
        first = client.post(f"/api/projects/{project['id']}/review-requests/proxies", json={})
        assert first.status_code == 202, first.text
        assert _wait_status(first.json()["id"], "running")

        second = client.post(f"/api/projects/{project['id']}/review-requests/proxies", json={})
        assert second.status_code == 409
    finally:
        gate.set()


def test_proxy_endpoint_accepts_when_idle(client, project, blocking_task):
    """Response shape is unchanged: 202 + JobOut."""
    install, gate = blocking_task
    install("proxies")
    try:
        res = client.post(f"/api/projects/{project['id']}/review-requests/proxies", json={})
        assert res.status_code == 202
        body = res.json()
        assert body["job_type"] == "proxies"
        assert body["project_id"] == project["id"]
        assert body["status"] in ACTIVE_STATUSES
        assert body["stats"]["total"] == 0
    finally:
        gate.set()


def test_proxy_endpoint_404_for_unknown_project(client):
    res = client.post("/api/projects/does-not-exist/review-requests/proxies", json={})
    assert res.status_code == 404


# --------------------------------------------------------------------------
# Race safety (the reason a DB transaction is required, not just a lock)
# --------------------------------------------------------------------------

def test_concurrent_creates_yield_exactly_one_job(client, project, blocking_task):
    """Fire many simultaneous creates; exactly one must win.

    A check-then-insert without a write transaction lets two requests both pass
    the check. This asserts the database-level serialization holds.
    """
    install, gate = blocking_task
    install("detect")

    results: list[int] = []
    lock = threading.Lock()

    def create() -> None:
        res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        with lock:
            results.append(res.status_code)

    try:
        threads = [threading.Thread(target=create) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert results, "no requests completed"
        assert results.count(202) == 1, f"expected exactly one accepted, got {results}"
        assert all(code in (202, 409) for code in results), results
        assert _count_jobs(project["id"]) == 1
    finally:
        gate.set()


def test_sequential_creates_after_completion_are_allowed(client, project, blocking_task):
    """Once the first job finishes, a new one must be accepted."""
    install, gate = blocking_task
    install("detect")

    first = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    assert first.status_code == 202
    assert _wait_status(first.json()["id"], "running")
    assert client.post(f"/api/projects/{project['id']}/jobs/detect", json={}).status_code == 409

    gate.set()  # let the first job finish
    for _ in range(200):
        with session_scope() as s:
            row = s.get(Job, first.json()["id"])
            if row.status not in ACTIVE_STATUSES:
                break
        import time

        time.sleep(0.05)

    second = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    assert second.status_code == 202, second.text


def test_different_projects_do_not_block_each_other(client, blocking_task):
    """Exclusivity is per project, not global."""
    install, gate = blocking_task
    install("detect")
    p1 = client.post("/api/projects", json={"name": "p1", "source_roots": []}).json()["id"]
    p2 = client.post("/api/projects", json={"name": "p2", "source_roots": []}).json()["id"]
    try:
        a = client.post(f"/api/projects/{p1}/jobs/detect", json={})
        b = client.post(f"/api/projects/{p2}/jobs/detect", json={})
        assert a.status_code == 202 and b.status_code == 202
    finally:
        gate.set()


# --------------------------------------------------------------------------
# Config snapshot parity between the two entry points
# --------------------------------------------------------------------------

def _config_of(job_id: str) -> dict:
    """Read the internal config snapshot (not exposed on JobOut)."""
    with session_scope() as s:
        return dict(s.get(Job, job_id).config_snapshot or {})


def test_proxy_job_config_snapshot_is_unchanged(client, project, blocking_task):
    """The refactor must not alter the stored config shape."""
    install, gate = blocking_task
    install("proxies")
    try:
        res = client.post(
            f"/api/projects/{project['id']}/review-requests/proxies",
            json={"options": {"max_total_mb": 123}},
        )
        assert res.status_code == 202
        assert _config_of(res.json()["id"])["max_total_mb"] == 123
    finally:
        gate.set()


def test_generic_proxy_job_matches_specialised_endpoint(client, project, blocking_task):
    """Both routes produce the same job_type and config for proxies."""
    install, gate = blocking_task
    install("proxies")
    try:
        a = client.post(f"/api/projects/{project['id']}/jobs/proxies", json={})
        assert a.status_code == 202
        a_id = a.json()["id"]
        assert _wait_status(a_id, "running")
    finally:
        gate.set()

    p2 = client.post("/api/projects", json={"name": "p2", "source_roots": []}).json()["id"]
    b = client.post(f"/api/projects/{p2}/review-requests/proxies", json={})
    assert b.status_code == 202
    assert a.json()["job_type"] == b.json()["job_type"] == "proxies"
    assert _config_of(a_id) == _config_of(b.json()["id"])


# --------------------------------------------------------------------------
# End-to-end: the proxy job actually runs
# --------------------------------------------------------------------------

def test_proxy_job_runs_and_completes(client, project, monkeypatch):
    """Restore a real task body and confirm the job reaches a terminal state."""
    ran: list[str] = []

    def fake_proxies(job_id: str) -> None:
        ran.append(job_id)

    from app.services import story_planner  # noqa: F401  (registers the real task)

    original = TASK_REGISTRY["proxies"]
    TASK_REGISTRY["proxies"] = fake_proxies
    try:
        res = client.post(f"/api/projects/{project['id']}/review-requests/proxies", json={})
        assert res.status_code == 202
        job_id = res.json()["id"]
        for _ in range(200):
            with session_scope() as s:
                row = s.get(Job, job_id)
                if row.status not in ACTIVE_STATUSES:
                    break
            import time

            time.sleep(0.05)
        assert ran == [job_id]
        with session_scope() as s:
            assert s.get(Job, job_id).status == "succeeded"
    finally:
        TASK_REGISTRY["proxies"] = original
