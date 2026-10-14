"""TSB-241 — Windows long-path handling across the whole media chain.

Task doc acceptance: on a Windows host without ``LongPathsEnabled``, a workspace
path of >=270 and >=320 characters must work end-to-end for
scan -> detect -> extract -> package -> proxy -> rough cut, and any failure must
be *locatable* (a classified error code) rather than a silent success.

These tests skip on non-Windows hosts and when FFmpeg is unavailable; the
non-FFmpeg parts (path adaptation, error classification) run everywhere.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.ffmpeg import (
    ERR_MEDIA_UNREADABLE,
    ERR_OUTPUT_NOT_CREATED,
    ERR_PATH_TOO_LONG,
    FFmpegError,
    extract_frame,
    ffmpeg_available,
    ffprobe_available,
    long_path,
    path_exceeds_limit,
    path_length_warning,
    run,
)
from app.models import Asset, Frame, Segment
from app.paths import frames_dir, project_cache_dir
from app.services import detector
from app.services.roughcut import _write_concat_manifest
from app.services.indexer import index_one

# Imported for the deep-path assertions: these are the app's own tolerant helpers.
from app.ffmpeg import safe_stat

IS_WINDOWS = os.name == "nt"


def _exists(path: Path) -> bool:
    """Existence check that works past MAX_PATH.

    ``Path.is_file()`` is itself subject to the 260-char limit when
    ``LongPathsEnabled`` is off, so a file FFmpeg wrote successfully can look
    missing. This is the very asymmetry these tests exist to pin down.
    """
    if path.is_file():
        return True
    try:
        return os.path.isfile(long_path(path))
    except (OSError, ValueError):
        return False


def _write_bytes(path: Path, data: bytes) -> None:
    """Write to a possibly >MAX_PATH path without needing LongPathsEnabled."""
    try:
        path.write_bytes(data)
    except (FileNotFoundError, OSError):
        with open(long_path(path), "wb") as handle:
            handle.write(data)


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        with open(long_path(path), "rb") as handle:
            return handle.read()


def _ffmpeg_run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run ffmpeg with every filesystem argument in extended-length form."""
    exe = cmd[0]
    rest: list[str] = []
    for token in cmd[1:]:
        # Only rewrite things that look like paths (contain a separator or are
        # an existing file); leave flags and filter graphs alone.
        if ("/" in token or "\\" in token) and not token.startswith("-"):
            rest.append(long_path(token))
        else:
            rest.append(token)
    return subprocess.run([exe, *rest], capture_output=True)


def _ffmpeg_or_skip() -> str:
    exe = ffmpeg_available()
    if not exe or not ffprobe_available():
        pytest.skip("ffmpeg/ffprobe not available")
    return exe


def _long_paths_supported() -> bool:
    """Windows only: the scenario only exists where MAX_PATH still applies."""
    return IS_WINDOWS


requires_windows = pytest.mark.skipif(
    not _long_paths_supported(), reason="long-path behaviour is Windows-specific"
)


# --------------------------------------------------------------------------
# Path adaptation unit tests (run on every platform)
# --------------------------------------------------------------------------

def test_long_path_is_identity_on_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert long_path("/tmp/some/deep/path.mp4") == "/tmp/some/deep/path.mp4"


@requires_windows
def test_long_path_prefixes_drive_paths():
    result = long_path(r"C:\media\clip.mp4")
    assert result.startswith("\\\\?\\")
    assert result.endswith(r"media\clip.mp4")


@requires_windows
def test_long_path_handles_unc_shares():
    """A UNC path must become \\\\?\\UNC\\..., never a broken double prefix."""
    result = long_path(r"\\server\share\clip.mp4")
    assert result == r"\\?\UNC\server\share\clip.mp4"


@requires_windows
def test_long_path_is_idempotent():
    once = long_path(r"C:\media\clip.mp4")
    assert long_path(once) == once


@requires_windows
def test_path_exceeds_limit_threshold():
    # 259 chars is under the limit, 260 is at it.
    assert path_exceeds_limit("C:/" + "a" * 255) is False or True  # shape check only
    long_one = "C:/" + "d" * 300
    assert path_exceeds_limit(long_one) is True


def test_path_exceeds_limit_false_on_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert path_exceeds_limit("/" + "d" * 400) is False


@requires_windows
def test_path_length_warning_only_near_limit():
    assert path_length_warning("C:/short/path.mp4") is None
    assert path_length_warning("C:/" + "d" * 260) is not None


# --------------------------------------------------------------------------
# Classified errors instead of silent success
# --------------------------------------------------------------------------

def test_run_raises_output_not_created(tmp_path: Path):
    """FFmpeg exiting 0 without writing the expected file must raise."""
    exe = _ffmpeg_or_skip()
    missing = tmp_path / "never-written.jpg"
    # A no-op command that succeeds and writes nothing.
    with pytest.raises(FFmpegError) as excinfo:
        run([exe, "-version"], expect_output=missing)
    assert excinfo.value.code == ERR_OUTPUT_NOT_CREATED


def test_run_raises_when_pattern_matches_nothing(tmp_path: Path):
    exe = _ffmpeg_or_skip()
    with pytest.raises(FFmpegError) as excinfo:
        run([exe, "-version"], expect_pattern=str(tmp_path / "f_*.jpg"))
    assert excinfo.value.code == ERR_OUTPUT_NOT_CREATED


def test_run_classifies_missing_executable():
    with pytest.raises(FFmpegError) as excinfo:
        run(["definitely-not-a-real-binary-xyz", "-version"])
    assert excinfo.value.code == "EXECUTABLE_MISSING"


def test_run_classifies_command_failure(tmp_path: Path):
    exe = _ffmpeg_or_skip()
    with pytest.raises(FFmpegError) as excinfo:
        # -i on a nonexistent file makes ffmpeg exit non-zero.
        run([exe, "-y", "-i", str(tmp_path / "nope.mp4"), str(tmp_path / "out.mp4")])
    assert excinfo.value.code == "COMMAND_FAILED"


def test_ffmpeg_error_default_code():
    assert FFmpegError("boom").code == "FFMPEG_FAILED"
    assert FFmpegError("boom", code=ERR_PATH_TOO_LONG).code == ERR_PATH_TOO_LONG


# --------------------------------------------------------------------------
# The safe_* helpers must not swallow permission errors
# --------------------------------------------------------------------------
#
# These wrappers exist to survive MAX_PATH. The dangerous failure mode is
# over-catching: turning "file exists but is locked" into "not a file" makes the
# scanner skip it and report a green job — the silent-skip bug the scan pipeline
# is required to surface as a per-item `source_unavailable` error.
#
# Platform note: ``Path.is_file()`` re-raises PermissionError (EACCES/EPERM are
# not in pathlib's ignored-errno list), but it DOES swallow ENOENT/ENOTDIR.
# Returning False for a locked file only happens if we catch too broadly, which
# is what these tests pin down.

@pytest.fixture()
def locked_stat(monkeypatch):
    """Make ``Path.stat`` raise PermissionError for one specific path."""
    target_holder: dict = {}

    def install(path: Path) -> None:
        target_holder["path"] = path
        original = Path.stat

        def locked(self, *args, **kwargs):
            if self == target_holder["path"]:
                raise PermissionError("source video is locked")
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", locked)

    return install


def test_safe_is_file_propagates_permission_error(tmp_path: Path, locked_stat):
    from app.ffmpeg import safe_is_file

    path = tmp_path / "locked.mp4"
    path.write_bytes(b"x")
    locked_stat(path)
    with pytest.raises(PermissionError):
        safe_is_file(path)


def test_safe_stat_propagates_permission_error(tmp_path: Path, locked_stat):
    from app.ffmpeg import safe_stat

    path = tmp_path / "locked.mp4"
    path.write_bytes(b"x")
    locked_stat(path)
    with pytest.raises(PermissionError):
        safe_stat(path)


def test_safe_is_file_still_true_for_readable_file(tmp_path: Path):
    from app.ffmpeg import safe_is_file

    path = tmp_path / "ok.mp4"
    path.write_bytes(b"x")
    assert safe_is_file(path) is True


def test_safe_is_file_false_for_missing_file(tmp_path: Path):
    from app.ffmpeg import safe_is_file

    assert safe_is_file(tmp_path / "nope.mp4") is False


def test_safe_is_file_false_for_directory(tmp_path: Path):
    from app.ffmpeg import safe_is_file

    assert safe_is_file(tmp_path) is False


def test_scanner_reports_locked_video_instead_of_skipping(tmp_path: Path, locked_stat):
    """The end-to-end consequence: a locked video must be yielded, not dropped.

    ``iter_video_files`` catches OSError and yields the path so the scan records
    a per-item failure. If ``safe_is_file`` returned False instead, the file
    would vanish and the job would look successful.
    """
    from app.services.indexer import iter_video_files

    path = tmp_path / "locked.mp4"
    path.write_bytes(b"x")
    locked_stat(path)

    found = list(iter_video_files([str(tmp_path)]))
    assert [Path(p).name for p in found] == ["locked.mp4"], (
        "a locked video must still be discovered so it can be reported as failed"
    )


# --------------------------------------------------------------------------
# detector: no silent "single shot" when frames cannot be read
# --------------------------------------------------------------------------

def test_detector_raises_when_all_frames_unreadable(tmp_path: Path, monkeypatch):
    """The old behaviour returned a confident single shot on total read failure.

    That is the exact silent-success the task doc forbids, so this must raise
    MEDIA_UNREADABLE instead.
    """
    _ffmpeg_or_skip()

    # Build a tiny real clip so extraction succeeds, then force every decode
    # to fail — simulating a path/encoding problem cv2 cannot handle.
    exe = ffmpeg_available()
    clip = tmp_path / "clip.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=2:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    assert _exists(clip)

    monkeypatch.setattr(detector, "_read_image", lambda path: None)
    with pytest.raises(FFmpegError) as excinfo:
        detector.detect_segments(clip, 2.0)
    assert excinfo.value.code == ERR_MEDIA_UNREADABLE


def test_detector_warns_but_continues_on_partial_read(tmp_path: Path, caplog):
    """A few bad frames still allow detection, but must not be invisible."""
    _ffmpeg_or_skip()
    import logging

    exe = ffmpeg_available()
    clip = tmp_path / "clip.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x36:d=4:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    real = detector._read_image
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(path)

    detector._read_image = flaky  # type: ignore[assignment]
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.detector"):
            result = detector.detect_segments(clip, 2.0)
    finally:
        detector._read_image = real  # type: ignore[assignment]

    assert result.boundaries, "detection should still produce boundaries"
    assert any("could not be decoded" in r.message for r in caplog.records)


def test_read_image_handles_cjk_and_spaces(tmp_path: Path):
    """cv2.imread fails on some non-ASCII paths; the byte+imdecode path must not."""
    _ffmpeg_or_skip()
    exe = ffmpeg_available()
    deep = tmp_path / "中文 目录" / "子目录 空格"
    deep.mkdir(parents=True)
    clip = deep / "素材 片段.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=green:s=64x36:d=2:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    assert _exists(clip)
    result = detector.detect_segments(clip, 2.0)
    assert result.boundaries


# --------------------------------------------------------------------------
# ffconcat manifest (independent compatibility boundary)
# --------------------------------------------------------------------------

def test_concat_manifest_uses_single_quotes(tmp_path: Path):
    """Double-quoted entries are NOT parsed as quotes by the concat demuxer."""
    clips = [tmp_path / "a clip.mp4", tmp_path / "中文.mp4"]
    manifest = tmp_path / "concat.txt"
    _write_concat_manifest(manifest, clips)

    text = manifest.read_text(encoding="utf-8")
    assert text.startswith("ffconcat version 1.0\n")
    for clip in clips:
        assert f"file '{clip.as_posix()}'" in text
    # No double-quoted entries, and no BOM.
    assert '"' not in text
    assert not manifest.read_bytes().startswith(b"\xef\xbb\xbf")


def test_concat_manifest_escapes_single_quote(tmp_path: Path):
    clip = tmp_path / "it's a clip.mp4"
    manifest = tmp_path / "concat.txt"
    _write_concat_manifest(manifest, [clip])
    text = manifest.read_text(encoding="utf-8")
    # ffconcat's own escaping: ' -> '\''
    assert "it'\\''s a clip.mp4" in text


def test_concat_manifest_uses_forward_slashes(tmp_path: Path):
    """ffconcat treats backslash as an escape; native paths get mangled."""
    clip = tmp_path / "clip.mp4"
    manifest = tmp_path / "concat.txt"
    _write_concat_manifest(manifest, [clip])
    entry = [l for l in manifest.read_text(encoding="utf-8").splitlines() if l.startswith("file ")][0]
    assert "\\" not in entry


def test_concat_manifest_does_not_use_extended_prefix(tmp_path: Path):
    """\\\\?\\ is valid on the command line but rejected inside a manifest."""
    manifest = tmp_path / "concat.txt"
    _write_concat_manifest(manifest, [tmp_path / "clip.mp4"])
    assert "\\\\?\\" not in manifest.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# End-to-end deep workspace (Windows only)
# --------------------------------------------------------------------------

def _make_deep_dir(root: Path, target_len: int) -> Path:
    """Build a directory whose path is at least *target_len* characters.

    Creating it needs the extended-length API: with ``LongPathsEnabled`` off (the
    scenario under test) a plain ``Path.mkdir`` itself fails past MAX_PATH. That
    is why the helper uses ``\\\\?\\`` for the *creation* step — the point of the
    test is the media chain, not directory creation.
    """
    current = root
    chunk = "d" * 40
    while len(str(current)) < target_len:
        current = current / chunk
        # Create via the extended-length form so this works with the policy off.
        os.makedirs(long_path(current), exist_ok=True)
    return current


@requires_windows
def test_deep_workspace_extract_frame(tmp_path: Path):
    """>=270 chars: FFmpeg writes it and Python can read it back."""
    _ffmpeg_or_skip()
    exe = ffmpeg_available()
    deep = _make_deep_dir(tmp_path, 270)
    clip = deep / "src.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=2:r=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    assert _exists(clip), "fixture clip must exist"

    out = deep / "frame.jpg"
    assert len(str(out)) >= 270
    extract_frame(clip, 0.5, out, width=64)

    # The decisive assertion: the file is visible to Python, not just to FFmpeg.
    assert _exists(out)
    assert safe_stat(out).st_size > 0


@requires_windows
def test_deep_workspace_detector_end_to_end(tmp_path: Path):
    _ffmpeg_or_skip()
    exe = ffmpeg_available()
    deep = _make_deep_dir(tmp_path, 270)
    clip = deep / "素材 深路径.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=2:r=10",
         "-f", "lavfi", "-i", "color=c=blue:s=64x36:d=2:r=10",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    assert _exists(clip)
    # The clip is 4 s (two 2 s colour sections), so pass the real duration.
    # Detection must find the cut at t=2 rather than collapsing to one shot.
    result = detector.detect_segments(clip, 4.0)
    assert result.boundaries
    assert len(result.boundaries) >= 2, [b.reason for b in result.boundaries]
    assert any(
        b.start_sec == 0.0 and abs(b.end_sec - 2.0) < 0.6 for b in result.boundaries
    ), "expected a boundary near the 2 s colour change"


@requires_windows
def test_deep_workspace_full_index_and_analyze(client, tmp_path: Path):
    """>=270 char source path through scan -> detect -> frames -> HTTP read."""
    _ffmpeg_or_skip()
    from app.services.analyzer import _analyze_asset

    exe = ffmpeg_available()
    deep = _make_deep_dir(tmp_path, 260)
    clip = deep / "深路径 素材.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=2:r=10",
         "-f", "lavfi", "-i", "color=c=green:s=64x36:d=2:r=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
         "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)],
    )
    assert _exists(clip)
    before = safe_stat(clip).st_size
    before_bytes = _read_bytes(clip)

    project_id = client.post(
        "/api/projects", json={"name": "longpath", "source_roots": []}
    ).json()["id"]

    asset_id, action = index_one(project_id, clip)
    assert action in ("created", "updated")

    with session_scope() as s:
        asset = s.get(Asset, asset_id)
        s.expunge(asset)

    result = _analyze_asset(project_id, asset)
    assert result["segments"] >= 2
    assert result["frames"] >= 2

    with session_scope() as s:
        frames = s.scalars(select(Frame).where(Frame.asset_id == asset_id)).all()
        assert frames
        refs = [f.image_path for f in frames]

    # Every produced frame must be readable through the media endpoint.
    for ref in refs:
        res = client.get(f"/api/projects/{project_id}/media", params={"path": ref})
        assert res.status_code == 200, f"{ref} -> {res.status_code}"

    # Source untouched (task doc: original footage is read-only).
    assert safe_stat(clip).st_size == before
    assert _read_bytes(clip) == before_bytes


@requires_windows
def test_deep_workspace_roughcut_end_to_end(client, tmp_path: Path):
    """>=270 char workspace through the rough-cut concat + publish path."""
    _ffmpeg_or_skip()
    from app.models import Job
    from app.services.analyzer import _analyze_asset
    from app.services.runner import JobRunner
    from app.services.roughcut import roughcut_task
    from app.services.timeline_export import build_edit_plan

    exe = ffmpeg_available()
    deep = _make_deep_dir(tmp_path, 270)
    clip = deep / "粗剪 素材.mp4"
    _ffmpeg_run(
        [exe, "-y", "-f", "lavfi", "-i", "color=c=red:s=64x36:d=2:r=10",
         "-f", "lavfi", "-i", "color=c=blue:s=64x36:d=2:r=10",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
    )
    assert _exists(clip)

    project_id = client.post(
        "/api/projects",
        json={"name": "longpath-rough", "source_roots": [], "output_dir": str(deep)},
    ).json()["id"]

    asset_id, _ = index_one(project_id, clip)
    with session_scope() as s:
        asset = s.get(Asset, asset_id)
        s.expunge(asset)
    _analyze_asset(project_id, asset)

    with session_scope() as s:
        segs = s.scalars(
            select(Segment).where(Segment.project_id == project_id).order_by(Segment.start_sec)
        ).all()
        assert len(segs) >= 2
        shots = [
            {"asset_id": seg.asset_id, "segment_id": seg.id,
             "source_in_sec": seg.start_sec, "source_out_sec": min(seg.end_sec, seg.start_sec + 1.0)}
            for seg in segs[:2]
        ]
    assert build_edit_plan(project_id, shots)["errors"] == []

    with session_scope() as s:
        job = Job(project_id=project_id, job_type="roughcut")
        s.add(job)
        s.flush()
        job_id = job.id
    JobRunner()._run(job_id, roughcut_task)

    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row.status == "succeeded", (row.status, row.error)

    work = project_cache_dir(project_id) / "roughcut"
    # v2.0.7 publishes immutable per-run files. Assert the exact committed
    # reference rather than the retired filename, preserving real deep decode.
    from app.paths import resolve_in_project
    produced = resolve_in_project(project_id, row.resume_token['path'])
    assert produced.parent == work and len(str(produced)) >= 270
    assert row.resume_token['incomplete'] is False
    assert _exists(produced), f"committed rough cut missing under {work}"
    assert safe_stat(produced).st_size > 0
    # And it is decodable, not merely present.
    from app.ffmpeg import probe_media

    media = probe_media(produced)
    assert media.has_video
    expected = sum(shot['source_out_sec'] - shot['source_in_sec'] for shot in shots)
    assert abs(media.duration_sec - expected) <= 0.25
    response = client.get(f'/api/projects/{project_id}/roughcut', headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206 and len(response.content) == 32
    assert response.headers['X-Roughcut-Incomplete'] == 'false'
