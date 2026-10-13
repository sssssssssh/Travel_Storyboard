"""End-to-end media pipeline tests — run only when FFmpeg is available.

CI has no FFmpeg, so every test here **skips** there; locally (and on a machine
with FFmpeg on PATH or configured via ``TRAVELSB_FFMPEG_PATH``) they exercise
the real decode/extract/transcode path that the unit suite deliberately avoids.

This is the "集成 FFmpeg 测试" the audit asked for, without making CI depend on
a 100 MB binary download.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.ffmpeg import (
    extract_frame,
    ffmpeg_available,
    ffprobe_available,
    locate_ffprobe,
    probe_media,
    run,
)
from app.models import Asset, Frame, Segment
from app.paths import frames_dir, project_cache_dir
from app.services.analyzer import _detect_config_key, _analyze_asset, _detected_key
from app.services.indexer import index_one
from app.services.roughcut import _clamp_duration
from app.config import get_settings


def _ffmpeg_or_skip() -> str:
    """Return a *usable* ffmpeg path or skip.

    ``locate_ffmpeg()`` falls back to the bare string ``"ffmpeg"`` so callers can
    build a command line before checking; that is not proof the binary exists.
    Use the same availability probe the health endpoint uses.
    """
    exe = ffmpeg_available()
    if not exe or not ffprobe_available():
        pytest.skip("ffmpeg/ffprobe not available")
    return exe


@pytest.fixture(scope="module")
def synthetic_clip(tmp_path_factory) -> Path:
    """A 6-second 640x360 clip with three distinct colour sections + audio.

    Built with ffmpeg's own generators so the fixture has no binary dependency.
    """
    exe = _ffmpeg_or_skip()
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    cmd = [
        exe, "-y",
        # 2s red, 2s green, 2s blue (concatenated so there are real hard cuts)
        "-f", "lavfi", "-i", "color=c=red:s=640x360:d=2:r=30",
        "-f", "lavfi", "-i", "color=c=green:s=640x360:d=2:r=30",
        "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=2:r=30",
        # A quiet tone so has_audio is true.
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        pytest.skip(f"could not synthesise a clip: {proc.stderr.decode()[-300:]}")
    return out


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "ffmpeg-it", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


# --- probing -----------------------------------------------------------------

def test_probe_reads_real_metadata(synthetic_clip: Path):
    _ffmpeg_or_skip()
    info = probe_media(synthetic_clip)
    assert info.duration_sec == pytest.approx(6.0, abs=0.5)
    assert info.has_video is True
    assert info.video.width == 640 and info.video.height == 360
    assert info.has_audio is True


def test_index_one_succeeds_on_a_real_file(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    asset_id, action = index_one(project["id"], synthetic_clip)
    assert action in ("created", "updated"), action

    with session_scope() as s:
        asset = s.get(Asset, asset_id)
        assert asset.scan_status == "succeeded"
        assert asset.duration_sec > 0
        assert asset.width == 640


def test_reindexing_does_not_duplicate(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    first, _ = index_one(project["id"], synthetic_clip)
    second, _ = index_one(project["id"], synthetic_clip)
    assert first == second

    with session_scope() as s:
        count = s.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == project["id"])
        )
    assert count == 1


# --- frame extraction --------------------------------------------------------

def test_extract_frame_writes_a_readable_jpeg(tmp_path: Path, synthetic_clip: Path):
    _ffmpeg_or_skip()
    from PIL import Image

    out = tmp_path / "frame.jpg"
    extract_frame(synthetic_clip, 1.0, out, width=320)
    assert out.is_file() and out.stat().st_size > 0
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size[0] == 320


def test_extract_preview_with_audio(tmp_path: Path, synthetic_clip: Path):
    _ffmpeg_or_skip()
    from app.ffmpeg import extract_preview

    out = tmp_path / "preview.mp4"
    extract_preview(synthetic_clip, 0.5, 2.0, out, width=320, keep_audio=True)
    assert out.is_file()

    probe = locate_ffprobe()
    proc = run([
        probe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out),
    ])
    assert b"aac" in proc.stdout.lower() or b"mp4a" in proc.stdout.lower()


# --- detection + analysis end-to-end ----------------------------------------

def test_detect_finds_the_three_colour_sections(client, project, synthetic_clip: Path):
    """The synthetic clip has two hard cuts, so detection should find ~3 shots."""
    _ffmpeg_or_skip()
    from app.services.detector import detect_segments

    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)

    st = get_settings()
    detection = detect_segments(
        asset.source_path,
        asset.duration_sec,
        sample_fps=st.detect_sample_fps,
        threshold=st.detect_threshold,
        max_segment_sec=st.detect_max_segment_sec,
    )
    assert len(detection.boundaries) >= 2, [b.reason for b in detection.boundaries]
    # Boundaries must tile the clip without gaps.
    for a, b in zip(detection.boundaries, detection.boundaries[1:]):
        assert abs(a.end_sec - b.start_sec) < 0.01


def test_analyze_asset_creates_segments_frames_and_survives_rerun(
    client, project, synthetic_clip: Path
):
    """Full analysis, then a forced re-run: ids and annotations must persist."""
    _ffmpeg_or_skip()
    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)

    first = _analyze_asset(project["id"], asset)
    assert first["segments"] >= 2
    assert first["frames"] >= 2

    with session_scope() as s:
        segs = s.scalars(
            select(Segment).where(Segment.project_id == project["id"]).order_by(Segment.start_sec)
        ).all()
        target = segs[0]
        target_id = target.id
        detected = (target.detected_start_sec, target.detected_end_sec)
        # Simulate a user trim + favourite.
        span = target.end_sec - target.start_sec
        target.start_sec = target.start_sec + span * 0.25
        target.end_sec = target.end_sec - span * 0.25
        target.is_trimmed = True
        target.user_rating = "favorite"

    with session_scope() as s:
        asset = s.get(Asset, asset.id)
        s.expunge(asset)
    second = _analyze_asset(project["id"], asset)

    with session_scope() as s:
        again = s.get(Segment, target_id)
        assert again is not None, "trimmed segment was destroyed by re-analysis"
        assert again.user_rating == "favorite"
        assert again.is_trimmed is True
        assert (again.detected_start_sec, again.detected_end_sec) == detected
        assert _detected_key(again) == _detected_key(
            type(again)(
                asset_id=again.asset_id,
                project_id=again.project_id,
                detected_start_sec=detected[0],
                detected_end_sec=detected[1],
            )
        )

    # Frame cache must not grow across runs.
    frames_on_disk = list(frames_dir(project["id"], asset.id).glob("*.jpg"))
    with session_scope() as s:
        rows = s.scalar(
            select(func.count()).select_from(Frame).where(Frame.asset_id == asset.id)
        )
    assert len(frames_on_disk) == rows, (len(frames_on_disk), rows)
    assert second.get("frames_reclaimed", 0) >= 0


def test_detect_config_key_is_stable():
    st = get_settings()
    assert _detect_config_key(st) == _detect_config_key(st)


# --- rough cut duration clamping --------------------------------------------

def test_clamp_duration_never_exceeds_source(synthetic_clip: Path):
    _ffmpeg_or_skip()
    from app.models import EditPlanShot

    asset = Asset(
        id=str(uuid.uuid4()),
        project_id="p",
        source_path=str(synthetic_clip),
        display_name="s.mp4",
        duration_sec=6.0,
        has_audio=True,
    )
    # Planned longer than the selection -> clamped to the selection.
    shot = EditPlanShot(
        project_id="p", order=0, source_in_sec=1.0, source_out_sec=3.0,
        target_duration_sec=30.0,
    )
    assert _clamp_duration(shot, asset) == pytest.approx(2.0)

    # Selection running past the end of the file -> clamped to what exists.
    shot2 = EditPlanShot(
        project_id="p", order=1, source_in_sec=4.0, source_out_sec=10.0,
        target_duration_sec=6.0,
    )
    assert _clamp_duration(shot2, asset) == pytest.approx(2.0)

    # A normal shortened shot is honoured.
    shot3 = EditPlanShot(
        project_id="p", order=2, source_in_sec=0.0, source_out_sec=4.0,
        target_duration_sec=1.5,
    )
    assert _clamp_duration(shot3, asset) == pytest.approx(1.5)


def test_project_cache_dir_exists_after_analysis(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)
    _analyze_asset(project["id"], asset)
    root = project_cache_dir(project["id"])
    assert (root / "frames").is_dir()
