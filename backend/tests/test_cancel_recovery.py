"""TSB-264 — end-to-end regression matrix around the cancel lifecycle.

Covers the remaining acceptance cases from the task book §6 that are about how
the cancellation *surface* behaves rather than the state machine or the media
layer in isolation:

* **TC-24** — after cancelling over HTTP, the job list the UI reads must agree
  with the state, including which job is blocking the project.
* **TC-25** — a cancelled detect/scan must not damage checkpoints or the user's
  own work: ratings, notes, trims and annotations survive, and re-running
  detection still preserves them.
* **TC-26** — several projects starting and settling repeatedly must not
  deadlock, double-run, or delete each other's artefacts.
* **TC-29** — while a cancelled job is still cleaning up, starting the same work
  again stays blocked.

The book's discipline is applied here too: concurrency is synchronised with
events and observable conditions, never ``sleep(0.1)`` as a proxy for a race.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, EditPlanShot, Frame, Job, Segment, UserAnnotation
from app.paths import project_cache_dir
from app.services import runner as runner_mod
from app.services.runner import (
    STATUS_CANCELLED,
    STATUS_CANCELLING,
    STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
    TASK_REGISTRY,
)

pytestmark = pytest.mark.usefixtures("_app_ready")


class Gate:
    """A worker-side rendezvous the test observes and releases."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_entered(self, timeout: float = 10.0) -> bool:
        return self.entered.wait(timeout)

    def open(self) -> None:
        self.release.set()


@pytest.fixture()
def swap_task():
    original = dict(TASK_REGISTRY)

    def _install(job_type: str, fn):
        TASK_REGISTRY[job_type] = fn

    yield _install
    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)


def _make_project(client, name: str) -> dict:
    res = client.post("/api/projects", json={"name": name, "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _job_row(job_id: str) -> Job | None:
    with session_scope() as s:
        return s.get(Job, job_id)


def _wait_status(job_id: str, wanted, timeout: float = 20.0) -> str | None:
    if isinstance(wanted, str):
        wanted = (wanted,)
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _job_row(job_id)
        if row is not None and row.status in wanted:
            return row.status
        time.sleep(0.02)
    row = _job_row(job_id)
    return row.status if row else None


def _wait_terminal(job_id: str, timeout: float = 25.0) -> str | None:
    return _wait_status(job_id, TERMINAL_STATUSES, timeout)


# ==========================================================================
# TC-24: the HTTP surface must agree with the state
# ==========================================================================

def test_job_list_reports_cancelling_and_the_blocking_job(client, swap_task):
    """TC-24: the list the UI polls must show the in-progress cancellation."""
    gate = Gate()

    def _task(job_id: str) -> None:
        gate.entered.set()
        gate.release.wait(timeout=20)

    swap_task("detect", _task)

    project = _make_project(client, "tc24")
    res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    job_id = res.json()["id"]
    assert gate.wait_entered()

    client.post(f"/api/jobs/{job_id}/cancel")

    listed = client.get(f"/api/projects/{project['id']}/jobs")
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_CANCELLING, (
        f"the job list must expose `cancelling`, got {rows[0]['status']}"
    )
    # The blocking job is identifiable, which is what the UI message needs.
    blocked = client.post(f"/api/projects/{project['id']}/jobs/scan", json={})
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["active_job_id"] == job_id
    assert detail["active_job_type"] == "detect"

    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED

    # After it settles, the list shows the terminal state and the project frees up.
    rows = client.get(f"/api/projects/{project['id']}/jobs").json()
    assert rows[0]["status"] == STATUS_CANCELLED
    assert rows[0]["finished_at"] is not None, (
        "finished_at must be set once the worker really stopped"
    )

    retry = client.post(f"/api/projects/{project['id']}/jobs/scan", json={})
    assert retry.status_code in (200, 201, 202), retry.text
    _wait_terminal(retry.json()["id"])


def test_single_job_endpoint_matches_the_list(client, swap_task):
    """TC-24: `/jobs/{id}` and the project list must not disagree."""
    gate = Gate()
    swap_task("package", lambda job_id: (gate.entered.set(),
                                         gate.release.wait(timeout=20)))

    project = _make_project(client, "tc24b")
    job_id = client.post(f"/api/projects/{project['id']}/jobs/package",
                         json={}).json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_id}/cancel")

    single = client.get(f"/api/jobs/{job_id}").json()
    listed = client.get(f"/api/projects/{project['id']}/jobs").json()[0]
    assert single["status"] == listed["status"] == STATUS_CANCELLING

    gate.open()
    _wait_terminal(job_id)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == STATUS_CANCELLED


# ==========================================================================
# TC-25: user work survives a cancelled run
# ==========================================================================

def _seed_annotated_library(project_id: str) -> dict:
    """One asset with a segment carrying every kind of user-owned state.

    Inserted with explicit ``flush()`` between the asset, the segment and the
    frame: the three are linked by foreign keys, and letting SQLAlchemy order a
    single flush is not guaranteed to create the parent first.
    """
    asset_id, segment_id, frame_id = (str(uuid.uuid4()) for _ in range(3))
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path="C:/media/keep.mp4",
                    display_name="keep.mp4", duration_sec=30.0, scan_status="succeeded"))
        s.flush()
        s.add(Segment(id=segment_id, project_id=project_id, asset_id=asset_id,
                      detected_start_sec=1.0, detected_end_sec=5.0,
                      start_sec=1.5, end_sec=4.5, is_trimmed=True,
                      user_rating="favorite", story_role="opening",
                      status="selected"))
        s.flush()
        s.add(Frame(id=frame_id, segment_id=segment_id, asset_id=asset_id,
                    time_sec=2.0, image_path="frames/keep/2.jpg",
                    is_representative=True))
        s.flush()
        s.add(UserAnnotation(id=str(uuid.uuid4()), segment_id=segment_id,
                             rating="favorite", note="黄金时刻，保留",
                             in_sec=1.5, out_sec=4.5))
        s.add(EditPlanShot(id=str(uuid.uuid4()), project_id=project_id, order=0,
                           asset_id=asset_id, segment_id=segment_id,
                           source_in_sec=1.5, source_out_sec=4.5,
                           target_duration_sec=3.0))
    return {"asset_id": asset_id, "segment_id": segment_id, "frame_id": frame_id}


def test_cancelled_run_preserves_ratings_notes_trims_and_plan(client, swap_task):
    """TC-25: cancelling must not touch the user's own state."""
    gate = Gate()
    swap_task("detect", lambda job_id: (gate.entered.set(),
                                        gate.release.wait(timeout=20)))

    project = _make_project(client, "tc25")
    ids = _seed_annotated_library(project["id"])

    job_id = client.post(f"/api/projects/{project['id']}/jobs/detect",
                         json={}).json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_id}/cancel")
    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED

    with session_scope() as s:
        seg = s.get(Segment, ids["segment_id"])
        assert seg is not None, "a cancelled run must not delete segments"
        assert seg.user_rating == "favorite"
        assert seg.story_role == "opening"
        assert seg.is_trimmed is True
        assert (seg.start_sec, seg.end_sec) == (1.5, 4.5), "manual trim was changed"
        assert (seg.detected_start_sec, seg.detected_end_sec) == (1.0, 5.0), (
            "immutable detection bounds were changed"
        )

        note = s.scalars(select(UserAnnotation).where(
            UserAnnotation.segment_id == ids["segment_id"])).first()
        assert note is not None and note.note == "黄金时刻，保留"

        frame = s.get(Frame, ids["frame_id"])
        assert frame is not None, "a committed frame reference was dropped"

        shot = s.scalars(select(EditPlanShot).where(
            EditPlanShot.project_id == project["id"])).first()
        assert shot is not None and shot.segment_id == ids["segment_id"]


def test_rerun_after_a_cancel_still_works_and_keeps_user_state(client, swap_task):
    """TC-25: the project must be usable again, with state intact."""
    gate = Gate()
    calls: list[str] = []

    def _task(job_id: str) -> None:
        calls.append(job_id)
        if len(calls) == 1:
            gate.entered.set()
            gate.release.wait(timeout=20)
        # The second run completes normally.

    swap_task("detect", _task)

    project = _make_project(client, "tc25b")
    ids = _seed_annotated_library(project["id"])

    first = client.post(f"/api/projects/{project['id']}/jobs/detect",
                        json={}).json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{first}/cancel")
    gate.open()
    assert _wait_terminal(first) == STATUS_CANCELLED
    runner_mod.clear_active_job_type(first)

    second = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
    assert second.status_code in (200, 201, 202), (
        f"the project must be reusable after the worker stopped: {second.text}"
    )
    assert _wait_terminal(second.json()["id"]) == STATUS_SUCCEEDED

    with session_scope() as s:
        seg = s.get(Segment, ids["segment_id"])
        assert seg.user_rating == "favorite", "re-run lost the user's rating"
        assert seg.is_trimmed is True, "re-run lost the manual trim"


def test_cancelled_scan_preserves_checkpoints(client, swap_task):
    """TC-25: cancelling a scan keeps the resume token/checkpoints it had."""
    gate = Gate()

    def _task(job_id: str) -> None:
        # Persist a checkpoint, then wait to be cancelled.
        from app.services.runner import update_job

        update_job(job_id, resume_token={"done": ["a", "b"], "cursor": 2})
        gate.entered.set()
        gate.release.wait(timeout=20)

    swap_task("scan", _task)

    project = _make_project(client, "tc25c")
    job_id = client.post(f"/api/projects/{project['id']}/jobs/scan",
                         json={}).json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_id}/cancel")
    gate.open()
    assert _wait_terminal(job_id) == STATUS_CANCELLED

    with session_scope() as s:
        row = s.get(Job, job_id)
        assert (row.resume_token or {}).get("done") == ["a", "b"], (
            "cancelling must keep the checkpoint so the next run can resume"
        )


# ==========================================================================
# TC-26: several projects, repeated start/settle
# ==========================================================================

def test_multiple_projects_cancel_concurrently_without_deadlock(client, swap_task):
    """TC-26: several projects cancelling must all settle, independently.

    Sized to the runner's worker pool (``max_concurrency``, default 2): jobs
    beyond it legitimately stay ``pending``, so asserting that every one of them
    "starts" would test the pool size rather than the cancellation contract. The
    point here is that cancelling across projects never deadlocks and every job
    reaches a terminal state.
    """
    from app.config import get_settings

    workers = max(1, get_settings().max_concurrency)
    project_count = workers

    # One gate per *project*, resolved by the job's project id. Keying by job id
    # would race: the worker can start before the test has recorded the id it
    # was just handed, and the lookup would then KeyError inside the task.
    gates: dict[str, Gate] = {}

    def _task(job_id: str) -> None:
        with session_scope() as s:
            project_id = s.get(Job, job_id).project_id
        gate = gates.setdefault(project_id, Gate())
        gate.entered.set()
        gate.release.wait(timeout=30)

    swap_task("detect", _task)

    projects = [_make_project(client, f"tc26-{i}") for i in range(project_count)]
    for project in projects:
        gates[project["id"]] = Gate()

    job_ids: list[str] = []
    for project in projects:
        res = client.post(f"/api/projects/{project['id']}/jobs/detect", json={})
        assert res.status_code in (200, 201, 202), res.text
        job_id = res.json()["id"]
        job_ids.append(job_id)
        # The gate already exists (keyed by project), so a worker that starts
        # immediately still finds it — no window where the lookup can miss.

    for project in projects:
        assert gates[project["id"]].wait_entered(20), (
            f"project {project['id']} job never started"
        )

    # Cancel them all from separate threads, as a user clicking across tabs would.
    barrier = threading.Barrier(project_count)
    codes: list[int] = []
    lock = threading.Lock()

    def _cancel(job_id: str) -> None:
        barrier.wait(timeout=15)
        res = client.post(f"/api/jobs/{job_id}/cancel")
        with lock:
            codes.append(res.status_code)

    threads = [threading.Thread(target=_cancel, args=(jid,)) for jid in job_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(c in (200, 202) for c in codes), codes

    for project in projects:
        gates[project["id"]].open()

    for job_id in job_ids:
        assert _wait_terminal(job_id) == STATUS_CANCELLED, (
            f"{job_id} did not settle — possible deadlock"
        )


def test_queued_job_beyond_the_pool_is_cancelled_without_running(client, swap_task):
    """TC-03/TC-26: a job that never got a worker must still converge.

    Submitting more exclusive jobs than the pool allows leaves the extras
    ``pending`` (correct). Cancelling such a job must not leave it stuck in
    ``cancelling`` with the project locked — the convergence path has to settle
    it, and no work may run for it.
    """
    from app.config import get_settings
    from app.services.runner import converge_pending_cancellations

    workers = max(1, get_settings().max_concurrency)
    gates: dict[str, Gate] = {}
    ran: list[str] = []
    lock = threading.Lock()

    # One gate per project: the first `workers` projects occupy the pool.
    def _task(job_id: str) -> None:
        with session_scope() as s:
            project_id = s.get(Job, job_id).project_id
        with lock:
            ran.append(job_id)
        gate = gates.setdefault(project_id, Gate())
        gate.entered.set()
        gate.release.wait(timeout=30)

    swap_task("detect", _task)

    projects = [_make_project(client, f"tc26q-{i}") for i in range(workers + 1)]
    for project in projects:
        gates[project["id"]] = Gate()

    busy_ids = [client.post(f"/api/projects/{p['id']}/jobs/detect", json={}).json()["id"]
                for p in projects[:workers]]
    for project in projects[:workers]:
        assert gates[project["id"]].wait_entered(20)

    # The extra project is a *different* project, so it is not blocked by the
    # exclusivity rule; it simply has no free worker yet.
    extra = client.post(f"/api/projects/{projects[workers]['id']}/jobs/detect",
                        json={})
    assert extra.status_code in (200, 201, 202), extra.text
    queued_id = extra.json()["id"]

    # It may start if a worker freed up; if not, it stays pending. Either way,
    # cancelling it must settle it and never run its body twice.
    client.post(f"/api/jobs/{queued_id}/cancel")
    converge_pending_cancellations()

    assert _wait_terminal(queued_id) == STATUS_CANCELLED, (
        "a queued job whose cancel was requested must converge to cancelled"
    )
    assert ran.count(queued_id) <= 1, "the queued job ran more than once"

    for project in projects[:workers]:
        gates[project["id"]].open()
    for job_id in busy_ids:
        _wait_terminal(job_id)


def test_interleaved_create_and_cancel_across_projects(client, swap_task):
    """TC-26: repeated create/cancel cycles must not double-run or deadlock."""
    started: list[str] = []
    lock = threading.Lock()

    def _task(job_id: str) -> None:
        with lock:
            started.append(job_id)
        # Exit immediately: we only care about the scheduling contract.

    swap_task("roughcut", _task)

    projects = [_make_project(client, f"tc26b-{i}") for i in range(3)]
    created: list[str] = []
    for _round in range(3):
        for project in projects:
            res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
            if res.status_code in (200, 201, 202):
                created.append(res.json()["id"])
            else:
                assert res.status_code == 409, res.text
        for job_id in list(created):
            client.post(f"/api/jobs/{job_id}/cancel")
        for job_id in list(created):
            _wait_terminal(job_id)
        created.clear()

    # Each job must have run at most once.
    assert len(started) == len(set(started)), (
        f"a job was dispatched more than once: {started}"
    )


def test_artifacts_are_not_cross_deleted_between_projects(client, swap_task):
    """TC-26: project isolation must hold through the cleanup path."""
    gate = Gate()
    swap_task("detect", lambda job_id: (gate.entered.set(),
                                        gate.release.wait(timeout=20)))

    a = _make_project(client, "tc26c-A")
    b = _make_project(client, "tc26c-B")

    # Give each project a private artifact, as a real run would.
    marker_a = project_cache_dir(a["id"]) / "roughcut" / "roughcut.mp4"
    marker_b = project_cache_dir(b["id"]) / "roughcut" / "roughcut.mp4"
    for marker, payload in ((marker_a, b"A-RELEASE"), (marker_b, b"B-RELEASE")):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(payload)

    job_a = client.post(f"/api/projects/{a['id']}/jobs/detect", json={}).json()["id"]
    assert gate.wait_entered()
    client.post(f"/api/jobs/{job_a}/cancel")
    gate.open()
    assert _wait_terminal(job_a) == STATUS_CANCELLED

    assert marker_a.read_bytes() == b"A-RELEASE", "A's artifact was damaged"
    assert marker_b.read_bytes() == b"B-RELEASE", (
        "cancelling a job in project A deleted project B's artifact"
    )


# ==========================================================================
# TC-29: starting the same work again while cleanup is still running
# ==========================================================================

def test_repeat_request_stays_blocked_until_cleanup_finishes(client, swap_task):
    """TC-29: cleanup and worker unlock are independently observable phases."""
    cleanup_started, cleanup_done = threading.Event(), threading.Event()
    task_entered, release = threading.Event(), threading.Event()
    finish_cleanup = threading.Event()

    def _task(job_id: str) -> None:
        task_entered.set()
        assert release.wait(20)
        cleanup_started.set()
        assert finish_cleanup.wait(20)
        cleanup_done.set()

    swap_task("detect", _task)
    project = _make_project(client, "tc29")
    pid = project['id']
    response = client.post(f"/api/projects/{pid}/jobs/detect", json={})
    assert response.status_code == 202
    job_id = response.json()['id']
    try:
        assert task_entered.wait(10)
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        for in_cleanup in (False, True):
            if in_cleanup:
                release.set()
                assert cleanup_started.wait(10)
            with session_scope() as s:
                before = len(s.scalars(select(Job).where(Job.project_id == pid)).all())
                row = s.get(Job, job_id)
                assert row.status == STATUS_CANCELLING and row.finished_at is None
            blocked = client.post(f"/api/projects/{pid}/jobs/detect", json={})
            assert blocked.status_code == 409, blocked.text
            with session_scope() as s:
                assert len(s.scalars(select(Job).where(Job.project_id == pid)).all()) == before
        finish_cleanup.set()
        assert cleanup_done.wait(10)
        # A visible terminal row precedes unlock by design. Wait for the
        # entire worker, not for a DB snapshot or a scheduling sleep.
        with runner_mod.runner._submit_lock:
            future = runner_mod.runner._futures[job_id]
        future.result(15)
        assert client.get(f"/api/jobs/{job_id}").json()['status'] == STATUS_CANCELLED
        reopened = client.post(f"/api/projects/{pid}/jobs/detect", json={})
        assert reopened.status_code == 202, reopened.text
        with runner_mod.runner._submit_lock:
            next_future = runner_mod.runner._futures[reopened.json()['id']]
        next_future.result(15)
    finally:
        release.set()
        finish_cleanup.set()
