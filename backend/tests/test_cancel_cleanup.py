"""TSB-262 — a cancelled or failed task must collect its own staging.

The confirmed defect (task book §4): ``roughcut_task`` only ran
``shutil.rmtree(stage)`` **after** the concat and publish succeeded. Every early
exit — cancel, timeout, "no clips were produced", a raising encoder — returned
before that line, leaving the whole staging directory (all encoded clips) on
disk. ``_analyze_asset`` had the same shape, and ``proxies_task`` used a bare
``ignore_errors=True`` that cannot distinguish "cleaned" from "failed to clean".

Two separate problems are guarded here:

1. **The leak**: staging created by *this* run survives cancellation.
2. **The blast radius**: a cleanup helper that deletes too much. It must own only
   the current task's staging, never the published ``frames/``, ``proxies/`` or
   ``roughcut/`` output, never the workspace root, and never anything reachable
   through a symlink/junction that points outside the project.

The book also forbids treating ``ignore_errors=True`` as evidence of cleanup, so
failures must be reported through a typed result rather than swallowed.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.ffmpeg import FFmpegCancelled, long_path
from app.models import Asset, EditPlanShot, Job, Segment
from app.paths import project_cache_dir
from app.services import runner as runner_mod
from app.services.runner import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    TASK_REGISTRY,
    JobCancelled,
)
from app.services.staging_cleanup import (
    CleanupResult,
    cleanup_owned_staging,
    count_tree,
)

IS_WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(
    not IS_WINDOWS, reason="long-path behaviour is Windows-specific"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_project(client, name: str = "cleanup") -> dict:
    res = client.post("/api/projects", json={"name": name, "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _add_asset(project_id: str, path: str = "C:/media/a.mp4") -> str:
    with session_scope() as s:
        asset = Asset(id=str(uuid.uuid4()), project_id=project_id,
                      source_path=path, display_name="a.mp4",
                      duration_sec=60.0, scan_status="succeeded")
        s.add(asset)
        s.flush()
        return asset.id


def _add_segment(project_id: str, asset_id: str) -> str:
    with session_scope() as s:
        seg = Segment(id=str(uuid.uuid4()), project_id=project_id, asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=5.0,
                      start_sec=0.0, end_sec=5.0, status="candidate")
        s.add(seg)
        s.flush()
        return seg.id


def _add_shot(project_id: str, asset_id: str, segment_id: str, order: int = 0) -> str:
    with session_scope() as s:
        shot = EditPlanShot(id=str(uuid.uuid4()), project_id=project_id,
                            order=order, asset_id=asset_id, segment_id=segment_id,
                            source_in_sec=0.0, source_out_sec=2.0,
                            target_duration_sec=2.0)
        s.add(shot)
        s.flush()
        return shot.id


def _make_deep_dir(root: Path, target_len: int) -> Path:
    current = root
    chunk = "d" * 40
    while len(str(current)) < target_len:
        current = current / chunk
        os.makedirs(long_path(current), exist_ok=True)
    return current


def _write(path: Path, data: bytes = b"x") -> None:
    os.makedirs(long_path(path.parent), exist_ok=True)
    try:
        path.write_bytes(data)
    except (FileNotFoundError, OSError):
        with open(long_path(path), "wb") as handle:
            handle.write(data)


def _exists(p: Path) -> bool:
    if p.exists():
        return True
    return Path(long_path(p)).exists()


# ==========================================================================
# 1. cleanup_owned_staging: the safety boundary
# ==========================================================================

def test_cleanup_removes_a_task_owned_stage(client):
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-abc123"
    _write(stage / "clip_0000.mp4", b"y" * 64)
    _write(stage / "concat.txt", b"file 'x'")

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert isinstance(result, CleanupResult)
    assert result.ok is True, result
    assert result.removed_files >= 2
    assert not _exists(stage), "the task's own staging must be gone"


def test_cleanup_is_idempotent_on_a_missing_stage(client):
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-gone"

    first = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")
    second = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert first.ok is True
    assert second.ok is True, "a second cleanup must not report a spurious failure"


def test_cleanup_refuses_the_workspace_root(client):
    """Deleting the project root would take every artifact with it."""
    project = _make_project(client)
    root = project_cache_dir(project["id"])
    _write(root / "frames" / "a" / "f.jpg")
    _write(root / "roughcut" / "roughcut.mp4")

    result = cleanup_owned_staging(project["id"], root, owner_job_id="job-1")

    assert result.ok is False
    assert result.error_code == "STAGING_NOT_OWNED"
    assert _exists(root / "frames" / "a" / "f.jpg"), "published frames must survive"


def test_cleanup_refuses_published_directories(client):
    """``frames/``, ``proxies/`` and ``roughcut/`` are not staging."""
    project = _make_project(client)
    root = project_cache_dir(project["id"])
    targets = {
        "frames": root / "frames",
        "proxies": root / "proxies",
        "roughcut": root / "roughcut",
        "packages": root / "packages",
    }
    for name, path in targets.items():
        _write(path / "keep.bin", b"published")

    for name, path in targets.items():
        result = cleanup_owned_staging(project["id"], path, owner_job_id="job-1")
        assert result.ok is False, f"{name} must not be deletable as staging"
        assert result.error_code == "STAGING_NOT_OWNED"
        assert _exists(path / "keep.bin"), f"{name} contents must survive"


def test_cleanup_refuses_a_path_outside_the_project(client):
    """A staging path must not escape the owning project's cache dir."""
    project = _make_project(client)
    outside = Path(os.environ["TRAVELSB_DATA_DIR"]) / "not-this-project" / "staging-x"
    _write(outside / "secret.bin", b"do not delete")

    result = cleanup_owned_staging(project["id"], outside, owner_job_id="job-1")

    assert result.ok is False
    assert result.error_code == "STAGING_NOT_OWNED"
    assert _exists(outside / "secret.bin"), "an outside path must never be deleted"


def test_cleanup_refuses_the_data_root_and_user_source_dir(client, tmp_path):
    """The two trees the book names explicitly: data root and footage."""
    project = _make_project(client)
    data_root = Path(os.environ["TRAVELSB_DATA_DIR"])

    for target in (data_root, tmp_path):
        result = cleanup_owned_staging(project["id"], target, owner_job_id="job-1")
        assert result.ok is False, f"{target} must be refused"
        assert result.error_code == "STAGING_NOT_OWNED"


def test_cleanup_allows_analyzer_staging_pattern(client):
    """The analyzer's ``staging/<asset>/<run-token>`` shape is legitimate."""
    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    stage = project_cache_dir(project["id"]) / "staging" / asset_id / "run1234"
    _write(stage / "f_000001.jpg", b"j" * 32)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert result.ok is True, result
    assert not _exists(stage)


def test_cleanup_allows_proxies_staging_pattern(client):
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "proxies" / "staging" / "tok123"
    _write(stage / "temp.mp4", b"v" * 32)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert result.ok is True, result
    assert not _exists(stage)


def test_cleanup_reports_failure_instead_of_swallowing_it(client, monkeypatch):
    """A staged file that cannot be deleted must be reported, not hidden.

    ``ignore_errors=True`` cannot distinguish "cleaned" from "failed to clean",
    so the contract is: the residual path is named, the result is not ``ok``,
    and the run is marked retryable. Whether the OS ultimately lets go of the
    file is not something this module gets to assume either way, so the test
    drives a directory removal that genuinely cannot succeed.
    """
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-busy"
    _write(stage / "clip.mp4", b"busy")

    import app.services.staging_cleanup as mod

    # Make the *directory* removal fail as well, so the residual is real: this
    # models "the tree is still here after cleanup" rather than relying on a
    # transient Windows file-lock quirk.
    def _locked_unlink(path):
        raise PermissionError("file is locked by another process")

    def _refuse_rmtree(path, ignore_errors=False):
        raise PermissionError("directory is in use")

    monkeypatch.setattr(mod, "safe_unlink", _locked_unlink)
    monkeypatch.setattr(mod.shutil, "rmtree", _refuse_rmtree)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert result.ok is False, "a staging tree still on disk must not report ok"
    assert result.error_code == "CLEANUP_FAILED"
    assert result.retryable is True, "a locked stage should be retryable"
    assert result.residual_paths, "the residual path must be named for triage"


def test_cleanup_accounting_matches_what_actually_disappeared(client):
    """``removed_files``/``removed_bytes`` must describe reality, not intent.

    A regressed version reported ``ok=True, removed_files=0`` for a stage that
    was fully deleted, because it counted successes per call instead of
    measuring what was left.
    """
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-account"
    _write(stage / "a.bin", b"1" * 40)
    _write(stage / "sub" / "b.bin", b"2" * 60)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert result.ok is True, result
    assert not _exists(stage)
    assert result.removed_files == 2, result.removed_files
    assert result.removed_bytes == 100, result.removed_bytes


def test_cleanup_result_carries_traceable_details(client):
    """The book requires residual paths, bytes and a retry flag."""
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-detail"
    _write(stage / "clip_0000.mp4", b"z" * 128)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")
    assert result.ok is True
    assert result.removed_bytes >= 128

    payload = result.as_dict()
    for key in ("ok", "error_code", "removed_files", "removed_bytes",
                "residual_paths", "retryable"):
        assert key in payload, f"{key} missing from the cleanup report"


def test_count_tree_measures_before_deleting(client):
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-measure"
    _write(stage / "a.bin", b"1" * 10)
    _write(stage / "sub" / "b.bin", b"2" * 20)

    files, total = count_tree(stage)
    assert files == 2
    assert total == 30


# ==========================================================================
# 2. TC-11..13: roughcut staging must not leak
# ==========================================================================

@pytest.fixture()
def swap_task():
    original = dict(TASK_REGISTRY)

    def _install(job_type: str, fn):
        TASK_REGISTRY[job_type] = fn

    yield _install
    TASK_REGISTRY.clear()
    TASK_REGISTRY.update(original)


def _roughcut_stages(project_id: str) -> list[Path]:
    work = project_cache_dir(project_id) / "roughcut"
    if not work.exists():
        return []
    return sorted(p for p in work.glob("staging-*"))


def _wait_job(job_id: str, timeout: float = 20.0) -> str | None:
    import time

    from app.services.runner import TERMINAL_STATUSES

    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.get(Job, job_id)
            status = job.status if job else None
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(0.02)
    return status


def test_cancelled_roughcut_removes_its_staging(client, monkeypatch):
    """TC-11/TC-12: a cancel during clip encoding must not leave staging behind.

    Reproduces the leak directly: the encoder raises ``FFmpegCancelled`` on the
    first clip, which used to bypass ``shutil.rmtree(stage)`` entirely.
    """
    from app.services import roughcut as mod

    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    segment_id = _add_segment(project["id"], asset_id)
    _add_shot(project["id"], asset_id, segment_id)

    def _cancelling_encode(source, start, dur, out, has_audio, cancel_check=None):
        # Write the partial clip, then abort exactly like a killed ffmpeg.
        _write(Path(out), b"partial")
        raise FFmpegCancelled("cancelled mid-encode")

    monkeypatch.setattr(mod, "_extract_normalized", _cancelling_encode)

    with pytest.raises(JobCancelled):
        mod.roughcut_task(_fake_job_id(project["id"]))

    assert _roughcut_stages(project["id"]) == [], (
        "a cancelled rough cut must collect its staging directory"
    )


def test_failed_roughcut_removes_its_staging(client, monkeypatch):
    """A raising encoder must clean up too, not just a cancel."""
    from app.services import roughcut as mod

    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    segment_id = _add_segment(project["id"], asset_id)
    _add_shot(project["id"], asset_id, segment_id)

    def _exploding_encode(source, start, dur, out, has_audio, cancel_check=None):
        _write(Path(out), b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_extract_normalized", _exploding_encode)

    with pytest.raises(RuntimeError):
        mod.roughcut_task(_fake_job_id(project["id"]))

    assert _roughcut_stages(project["id"]) == [], (
        "a failed rough cut must not leave its staging directory behind"
    )


def test_roughcut_staging_is_cleaned_on_the_success_path_too(client, monkeypatch):
    """No regression: the happy path still publishes and still cleans up."""
    from app.services import roughcut as mod

    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    segment_id = _add_segment(project["id"], asset_id)
    _add_shot(project["id"], asset_id, segment_id)

    calls = {"n": 0}

    def _ok_encode(source, start, dur, out, has_audio, cancel_check=None):
        calls["n"] += 1
        _write(Path(out), b"clip-bytes")

    def _fake_run(cmd, **kwargs):
        # The concat step: produce the declared output.
        out = kwargs.get("expect_output")
        if out is not None:
            _write(Path(out), b"joined")
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(mod, "_extract_normalized", _ok_encode)
    monkeypatch.setattr(mod, "run", _fake_run)

    mod.roughcut_task(_fake_job_id(project["id"]))

    assert calls["n"] == 1
    assert _roughcut_stages(project["id"]) == [], "staging must be collected"
    publishes = list((project_cache_dir(project["id"]) / "roughcut").glob("roughcut*.mp4"))
    assert publishes, "the finished rough cut must still be published"


def test_stale_roughcut_output_survives_a_cancelled_rerun(client, monkeypatch):
    """TC-12: cancelling a re-run must not touch the previously published file."""
    from app.services import roughcut as mod

    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    segment_id = _add_segment(project["id"], asset_id)
    _add_shot(project["id"], asset_id, segment_id)

    work = project_cache_dir(project["id"]) / "roughcut"
    published = work / "roughcut.mp4"
    _write(published, b"PREVIOUS-RELEASE")

    def _cancelling_encode(source, start, dur, out, has_audio, cancel_check=None):
        raise FFmpegCancelled("cancelled")

    monkeypatch.setattr(mod, "_extract_normalized", _cancelling_encode)

    with pytest.raises(JobCancelled):
        mod.roughcut_task(_fake_job_id(project["id"]))

    assert _exists(published), "the historical rough cut must survive"
    assert published.read_bytes() == b"PREVIOUS-RELEASE"
    assert _roughcut_stages(project["id"]) == []


# ==========================================================================
# 3. TC-14/15: analyzer staging
# ==========================================================================

def test_cancelled_detection_does_not_leak_frame_staging(client, tmp_path, monkeypatch):
    """TC-14/15: a cancel while extracting representative frames cleans up.

    The analyzer stages frames under ``staging/<asset>/<run-token>`` and used to
    remove them only on the later success/rollback paths, so a cancel during
    frame extraction left every sampled JPEG behind.
    """
    from app.services import analyzer as mod
    from app.services.detector import Boundary, DetectionResult

    project = _make_project(client)

    # A real file on disk: `_analyze_asset` verifies the source before analysis.
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 4096)

    with session_scope() as s:
        asset = Asset(id=str(uuid.uuid4()), project_id=project["id"],
                      source_path=str(source), display_name="clip.mp4",
                      duration_sec=10.0, scan_status="succeeded")
        s.add(asset)
        s.flush()
        asset_id = asset.id

    # One boundary, so the frame-extraction loop is actually entered.
    monkeypatch.setattr(
        mod, "detect_segments",
        lambda *a, **kw: DetectionResult(
            boundaries=[Boundary(start_sec=0.0, end_sec=4.0, reason="test")],
            sample_times=[0.0, 1.0, 2.0, 3.0],
            sample_diffs=[0.0, 1.0, 0.0, 1.0],
        ),
    )

    def _cancelling_extract(source_path, t, out, width=None, cancel_check=None):
        _write(Path(out), b"frame")
        raise FFmpegCancelled("cancelled while extracting frames")

    monkeypatch.setattr(mod, "extract_frame", _cancelling_extract)

    staging_root = project_cache_dir(project["id"]) / "staging" / asset_id

    with session_scope() as s:
        asset_row = s.get(Asset, asset_id)
        job_id = _fake_job_id(project["id"])
        # `_analyze_asset` lets FFmpegCancelled propagate; `detect_task` is what
        # converts it into JobCancelled. Either way the staging must be gone.
        with pytest.raises((JobCancelled, FFmpegCancelled)):
            mod._analyze_asset(project["id"], asset_row, job_id=job_id)

    leftovers = list(staging_root.glob("*")) if staging_root.exists() else []
    assert leftovers == [], f"cancelled detection left staging behind: {leftovers}"


# ==========================================================================
# 4. TC-18/19/20: deep paths, orphans, links
# ==========================================================================

@requires_windows
def test_cleanup_handles_deep_staging_paths(client, tmp_path):
    """TC-18: ≥270-character staging must really be deleted, not just 'ok'."""
    from app import paths as paths_mod

    deep_root = _make_deep_dir(tmp_path, 280)
    project = _make_project(client)
    # Point this project's cache root at the deep directory. `remember_project_root`
    # is the supported way in (it updates the in-process cache), rather than
    # poking the private map and hoping nothing re-resolved it first.
    paths_mod.remember_project_root(project["id"], str(deep_root))

    stage = paths_mod.project_cache_dir(project["id"]) / "roughcut" / "staging-deep"
    _write(stage / "clip_0000.mp4", b"d" * 64)
    _write(stage / "nested" / "deeper" / "x.bin", b"e" * 32)
    assert len(str(stage)) >= 270, len(str(stage))

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert result.ok is True, result
    assert not _exists(stage), "deep staging must actually be removed"
    assert not _exists(stage / "nested"), "nested directories too"


@requires_windows
def test_cleanup_counts_deep_staging_bytes(client, tmp_path):
    from app import paths as paths_mod

    deep_root = _make_deep_dir(tmp_path, 280)
    project = _make_project(client)
    paths_mod.remember_project_root(project["id"], str(deep_root))

    stage = paths_mod.project_cache_dir(project["id"]) / "proxies" / "staging" / "tok"
    _write(stage / "temp.mp4", b"m" * 256)

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")
    assert result.ok is True, result
    assert result.removed_bytes == 256


def test_cleanup_refuses_a_symlink_pointing_outside(client, tmp_path):
    """TC-20: a link escaping the project must never widen the delete.

    Guards the most severe defect found during TSB-262: an early implementation
    followed a junction and deleted data *outside* the project. The link is
    created here rather than assumed, and a host that forbids both a junction
    and a symlink is a platform fact (whitelisted in ``conftest``), not a
    silently dropped protection — on this project's CI both Windows and Linux
    runners can create one.
    """
    project = _make_project(client)
    stage = project_cache_dir(project["id"]) / "roughcut" / "staging-link"
    os.makedirs(long_path(stage), exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.bin").write_bytes(b"do not delete")

    link = stage / "escape"
    created = False
    if IS_WINDOWS:
        import subprocess

        # A directory junction needs no admin rights on Windows.
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
        )
        created = proc.returncode == 0
    if not created:
        try:
            link.symlink_to(outside, target_is_directory=True)
            created = True
        except (OSError, NotImplementedError):
            created = False
    if not created:
        pytest.skip("link creation is a platform fact: neither junction nor symlink permitted")

    result = cleanup_owned_staging(project["id"], stage, owner_job_id="job-1")

    assert (outside / "precious.bin").exists(), (
        "cleanup followed a link out of the project and deleted outside data"
    )
    assert result.ok is True or result.residual_paths, (
        "an escaping link must be reported rather than silently mishandled"
    )


# ==========================================================================
# 5. job-level: the cleanup happens before the mutex is released
# ==========================================================================

def test_staging_is_cleaned_before_the_job_reports_cancelled(client, monkeypatch):
    """The book's ordering requirement: clean up, *then* settle, *then* unlock."""
    from app.services import roughcut as mod

    project = _make_project(client)
    asset_id = _add_asset(project["id"])
    segment_id = _add_segment(project["id"], asset_id)
    _add_shot(project["id"], asset_id, segment_id)

    observed: dict = {}

    def _cancelling_encode(source, start, dur, out, has_audio, cancel_check=None):
        raise FFmpegCancelled("cancelled")

    monkeypatch.setattr(mod, "_extract_normalized", _cancelling_encode)

    res = client.post(f"/api/projects/{project['id']}/jobs/roughcut", json={})
    assert res.status_code in (200, 201, 202), res.text
    job_id = res.json()["id"]

    import time

    # Wait for the task to actually run and cancel it.
    deadline = time.time() + 10
    while time.time() < deadline:
        with session_scope() as s:
            if s.get(Job, job_id).status == "running":
                break
        time.sleep(0.02)
    client.post(f"/api/jobs/{job_id}/cancel")

    final = _wait_job(job_id)
    observed["status"] = final

    assert final == STATUS_CANCELLED, f"expected cancelled, got {final}"
    # By the time the job reads `cancelled`, staging must already be gone.
    assert _roughcut_stages(project["id"]) == [], (
        "the job reported cancelled before its staging was collected"
    )


def _fake_job_id(project_id: str) -> str:
    """A throwaway Job row so task bodies can call update_job/JobFailures."""
    with session_scope() as s:
        job = Job(project_id=project_id, job_type="roughcut", status="running",
                  config_snapshot={}, stats={}, failures={"count": 0, "items": []},
                  resume_token={})
        s.add(job)
        s.flush()
        return job.id
