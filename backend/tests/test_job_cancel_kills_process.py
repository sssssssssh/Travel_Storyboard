"""TSB-253 — cancelling a job must kill the in-flight media process.

The confirmed defect: cancellation was purely *cooperative*. ``is_cancelled``
was only consulted **between** items, so cancelling a long encode (a 10-minute
proxy, a rough-cut concat) left the FFmpeg child running to completion. The job
row said ``cancelled`` while the project lock stayed held and the CPU kept
burning — sometimes for minutes.

The fix threads an optional ``cancel_check`` callback from the job into
``ffmpeg.run``, which polls it while the child runs, terminates the process
group, and raises ``FFmpegCancelled`` (``code="CANCELLED"``).

Two classes of bug are guarded here:

1. **The process must actually die, promptly.** Asserting only "the job ended
   as cancelled" would pass against the old code, because the old code reached
   that state too — just far too late. These tests therefore measure *when* the
   child exits, and assert the runner stops the encode rather than waiting it
   out.
2. **A killed media process is a cancellation, not a failure.** The runner's
   generic ``except Exception`` used to rewrite the terminal state to
   ``failed``; that would turn a deliberate cancel into a red job.

The companion regression is that the fast path (no ``cancel_check``) keeps its
exact previous behaviour: same signature, same ``subprocess.run`` semantics,
same error classification.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.ffmpeg import (
    ERR_CANCELLED,
    ERR_COMMAND_FAILED,
    ERR_EXECUTABLE_MISSING,
    ERR_TIMEOUT,
    FFmpegCancelled,
    FFmpegError,
    FFmpegTimeout,
    ffmpeg_available,
    run,
)
from app.models import Job
from app.services import runner as runner_mod
from app.services.runner import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    TASK_REGISTRY,
    TERMINAL_STATUSES,
    cancel_check_for,
    is_cancelled,
)
from app.services.runner import JobCancelled


def _ffmpeg_or_skip() -> str:
    """A *usable* ffmpeg path, or skip (fail hard in the mandatory-media job)."""
    exe = ffmpeg_available()
    if not exe:
        if os.environ.get("TSB_REQUIRE_FFMPEG") == "1":
            pytest.fail("mandatory FFmpeg integration requires ffmpeg")
        pytest.skip("ffmpeg not available")
    return exe


def _sleep_cmd(seconds: float) -> list[str]:
    """A stand-in "media process" that runs for *seconds*.

    Uses the interpreter rather than a shell builtin so the child is a real,
    independently killable process on both Windows and POSIX.
    """
    import sys

    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _pid_alive(pid: int) -> bool:
    """Whether *pid* still exists (portable, no external tooling)."""
    if os.name == "nt":
        # On Windows os.kill(pid, 0) is not a liveness probe; OpenProcess is.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "cancel-kill", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


@pytest.fixture()
def swap_task():
    """Temporarily replace a registered task, restoring the original after."""
    original = dict(TASK_REGISTRY)

    def _install(job_type: str, fn):
        TASK_REGISTRY[job_type] = fn

    yield _install
    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)


def _wait_running(job_id: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job and job.status == "running":
                return True
        time.sleep(0.02)
    return False


def _wait_terminal(job_id: str, timeout: float = 20.0) -> str:
    """Wait for a genuinely terminal state (TSB-261 added non-terminal cancelling)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(Job, job_id)
            status = job.status if job else None
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ---------------------------------------------------------------------------
# 1. The media layer: cancel_check stops the child, promptly
# ---------------------------------------------------------------------------

def test_cancelled_run_kills_child_and_raises():
    """A truthy cancel_check must terminate the child and raise CANCELLED."""
    started = time.monotonic()
    with pytest.raises(FFmpegCancelled) as excinfo:
        run(_sleep_cmd(60), cancel_check=lambda: True, poll_interval=0.05)
    elapsed = time.monotonic() - started

    assert excinfo.value.code == ERR_CANCELLED
    # The whole point: we must NOT have waited out the 60s child.
    assert elapsed < 20, f"cancel took {elapsed:.1f}s — the child was not killed"


def test_cancelled_run_actually_reaps_the_os_process():
    """The child must be gone from the OS, not merely abandoned."""
    seen: dict[str, int] = {}

    def _check() -> bool:
        # Capture the pid on the first poll, then report cancelled.
        if "pid" not in seen:
            seen["pid"] = -1
            return False
        return True

    # Spawn via run() but learn the pid through a child that writes it out.
    marker = f"tsb-cancel-pid-{uuid.uuid4().hex}.txt"
    import sys
    import tempfile

    pid_file = os.path.join(tempfile.gettempdir(), marker)
    cmd = [
        sys.executable, "-c",
        f"import os,time;open(r'{pid_file}','w').write(str(os.getpid()));"
        f"time.sleep(60)",
    ]

    def _check_seen() -> bool:
        return os.path.isfile(pid_file)

    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=_check_seen, poll_interval=0.05)

        pid = int(open(pid_file).read().strip())
        # Give the OS a moment to reap, then assert it is gone.
        deadline = time.time() + 10
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        assert not _pid_alive(pid), f"child pid {pid} is still alive after cancel"
    finally:
        if os.path.exists(pid_file):
            os.unlink(pid_file)


def test_uncancelled_run_with_check_still_returns_normally():
    """A cancel_check that stays false must not disturb a successful run."""
    proc = run(_sleep_cmd(0.1), cancel_check=lambda: False, poll_interval=0.05)
    assert proc.returncode == 0


def test_cancel_check_result_is_read_as_truthy_per_poll():
    """The callback is polled repeatedly, so a late cancel still stops the child."""
    state = {"cancelled": False}

    def _flip_later() -> None:
        time.sleep(0.4)
        state["cancelled"] = True

    threading.Thread(target=_flip_later, daemon=True).start()

    started = time.monotonic()
    with pytest.raises(FFmpegCancelled):
        run(_sleep_cmd(60), cancel_check=lambda: state["cancelled"],
            poll_interval=0.05)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"late cancel took {elapsed:.1f}s — poll loop is broken"


# ---------------------------------------------------------------------------
# 2. The fast path: unchanged behaviour without cancel_check
# ---------------------------------------------------------------------------

def test_run_without_cancel_check_is_unchanged():
    proc = run(_sleep_cmd(0.1))
    assert proc.returncode == 0
    assert proc.stdout is not None and proc.stderr is not None


def test_run_without_cancel_check_still_classifies_missing_executable():
    with pytest.raises(FFmpegError) as excinfo:
        run(["definitely-not-a-real-binary-tsb253", "--version"])
    assert excinfo.value.code == ERR_EXECUTABLE_MISSING


def test_run_without_cancel_check_still_classifies_failure():
    import sys

    with pytest.raises(FFmpegError) as excinfo:
        run([sys.executable, "-c", "raise SystemExit(3)"])
    assert excinfo.value.code == ERR_COMMAND_FAILED


def test_run_without_cancel_check_still_classifies_timeout():
    with pytest.raises(FFmpegTimeout) as excinfo:
        run(_sleep_cmd(30), timeout=1)
    assert excinfo.value.code == ERR_TIMEOUT


def test_timeout_also_applies_when_cancel_check_is_present():
    """The polling loop must not swallow the timeout classification."""
    with pytest.raises(FFmpegTimeout) as excinfo:
        run(_sleep_cmd(30), timeout=1, cancel_check=lambda: False,
            poll_interval=0.05)
    assert excinfo.value.code == ERR_TIMEOUT


def test_cancel_wins_over_timeout_when_both_are_due():
    """A cancelled job must be reported as cancelled, never as a timeout."""
    with pytest.raises(FFmpegCancelled):
        run(_sleep_cmd(60), timeout=0.2, cancel_check=lambda: True,
            poll_interval=0.05)


# ---------------------------------------------------------------------------
# 3. The job layer: cancel_check_for + terminal-state handling
# ---------------------------------------------------------------------------

def test_cancel_check_for_is_none_without_a_job():
    """No job means the one-shot detectors keep their original code path."""
    assert cancel_check_for(None) is None
    assert cancel_check_for("") is None


def test_cancel_check_for_tracks_the_job_row(client, project):
    """The callback reflects the live DB status, not a stale snapshot."""
    res = client.post(f"/api/projects/{project['id']}/jobs/package", json={})
    job_id = res.json()["id"]

    check = cancel_check_for(job_id)
    assert check is not None
    assert check() is False

    client.post(f"/api/jobs/{job_id}/cancel")
    assert check() is True
    assert is_cancelled(job_id) is True


def test_job_cancelled_media_process_is_not_marked_failed(client, project, swap_task):
    """**The regression**: a killed media child is a cancel, not a failure.

    Before the fix, ``FFmpegCancelled`` (a plain ``FFmpegError``) fell into the
    runner's generic ``except Exception`` and the terminal state became
    ``failed`` with a traceback — a deliberate cancel reported as a red job.
    """

    def _task(job_id: str) -> None:
        run(_sleep_cmd(60), cancel_check=cancel_check_for(job_id),
            poll_interval=0.05)

    swap_task("roughcut", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]
    assert _wait_running(job_id), "task never started"

    started = time.monotonic()
    client.post(f"/api/jobs/{job_id}/cancel")
    status = _wait_terminal(job_id)
    elapsed = time.monotonic() - started

    assert status == STATUS_CANCELLED, f"expected cancelled, got {status}"
    assert elapsed < 25, f"job took {elapsed:.1f}s to stop — encode was not killed"

    with session_scope() as s:
        job = s.get(Job, job_id)
        # The cancelled state must survive to the end of _run(), not be
        # overwritten by the generic failure branch.
        assert job.status == STATUS_CANCELLED
        assert job.error in (None, ""), f"cancelled job carries error: {job.error!r}"


def test_successful_job_with_a_cancel_check_still_succeeds(client, project, swap_task):
    """Threading cancel_check through must not change the success path."""

    def _task(job_id: str) -> None:
        run(_sleep_cmd(0.1), cancel_check=cancel_check_for(job_id),
            poll_interval=0.05)

    swap_task("roughcut", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_SUCCEEDED


def test_genuine_media_failure_is_still_reported_as_failed(client, project, swap_task):
    """Only CANCELLED is special-cased; real media errors stay failures."""

    def _task(job_id: str) -> None:
        run(["definitely-not-a-real-binary-tsb253"], cancel_check=lambda: False)

    swap_task("roughcut", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_FAILED

    with session_scope() as s:
        assert s.get(Job, job_id).error


def test_job_cancelled_from_inside_a_task_keeps_cancelled_state(client, project, swap_task):
    """The pre-existing JobCancelled path is unchanged (no regression)."""

    def _task(job_id: str) -> None:  # noqa: ARG001
        raise JobCancelled("cancelled during run")

    swap_task("roughcut", _task)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    job_id = res.json()["id"]
    assert _wait_terminal(job_id) == STATUS_CANCELLED


# ---------------------------------------------------------------------------
# 4. Concurrency: the kill is scoped to one process tree
# ---------------------------------------------------------------------------

def test_cancelling_one_run_does_not_kill_a_concurrent_run():
    """A project-scoped kill would reach other jobs; this one must stay local.

    ``_terminate_tree`` is deliberately scoped to the child it started. If it
    ever regressed to a name-based or global kill, this test would show it by
    the bystander child dying.
    """
    import sys

    marker = f"tsb-bystander-{uuid.uuid4().hex}.txt"
    import tempfile

    pid_file = os.path.join(tempfile.gettempdir(), marker)
    bystander_cmd = [
        sys.executable, "-c",
        f"import os,time;open(r'{pid_file}','w').write(str(os.getpid()));"
        f"time.sleep(60)",
    ]

    results: dict[str, object] = {}

    def _bystander() -> None:
        try:
            results["bystander"] = run(bystander_cmd, cancel_check=lambda: False,
                                        poll_interval=0.05)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            results["bystander_exc"] = exc

    thread = threading.Thread(target=_bystander, daemon=True)
    thread.start()

    # Wait for the bystander to be established.
    deadline = time.time() + 15
    while time.time() < deadline and not os.path.isfile(pid_file):
        time.sleep(0.02)
    assert os.path.isfile(pid_file), "bystander child never started"
    bystander_pid = int(open(pid_file).read().strip())

    try:
        # Now cancel a *different* run.
        with pytest.raises(FFmpegCancelled):
            run(_sleep_cmd(60), cancel_check=lambda: True, poll_interval=0.05)

        # The bystander must be untouched and still alive.
        time.sleep(0.5)
        assert _pid_alive(bystander_pid), "concurrent run was killed as collateral"
        assert "bystander_exc" not in results, results.get("bystander_exc")
    finally:
        # Clean up the bystander deliberately.
        try:
            import signal

            os.kill(bystander_pid, signal.SIGTERM)
        except (OSError, ProcessLookupError, AttributeError):
            pass
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(bystander_pid), "/F"],
                           capture_output=True)
        thread.join(timeout=10)
        if os.path.exists(pid_file):
            os.unlink(pid_file)


# ---------------------------------------------------------------------------
# 5. Wiring: the production call sites must actually pass the callback
# ---------------------------------------------------------------------------

def test_roughcut_passes_a_cancel_check_to_its_encodes():
    """Wiring guard: the encoder call must carry ``cancel_check``.

    The TSB-253 defect was not only a missing parameter — it was a missing
    *call site*. Asserting the helper accepts the kwarg proves nothing; this
    asserts the production path supplies it.
    """
    import inspect

    from app.services import roughcut as mod

    params = inspect.signature(mod._extract_normalized).parameters
    assert "cancel_check" in params, "_extract_normalized lost its cancel_check"

    src = inspect.getsource(mod.roughcut_task)
    assert "cancel_check_for(job_id)" in src, (
        "roughcut_task no longer builds a cancel callback — cancelling a rough "
        "cut would again wait for the encode to finish"
    )
    assert "cancel_check=cancel_check" in src, (
        "roughcut_task does not forward its cancel callback into the encode"
    )
    assert "except FFmpegCancelled" in src, (
        "roughcut_task would count a cancelled encode as one more failed clip "
        "and keep encoding"
    )


def test_proxy_task_passes_a_cancel_check_to_its_encodes():
    """Wiring guard for the proxy path (the longest encodes in the app)."""
    import inspect

    from app.services import story_planner as mod

    src = inspect.getsource(mod.proxies_task)
    assert "cancel_check_for(job_id)" in src, (
        "proxies_task no longer builds a cancel callback — cancelling proxies "
        "would wait out each encode"
    )
    assert "cancel_check=cancel_check_for(job_id)" in src, (
        "proxies_task does not forward its cancel callback into extract_preview"
    )
    assert "except FFmpegCancelled" in src, (
        "proxies_task would record a cancelled encode as one more failed request"
    )


def test_analyze_task_passes_a_cancel_check_to_sampling():
    """Wiring guard for the detect path (sampling + representative frames)."""
    import inspect

    from app.services import analyzer as mod

    src = inspect.getsource(mod.detect_task)
    assert "cancel_check_for(job_id)" in inspect.getsource(mod._analyze_asset), (
        "detect sampling lost its cancel callback"
    )
    assert "except FFmpegCancelled" in src, (
        "analyzer would record a cancelled detect as a per-asset failure and "
        "continue with the next asset"
    )


def test_detector_forwards_cancel_check_into_its_sampling_run(monkeypatch):
    """``detect_segments`` must hand its callback to the sampling ``run``."""
    from app.services import detector as mod

    seen: dict = {}

    def _fake_run(cmd, **kwargs):
        seen.update(kwargs)
        raise mod.FFmpegError("stop", code="COMMAND_FAILED")

    monkeypatch.setattr(mod, "run", _fake_run)

    marker = lambda: False  # noqa: E731 - identity matters, not the result
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(mod.FFmpegError):
            mod._extract_sample_frames("src.mp4", 1.0, 160, tmp,
                                       cancel_check=marker)

    assert seen.get("cancel_check") is marker, (
        "detector._extract_sample_frames dropped the cancel callback"
    )


def test_detect_segments_keeps_its_original_signature_contract():
    """Without a job the detector must stay on its original one-shot path."""
    import inspect

    from app.services import detector as mod

    params = inspect.signature(mod.detect_segments).parameters
    assert "cancel_check" in params
    assert params["cancel_check"].default is None, (
        "cancel_check must default to None so callers without a job are unchanged"
    )

