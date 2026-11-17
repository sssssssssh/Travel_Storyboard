"""TSB-371-R1 — the A/B page needs pictures, not just timecodes.

A blind review that shows only "segment 32.0-38.0s, reason: …" asks the reviewer to
judge the *prose*, not the cut. Two plans can read very differently and cut the
same pictures, or read identically and cut badly. Pictures are the point.

So the A/B side needs a preview clip per shot. The existing clip service builds one
per *segment* at the segment's own window; an A/B shot may cut a sub-window of a
segment (4.0-11.0 → 4.0-5.0), so the service must accept an explicit window.

Constraints carried over unchanged:

* the derived clip is silent (audio was never analysed);
* the source is read-only;
* paths never leave the service;
* a window outside the source is refused rather than clamped into nonsense;
* the same (project, segment, window) reuses its clip.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import evaluation_clips as clips


def _uid() -> str:
    return str(uuid.uuid4())


def _root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-abc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg

    found = locate_ffmpeg()
    if not found:
        pytest.skip("ffmpeg not available")
    return str(found)


def _make_video(path: Path, seconds: float = 30.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True, timeout=180)


def _seed() -> tuple[str, str]:
    """A project with one 30s analysed shot."""
    pid, seg_id = _uid(), _uid()
    src = _root() / f"{pid[:8]}.mp4"
    if not src.exists():
        _make_video(src)
    with session_scope() as s:
        s.add(Project(id=pid, name="abc", source_roots=[], output_dir="", settings={}))
        s.flush()
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=30.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=320, height=180, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None,
            scanned_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()
        seg = Segment(
            id=seg_id, asset_id=a.id, project_id=pid,
            detected_start_sec=0.0, detected_end_sec=30.0,
            start_sec=0.0, end_sec=30.0, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg_id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
    return pid, seg_id


# ---------------------------------------------------------------------------
# An explicit window is honoured
# ---------------------------------------------------------------------------

def test_a_sub_window_of_a_segment_can_be_clipped():
    """An A/B shot may cut 4.0-5.0 out of a 0-30s shot."""
    pid, seg_id = _seed()
    with session_scope() as s:
        record = clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
    assert record.duration_sec == pytest.approx(7.0, abs=0.15)


def test_the_default_window_still_uses_the_segment():
    pid, seg_id = _seed()
    with session_scope() as s:
        record = clips.ensure_clip(s, pid, seg_id)
        s.commit()
    # Segment is 30s, but clips are capped at MAX_CLIP_SECONDS.
    assert record.duration_sec == pytest.approx(clips.MAX_CLIP_SECONDS, abs=0.2)


def test_the_same_window_reuses_its_clip():
    pid, seg_id = _seed()
    with session_scope() as s:
        first = clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
        second = clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
    assert first.clip_id == second.clip_id


def test_a_different_window_gets_its_own_clip():
    pid, seg_id = _seed()
    with session_scope() as s:
        a = clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
        b = clips.ensure_clip(s, pid, seg_id, start_sec=11.0, end_sec=14.0)
        s.commit()
    assert a.clip_id != b.clip_id


# ---------------------------------------------------------------------------
# Bad windows are refused, not clamped into nonsense
# ---------------------------------------------------------------------------

def test_an_empty_window_is_refused():
    pid, seg_id = _seed()
    with session_scope() as s:
        with pytest.raises(clips.ClipError) as exc:
            clips.ensure_clip(s, pid, seg_id, start_sec=5.0, end_sec=5.0)
    assert exc.value.code == clips.ERROR_TOO_SHORT


def test_an_inverted_window_is_refused():
    pid, seg_id = _seed()
    with session_scope() as s:
        with pytest.raises(clips.ClipError) as exc:
            clips.ensure_clip(s, pid, seg_id, start_sec=9.0, end_sec=4.0)
    assert exc.value.code == clips.ERROR_TOO_SHORT


def test_a_window_beyond_the_source_is_refused():
    """29.9-999 would otherwise produce a clip that ends in nothing."""
    pid, seg_id = _seed()
    with session_scope() as s:
        with pytest.raises(clips.ClipError) as exc:
            clips.ensure_clip(s, pid, seg_id, start_sec=29.9, end_sec=999.0)
    assert exc.value.code in (clips.ERROR_TOO_SHORT, "EVAL_CLIP_WINDOW_OUTSIDE_SOURCE")


def test_a_negative_start_is_refused():
    pid, seg_id = _seed()
    with session_scope() as s:
        with pytest.raises(clips.ClipError):
            clips.ensure_clip(s, pid, seg_id, start_sec=-5.0, end_sec=4.0)


# ---------------------------------------------------------------------------
# The clip is still silent and the source untouched
# ---------------------------------------------------------------------------

def test_a_windowed_clip_is_silent():
    pid, seg_id = _seed()
    with session_scope() as s:
        record = clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
    path = clips.clip_media_path(pid, record.clip_id)
    probe = str(Path(_ffmpeg()).with_name("ffprobe" + Path(_ffmpeg()).suffix))
    if not Path(probe).exists():
        pytest.skip("ffprobe not available")
    import json
    out = subprocess.run(
        [probe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    streams = json.loads(out.stdout or "{}").get("streams", [])
    assert not any(s.get("codec_type") == "audio" for s in streams)


def test_a_windowed_clip_does_not_touch_the_source():
    pid, seg_id = _seed()
    with session_scope() as s:
        asset = s.scalars(
            __import__("sqlalchemy").select(Asset).where(Asset.project_id == pid)
        ).first()
        src = Path(asset.source_path)
    before = (src.stat().st_size, src.stat().st_mtime_ns)
    with session_scope() as s:
        clips.ensure_clip(s, pid, seg_id, start_sec=4.0, end_sec=11.0)
        s.commit()
    assert (src.stat().st_size, src.stat().st_mtime_ns) == before
