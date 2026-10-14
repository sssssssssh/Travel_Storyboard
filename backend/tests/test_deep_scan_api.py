"""TSB-254 — scanning a deep source root through the public HTTP API.

The gap this closes: ``test_long_paths.py`` called ``index_one()`` /
``_analyze_asset()`` directly, which is *not* the same as "user creates a
project, points it at a deep source root, starts a scan Job, and lists the
assets". The directory-enumeration layer in between is where the bug lived.

Confirmed defect: ``Path.rglob()`` raises ``FileNotFoundError`` for a directory
at ~271+ characters and **swallows it**, so it stops descending and every file
underneath is silently lost while the scan still reports success. That is the
"scan job quietly misses files" release veto.

These tests only ever drive real endpoints, and read job/asset state back
through the API or the database the API writes.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.ffmpeg import ffmpeg_available, long_path
from app.models import Asset, Job

IS_WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(
    not IS_WINDOWS, reason="MAX_PATH only constrains Windows"
)

ACTIVE = ("pending", "running")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_deep_dir(root: Path, target_len: int, chunk: str = "d" * 36) -> Path:
    current = root
    while len(str(current)) < target_len:
        current = current / chunk
        os.makedirs(long_path(current), exist_ok=True)
    return current


def _touch(path: Path, data: bytes = b"x") -> None:
    os.makedirs(long_path(path.parent), exist_ok=True)
    with open(long_path(path), "wb") as handle:
        handle.write(data)


def _ffmpeg() -> str:
    exe = ffmpeg_available()
    if not exe:
        pytest.skip("ffmpeg not available")
    return exe


def _make_video(path: Path, seconds: float = 1.0, color: str = "red") -> None:
    """A tiny real MP4, so the scan can actually probe it."""
    exe = _ffmpeg()
    os.makedirs(long_path(path.parent), exist_ok=True)
    subprocess.run(
        [exe, "-y", "-f", "lavfi", "-i", f"color=c={color}:s=64x36:d={seconds}:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         long_path(path)],
        capture_output=True,
    )


def _create_project(client, name: str, source_roots: list[str]) -> dict:
    res = client.post("/api/projects",
                      json={"name": name, "source_roots": source_roots})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _run_scan(client, project_id: str, *, force: bool = False, **options) -> dict:
    """Start a scan Job through the public endpoint and wait for it to settle.

    ``force`` is a top-level field on the job-create body (``options`` carries
    type-specific settings such as ``full_hash``), so it is passed separately.
    """
    body: dict = {"options": options} if options else {}
    if force:
        body["force"] = True
    res = client.post(f"/api/projects/{project_id}/jobs/scan", json=body or None)
    assert res.status_code == 202, res.text
    job_id = res.json()["id"]

    deadline = time.time() + 180
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ACTIVE:
            return job
        time.sleep(0.2)
    pytest.fail(f"scan job {job_id} did not finish in time")


def _assets_page(client, project_id: str, **params) -> dict:
    """The assets endpoint returns a paginated envelope, not a bare list."""
    res = client.get(f"/api/projects/{project_id}/assets", params=params)
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict) and "items" in body, body
    return body


def _assets_via_api(client, project_id: str) -> list[dict]:
    """Every asset for a project, following pagination if the page is full."""
    page = _assets_page(client, project_id, limit=500)
    return page["items"]


# --------------------------------------------------------------------------
# RT254-01: >=270 char root
# --------------------------------------------------------------------------

@requires_windows
def test_scan_deep_root_finds_every_video(client, tmp_path: Path):
    """RT254-01: nothing is silently dropped from a >=270 char source root.

    Layout mixes depth with breadth: videos directly in the deep directory, a
    video in an even deeper sub-directory, and a non-video that must be ignored.
    """
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    assert len(str(deep)) >= 265, len(str(deep))

    for i in range(3):
        _make_video(deep / f"clip{i}.mp4", color=("red", "green", "blue")[i])
    deepest = deep / ("e" * 30)
    _make_video(deepest / "buried.mp4", color="yellow")
    _touch(deepest / "notes.txt", b"not a video")

    project = _create_project(client, "deep-scan", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded", (job["status"], job.get("error"))
    names = sorted(a["display_name"] for a in _assets_via_api(client, project["id"]))
    assert names == ["buried.mp4", "clip0.mp4", "clip1.mp4", "clip2.mp4"], names
    # The deepest file is the one rglob used to lose.
    assert job["stats"]["total"] >= 4


@requires_windows
def test_scan_deep_root_stats_reconcile(client, tmp_path: Path):
    """RT254-01: file count, failures and stats can be reconciled."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    for i in range(2):
        _make_video(deep / f"a{i}.mp4")
    _touch(deep / "readme.md", b"x")

    project = _create_project(client, "deep-scan-stats", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded"
    assert job["stats"]["total"] == 2, job["stats"]
    assert job["failures"]["count"] == 0
    assert len(_assets_via_api(client, project["id"])) == 2


# --------------------------------------------------------------------------
# RT254-02: >=320 chars, nested, CJK + spaces + quote
# --------------------------------------------------------------------------

@requires_windows
def test_scan_320_char_cjk_root(client, tmp_path: Path):
    """RT254-02: a >=320 char root with Chinese, spaces and an apostrophe."""
    _ffmpeg()
    root = tmp_path / "旅行 素材"
    root.mkdir()
    # Chinese chunk keeps the path realistic while still long.
    deep = _make_deep_dir(root, 300, chunk="旅行素材目录" + "x" * 24)
    nested = deep / "二级 目录" / "it's here"
    os.makedirs(long_path(nested), exist_ok=True)
    assert len(str(nested)) >= 320, len(str(nested))

    _make_video(nested / "第一段.mp4", color="red")
    _make_video(nested / "第二段.mp4", color="blue")
    _touch(nested / "封面.jpg", b"not a video")

    project = _create_project(client, "deep-scan-cjk", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded", (job["status"], job.get("error"))
    names = sorted(a["display_name"] for a in _assets_via_api(client, project["id"]))
    assert names == ["第一段.mp4", "第二段.mp4"], names


@requires_windows
def test_scan_reports_damaged_video_as_a_failure(client, tmp_path: Path):
    """RT254-04: a corrupt video is recorded, not quietly skipped."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    _make_video(deep / "good.mp4")
    _touch(deep / "broken.mp4", b"this is definitely not an mp4 container")

    project = _create_project(client, "deep-scan-broken", [str(root)])
    job = _run_scan(client, project["id"])

    # The run completes but must not claim a clean sweep.
    assert job["status"] == "partial_failed", job["status"]
    assert job["failures"]["count"] >= 1

    # The failing file is locatable: `ref` carries the full source path and
    # `message` explains why. (The message itself is ffprobe's stderr, which
    # quotes a long path; the ref is the reliable identifier.)
    items = job["failures"]["items"]
    assert any(i["ref"].endswith("broken.mp4") for i in items), items
    assert any("moov atom" in i["message"] or "probe" in i["message"] for i in items), items

    # Both files are still represented, so the failure is locatable in the UI.
    assets = {a["display_name"]: a for a in _assets_via_api(client, project["id"])}
    assert "broken.mp4" in assets and "good.mp4" in assets
    assert assets["broken.mp4"]["scan_status"] == "failed"
    assert assets["broken.mp4"].get("error_code"), assets["broken.mp4"]
    assert assets["good.mp4"]["scan_status"] == "succeeded"


# --------------------------------------------------------------------------
# RT254-03: rescan and forced rescan
# --------------------------------------------------------------------------

@requires_windows
def test_rescan_reuses_asset_ids_and_is_idempotent(client, tmp_path: Path):
    """RT254-03: a second scan skips unchanged files and keeps stable IDs."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    _make_video(deep / "one.mp4")
    _make_video(deep / "two.mp4")

    project = _create_project(client, "deep-rescan", [str(root)])
    pid = project["id"]

    first = _run_scan(client, pid)
    assert first["status"] == "succeeded"
    assets_first = {a["display_name"]: a["id"] for a in _assets_via_api(client, pid)}
    assert len(assets_first) == 2

    second = _run_scan(client, pid)
    assert second["status"] == "succeeded"
    assets_second = {a["display_name"]: a["id"] for a in _assets_via_api(client, pid)}

    assert assets_first == assets_second, "asset ids must be stable across rescans"
    # Unchanged files are skipped rather than re-probed.
    assert second["stats"].get("skipped", 0) >= 2, second["stats"]

    # No duplicate rows for the same source path.
    with session_scope() as s:
        total = s.scalar(select(func.count()).select_from(Asset).where(Asset.project_id == pid))
    assert total == 2


@requires_windows
def test_force_rescan_reprobes_without_duplicating(client, tmp_path: Path):
    """RT254-03: force re-reads the files but still leaves one row each."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    _make_video(deep / "solo.mp4")

    project = _create_project(client, "deep-force", [str(root)])
    pid = project["id"]
    assert _run_scan(client, pid)["status"] == "succeeded"

    forced = _run_scan(client, pid, force=True)
    assert forced["status"] == "succeeded", (forced["status"], forced.get("error"))
    assert forced["stats"].get("skipped", 0) == 0, forced["stats"]

    assert len(_assets_via_api(client, pid)) == 1


@requires_windows
def test_added_file_is_picked_up_on_rescan(client, tmp_path: Path):
    """A new file under the deep root appears after a rescan."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    _make_video(deep / "first.mp4")

    project = _create_project(client, "deep-add", [str(root)])
    pid = project["id"]
    assert _run_scan(client, pid)["status"] == "succeeded"
    assert len(_assets_via_api(client, pid)) == 1

    _make_video(deep / "second.mp4", color="blue")
    assert _run_scan(client, pid)["status"] == "succeeded"

    names = sorted(a["display_name"] for a in _assets_via_api(client, pid))
    assert names == ["first.mp4", "second.mp4"]


# --------------------------------------------------------------------------
# Source footage stays read-only
# --------------------------------------------------------------------------

@requires_windows
def test_scan_does_not_modify_the_source_root(client, tmp_path: Path):
    """The doc's hard rule: scanning never writes to the footage."""
    _ffmpeg()
    root = tmp_path / "src"
    root.mkdir()
    deep = _make_deep_dir(root, 265)
    video = deep / "original.mp4"
    _make_video(video)

    before = _read(video)
    before_names = sorted(p.name for p in os.scandir(long_path(deep)))

    project = _create_project(client, "deep-readonly", [str(root)])
    assert _run_scan(client, project["id"])["status"] == "succeeded"

    assert _read(video) == before, "source video bytes changed"
    after_names = sorted(p.name for p in os.scandir(long_path(deep)))
    assert after_names == before_names, "scan added or removed files in the source"


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        with open(long_path(path), "rb") as handle:
            return handle.read()


# --------------------------------------------------------------------------
# RT254-04: unreadable sub-directory is reported, not hidden
# --------------------------------------------------------------------------

@requires_windows
def test_unreadable_root_is_reported_as_a_failure(client, tmp_path: Path, monkeypatch):
    """RT254-04: a denied root becomes a per-item error, not an empty success."""
    root = tmp_path / "denied"
    root.mkdir()
    (root / "a.mp4").write_bytes(b"x")

    from app.services import indexer

    real_scandir = indexer.safe_scandir

    def denied(path):
        if Path(path) == root:
            raise PermissionError("access denied")
        return real_scandir(path)

    monkeypatch.setattr(indexer, "safe_scandir", denied)
    monkeypatch.setattr(indexer, "_is_dir", lambda p: True)

    project = _create_project(client, "deep-denied", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "partial_failed", job["status"]
    assert job["failures"]["count"] >= 1
    codes = {i.get("error_code") for i in job["failures"]["items"]}
    assert "source_unavailable" in codes, job["failures"]["items"]


@requires_windows
def test_missing_root_does_not_look_like_an_empty_success(client, tmp_path: Path):
    """A root that no longer exists must be reported."""
    project = _create_project(client, "deep-missing", [str(tmp_path / "gone")])
    job = _run_scan(client, project["id"])

    assert job["status"] == "partial_failed", job["status"]
    assert job["failures"]["count"] >= 1


# --------------------------------------------------------------------------
# RT254-01/08: shallow roots keep working (both platforms)
# --------------------------------------------------------------------------

def test_scan_shallow_root_still_works(client, tmp_path: Path):
    """RT254-08: the ordinary short-path scan is unaffected."""
    _ffmpeg()
    root = tmp_path / "flat"
    root.mkdir()
    _make_video(root / "one.mp4", color="red")
    _make_video(root / "two.mp4", color="green")
    (root / "ignore.txt").write_bytes(b"x")

    project = _create_project(client, "flat-scan", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded", (job["status"], job.get("error"))
    names = sorted(a["display_name"] for a in _assets_via_api(client, project["id"]))
    assert names == ["one.mp4", "two.mp4"]


def test_scan_nested_shallow_tree(client, tmp_path: Path):
    """RT254-08: multi-level short trees still enumerate fully."""
    _ffmpeg()
    root = tmp_path / "tree"
    (root / "a" / "b" / "c").mkdir(parents=True)
    _make_video(root / "top.mp4")
    _make_video(root / "a" / "mid.mp4")
    _make_video(root / "a" / "b" / "c" / "bottom.mp4")

    project = _create_project(client, "tree-scan", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded"
    names = sorted(a["display_name"] for a in _assets_via_api(client, project["id"]))
    assert names == ["bottom.mp4", "mid.mp4", "top.mp4"]


def test_scan_empty_root_succeeds_with_no_assets(client, tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    project = _create_project(client, "empty-scan", [str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded"
    assert job["stats"]["total"] == 0
    assert _assets_via_api(client, project["id"]) == []


def test_two_roots_are_deduplicated(client, tmp_path: Path):
    """RT254-03: overlapping roots must not produce duplicate rows."""
    _ffmpeg()
    root = tmp_path / "shared"
    root.mkdir()
    _make_video(root / "only.mp4")

    # The same directory given twice.
    project = _create_project(client, "dup-roots", [str(root), str(root)])
    job = _run_scan(client, project["id"])

    assert job["status"] == "succeeded"
    assert len(_assets_via_api(client, project["id"])) == 1
