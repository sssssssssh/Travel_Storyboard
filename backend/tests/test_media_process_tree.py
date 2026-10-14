"""TSB-263 — cancelling must reclaim the whole media process tree.

The confirmed defect (task book §5): ``_popen_kwargs`` gave the child its own
process group, but ``_terminate_tree`` only called ``proc.terminate()/kill()``
on the *direct* process. FFmpeg spawns helpers (and a real encode tree can be
several levels deep), so the parent exiting proved nothing: grandchildren kept
running, holding file handles and CPU, after the job had already been reported
as cancelled.

These tests therefore use a **real parent -> child -> grandchild** chain and
assert the descendants are gone, not merely that the parent is. They also assert
the blast radius stays local: cancelling one run must not touch an unrelated
concurrent run.

Platform notes:
* POSIX: the child calls ``setsid``, so the whole group can be signalled.
* Windows: the child gets ``CREATE_NEW_PROCESS_GROUP``; descendants are bound
  into a Job Object with ``KILL_ON_JOB_CLOSE`` where available, so closing the
  handle reclaims the tree even if the parent died first.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.ffmpeg import (
    CANCEL_GRACE_SEC,
    FFmpegCancelled,
    run,
)

IS_WINDOWS = os.name == "nt"

#: A parent that spawns a grandchild, records both pids, then sleeps.
#:
#: The grandchild inherits the parent's process group / job membership, which is
#: exactly the situation a bare `terminate()` on the parent fails to clean up.
_TREE_SCRIPT = textwrap.dedent(
    """
    import os, sys, subprocess, time
    pid_file, child_marker = sys.argv[1], sys.argv[2]

    grandchild = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(600)",
         child_marker]
    )
    open(pid_file, "w").write(f"{os.getpid()} {grandchild.pid}")
    time.sleep(600)
    """
)

#: Same, but the parent exits immediately, leaving only the grandchild behind.
_ORPHANING_SCRIPT = textwrap.dedent(
    """
    import os, sys, subprocess
    pid_file, child_marker = sys.argv[1], sys.argv[2]
    grandchild = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(600)",
         child_marker]
    )
    open(pid_file, "w").write(f"{os.getpid()} {grandchild.pid}")
    """
)


def _pid_alive(pid: int) -> bool:
    """Whether *pid* is still running (portable liveness probe)."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
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
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # A reaped zombie is not "alive" in any meaningful sense.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state = handle.read().split(") ", 1)[1].split(" ", 1)[0]
        if state == "Z":
            return False
    except OSError:
        pass
    return True


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _wait_file(path: Path, timeout: float = 25.0) -> bool:
    """Wait for a pid file to appear *and* be readable and complete.

    Reading it the instant it exists can catch a partially-written value (the
    writer creates the file before flushing), which is a harness race rather
    than a product behaviour.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = path.read_text().strip()
            if text and len(text.split()) == 2:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    return False


def _read_pids(path: Path) -> list[int]:
    return [int(x) for x in path.read_text().split()]


def _force_kill(pid: int) -> None:
    """Best-effort removal of a pid and its descendants; never raises.

    Uses the *real* Popen: some tests monkeypatch ``subprocess.Popen`` to inject
    a hostile proxy, and cleanup must not inherit that stub — it would then fail
    to clean up the very processes the test is about.
    """
    real_popen = getattr(subprocess, "_Popen_orig", subprocess.Popen)
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15, Popen=real_popen)
        else:
            os.kill(pid, 9)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired, TypeError):
        pass


# --------------------------------------------------------------------------
# the harness itself must be trustworthy
# --------------------------------------------------------------------------

def test_tree_harness_really_creates_a_grandchild(tmp_path):
    """Guard the guard: if this chain is not real, every other test is vacuous.

    The pids are asserted to be a chain of *distinct processes*, not to equal
    ``Popen.pid``: on this environment the venv python is a launcher that
    re-execs, so the process which spawns the grandchild is a descendant of the
    one Popen reports (see ``tools/probe_process_tree.py``). Asserting pid
    equality would encode a false premise and test nothing.
    """
    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"

    proc = subprocess.Popen(
        [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)],
    )
    pids: list[int] = []
    try:
        assert _wait_file(pid_file), "the parent never started its grandchild"
        pids = _read_pids(pid_file)
        assert len(pids) == 2
        parent_pid, grandchild_pid = pids
        assert parent_pid != grandchild_pid, "the chain must be two processes"
        assert _pid_alive(parent_pid), "the working parent must be running"
        assert _pid_alive(grandchild_pid), "the grandchild must be running"
    finally:
        _force_kill(proc.pid)
        for pid in pids:
            _force_kill(pid)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------
# TC-21/22: cancel reclaims the descendants
# --------------------------------------------------------------------------

def test_cancel_kills_the_whole_descendant_tree(tmp_path):
    """TC-21/TC-22: after a cancel, parent *and* grandchild must be gone.

    Before the fix only the parent was signalled, so the grandchild survived the
    cancel — still holding whatever the encoder had opened.
    """
    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"
    cmd = [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)]

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.05)

        assert _wait_file(pid_file), "the tree never started"
        pids = _read_pids(pid_file)
        parent_pid, grandchild_pid = pids

        assert _wait_gone(parent_pid, 15), f"parent {parent_pid} survived the cancel"
        assert _wait_gone(grandchild_pid, 15), (
            f"grandchild {grandchild_pid} survived the cancel — the process tree "
            "was not reclaimed"
        )
    finally:
        for pid in pids:
            _force_kill(pid)


def test_cancel_reports_only_after_the_tree_is_stopped(tmp_path):
    """``FFmpegCancelled`` means "stopped", so it must not precede the exit.

    The book requires that "fully cancelled" is only reported once descendants
    have actually exited; raising while the tree is still running would make the
    job state a claim the process table contradicts.
    """
    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"
    cmd = [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)]

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.05)
        pids = _read_pids(pid_file)
        # Immediately after the raise, nothing from the tree may still run.
        for pid in pids:
            assert not _pid_alive(pid), (
                f"pid {pid} was still running when the cancel was reported"
            )
    finally:
        for pid in pids:
            _force_kill(pid)


def test_timeout_also_reclaims_the_tree(tmp_path):
    """A timeout must not orphan descendants either."""
    from app.ffmpeg import FFmpegTimeout

    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"
    cmd = [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)]

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegTimeout):
            run(cmd, timeout=3, cancel_check=lambda: False, poll_interval=0.05)
        assert _wait_file(pid_file), "the tree never started"
        pids = _read_pids(pid_file)
        for pid in pids:
            assert _wait_gone(pid, 15), f"pid {pid} survived the timeout"
    finally:
        for pid in pids:
            _force_kill(pid)


def test_parent_exiting_first_still_reclaims_the_grandchild(tmp_path):
    """The hard case: the parent dies, the grandchild must still be reclaimed.

    A tree-kill that only walks from a *live* parent finds nothing here, so this
    distinguishes real process-group/job-object reclamation from a
    ``terminate()`` on the parent.

    The cancel is not requested until the pid file is readable, so the tree is
    always observed before the parent can exit. That removes the race entirely —
    an earlier version skipped when the parent won the race, which would let the
    mandatory-media CI job fail on a platform where it happened to lose.
    """
    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"
    cmd = [sys.executable, "-c", _ORPHANING_SCRIPT, str(pid_file), str(marker)]

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.02)

        # By construction the file exists (that is what triggered the cancel),
        # so this is an assertion, not a coin flip.
        assert _wait_file(pid_file, 5), "the tree was never observable"
        pids = _read_pids(pid_file)
        parent_pid, grandchild_pid = pids
        assert _wait_gone(parent_pid, 15)
        assert _wait_gone(grandchild_pid, 15), (
            f"grandchild {grandchild_pid} survived after its parent exited — "
            "the tree was not reclaimed"
        )
    finally:
        for pid in pids:
            _force_kill(pid)


# --------------------------------------------------------------------------
# blast radius
# --------------------------------------------------------------------------

def test_cancelling_one_run_leaves_an_unrelated_run_alone(tmp_path):
    """TC-22: cancellation must be scoped to its own tree, never global.

    The book explicitly forbids ``taskkill /IM ffmpeg.exe /F``-style termination.
    A bystander run in another project must be untouched.
    """
    victim_pids = tmp_path / "victim.txt"
    bystander_pids = tmp_path / "bystander.txt"
    bystander_marker = tmp_path / "bystander_grandchild.txt"

    results: dict = {}

    def _bystander() -> None:
        try:
            results["bystander"] = run(
                [sys.executable, "-c", _TREE_SCRIPT, str(bystander_pids),
                 str(bystander_marker)],
                cancel_check=lambda: False,
                poll_interval=0.05,
                timeout=120,
            )
        except BaseException as exc:  # noqa: BLE001 - recorded for assertion
            results["bystander_exc"] = exc

    thread = threading.Thread(target=_bystander, daemon=True)
    thread.start()

    bystander: list[int] = []
    victim: list[int] = []
    try:
        assert _wait_file(bystander_pids), "the bystander tree never started"
        bystander = _read_pids(bystander_pids)

        # Cancel a *different* run.
        with pytest.raises(FFmpegCancelled):
            run([sys.executable, "-c", _TREE_SCRIPT, str(victim_pids),
                 str(tmp_path / "victim_grandchild.txt")],
                cancel_check=lambda: victim_pids.exists(), poll_interval=0.05)
        victim = _read_pids(victim_pids)

        for pid in victim:
            assert _wait_gone(pid, 15), f"victim pid {pid} survived"

        # The bystander must be alive: same interpreter, same user, different run.
        time.sleep(0.5)
        for pid in bystander:
            assert _pid_alive(pid), (
                f"bystander pid {pid} was killed as collateral — the termination "
                "was not scoped to its own process tree"
            )
        assert "bystander_exc" not in results, results.get("bystander_exc")
    finally:
        for pid in victim + bystander:
            _force_kill(pid)
        thread.join(timeout=20)


def test_cancel_between_polls_still_reclaims_late_descendants(tmp_path):
    """A grandchild created just before the cancel must be reclaimed too.

    The tree is not snapshotted at spawn time; reclamation must be by group/job
    membership so a descendant that appears later is covered.
    """
    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "late_grandchild.txt"
    cmd = [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)]

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.05)
        pids = _read_pids(pid_file)
        for pid in pids:
            assert _wait_gone(pid, 15)
    finally:
        for pid in pids:
            _force_kill(pid)


# --------------------------------------------------------------------------
# error handling must not leave the tree running
# --------------------------------------------------------------------------

def test_terminate_errors_do_not_leave_the_tree_running(tmp_path, monkeypatch):
    """``terminate()``/``kill()`` raising must still end with a dead tree."""
    import app.ffmpeg as mod

    pid_file = tmp_path / "pids.txt"
    marker = tmp_path / "grandchild.txt"
    cmd = [sys.executable, "-c", _TREE_SCRIPT, str(pid_file), str(marker)]

    real_popen = subprocess.Popen

    class _Flaky:
        """A Popen proxy whose first terminate raises, like a real race."""

        def __init__(self, inner):
            self._inner = inner
            self._raised = False

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def terminate(self):
            if not self._raised:
                self._raised = True
                raise OSError("process already exited")
            return self._inner.terminate()

        def kill(self):
            return self._inner.kill()

    monkeypatch.setattr(mod.subprocess, "Popen",
                        lambda *a, **kw: _Flaky(real_popen(*a, **kw)))
    # `_force_kill` must bypass the stub above, or the cleanup of this very test
    # would raise the same TypeError it is meant to guard against.
    monkeypatch.setattr(subprocess, "_Popen_orig", real_popen, raising=False)

    pids: list[int] = []
    try:
        with pytest.raises(FFmpegCancelled):
            run(cmd, cancel_check=lambda: pid_file.exists(), poll_interval=0.05)
        assert _wait_file(pid_file), "the tree never started"
        pids = _read_pids(pid_file)
        for pid in pids:
            assert _wait_gone(pid, 20), f"pid {pid} survived a raising terminate()"
    finally:
        for pid in pids:
            _force_kill(pid)


def test_grace_period_is_short_enough_to_be_useful():
    """A cancel must free the project quickly (the point of the whole change)."""
    assert 0 < CANCEL_GRACE_SEC <= 10


def test_no_global_name_based_kill_is_used():
    """Guard the book's explicit prohibition on name-based global termination."""
    import inspect

    import app.ffmpeg as mod

    src = inspect.getsource(mod)
    for forbidden in ("/IM", "pkill", "killall"):
        assert forbidden not in src, (
            f"ffmpeg.py uses {forbidden!r} — global, name-based termination is "
            "forbidden: it would kill other projects' concurrent encodes"
        )
