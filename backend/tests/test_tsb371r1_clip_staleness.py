"""TSB-371-R1 P1-06 — a stale clip must not play.

TSB-371 could *detect* staleness (``clip_status`` compared the source signature and
the segment window) but the media route never asked. So after the source file was
replaced or the shot re-trimmed, the old clip kept streaming as if it were current —
the reviewer would compare the model's words against footage that is no longer
behind those words. That is worse than showing nothing.

The media route must refuse, with a distinct code, so the page can say "regenerate".

Everything that already worked must keep working: Range and HEAD requests, the
time mapping, the silent derived clip, and the read-only source rule.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import evaluation_clips as clips

CLIPS = "/api/projects/{p}/evaluation/clips"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-p6"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg

    found = locate_ffmpeg()
    if not found:
        pytest.skip("ffmpeg not available")
    return str(found)


def _make_video(path: Path, *, seconds: float = 8.0, color: str = "red",
                pattern: str = "color") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-loglevel", "error"]
    if pattern == "color":
        cmd += ["-f", "lavfi", "-i",
                f"color=c={color}:s=320x180:d={seconds}:r=25"]
    else:
        cmd += ["-f", "lavfi", "-i",
                f"testsrc=size=320x180:rate=25:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)


def _seed(client, name="p6", *, start=0.0, end=4.0):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    src = _tmp_root() / f"{pid[:8]}.mp4"
    if not src.exists():
        _make_video(src)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=8.0, fps_num=25, fps_den=1,
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
            id=_uid(), asset_id=a.id, project_id=pid,
            detected_start_sec=start, detected_end_sec=end,
            start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"], "subjects": ["boats"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
        return pid, seg.id, a.id, src


def _make_clip(client, pid, seg_id) -> dict:
    r = client.post(CLIPS.format(p=pid), json={"segment_id": seg_id})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _media(client, pid, clip_id, **kwargs):
    return client.get(f"{CLIPS.format(p=pid)}/{clip_id}/media", **kwargs)


# ---------------------------------------------------------------------------
# A fresh clip plays
# ---------------------------------------------------------------------------

def test_a_fresh_clip_streams(client):
    pid, seg_id, _aid, _src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    r = _media(client, pid, clip["clip_id"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/")


def test_a_fresh_clip_supports_head(client):
    pid, seg_id, _aid, _src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    r = client.head(f"{CLIPS.format(p=pid)}/{clip['clip_id']}/media")
    assert r.status_code == 200


def test_a_fresh_clip_supports_range(client):
    pid, seg_id, _aid, _src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    r = _media(client, pid, clip["clip_id"], headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert 0 < len(r.content) <= 100


# ---------------------------------------------------------------------------
# A stale clip is refused
# ---------------------------------------------------------------------------

def test_a_clip_whose_source_changed_is_refused(client):
    """The source was replaced: the clip no longer shows what the words describe."""
    pid, seg_id, _aid, src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    assert _media(client, pid, clip["clip_id"]).status_code == 200

    # Replace the file with different content, then bump its stat.
    _make_video(src, color="blue", pattern="testsrc")

    r = _media(client, pid, clip["clip_id"])
    assert r.status_code == 409, r.text
    body = r.json()
    code = body.get("detail", {}).get("code") if isinstance(body.get("detail"), dict) \
        else body.get("detail")
    assert code == "EVAL_CLIP_STALE"


def test_a_clip_whose_segment_window_changed_is_refused(client):
    pid, seg_id, _aid, _src = _seed(client, start=0.0, end=4.0)
    clip = _make_clip(client, pid, seg_id)

    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        seg.start_sec, seg.end_sec = 1.0, 5.0
        s.flush()

    r = _media(client, pid, clip["clip_id"])
    assert r.status_code == 409
    assert "STALE" in r.text


def test_the_stale_error_says_how_to_recover(client):
    pid, seg_id, _aid, src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    _make_video(src, color="green", pattern="testsrc")
    r = _media(client, pid, clip["clip_id"])
    # The page needs an actionable message, not just a status code.
    text = json.dumps(r.json(), ensure_ascii=False)
    assert "regenerate" in text.lower() or "重新生成" in text


def test_regenerating_clears_the_stale_state(client):
    pid, seg_id, _aid, src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    _make_video(src, color="blue", pattern="testsrc")
    assert _media(client, pid, clip["clip_id"]).status_code == 409

    fresh = _make_clip(client, pid, seg_id)
    assert _media(client, pid, fresh["clip_id"]).status_code == 200


# ---------------------------------------------------------------------------
# Ownership and containment still hold
# ---------------------------------------------------------------------------

def test_another_projects_clip_is_404(client):
    pid_a, seg_a, _aid, _src = _seed(client, name="p6-a")
    pid_b, _seg_b, _a2, _s2 = _seed(client, name="p6-b")
    clip = _make_clip(client, pid_a, seg_a)
    assert _media(client, pid_b, clip["clip_id"]).status_code == 404


def test_a_traversing_clip_id_never_serves_files(client):
    pid, seg_id, _aid, _src = _seed(client)
    _make_clip(client, pid, seg_id)
    for bad in ("../../etc/passwd", "..%2f..%2fwin.ini", "a/../../b",
                "..\\..\\windows\\win.ini"):
        r = _media(client, pid, bad)
        assert r.status_code in (404, 422), (bad, r.status_code)


def test_an_unknown_clip_id_is_404(client):
    pid, _seg, _aid, _src = _seed(client)
    assert _media(client, pid, _uid()).status_code == 404


# ---------------------------------------------------------------------------
# The source stays read-only
# ---------------------------------------------------------------------------

def test_serving_a_clip_does_not_touch_the_source(client):
    pid, seg_id, _aid, src = _seed(client)
    before = (src.stat().st_size, src.stat().st_mtime_ns)
    clip = _make_clip(client, pid, seg_id)
    _media(client, pid, clip["clip_id"])
    after = (src.stat().st_size, src.stat().st_mtime_ns)
    assert after == before


def test_the_clip_is_silent(client):
    """Audio is never analysed, so the derived clip carries none."""
    pid, seg_id, _aid, _src = _seed(client)
    clip = _make_clip(client, pid, seg_id)
    assert clip["audio_included"] is False
    path = None
    with session_scope():
        pass
    path = clips.clip_media_path(pid, clip["clip_id"])
    probe = str(Path(_ffmpeg()).with_name("ffprobe" + Path(_ffmpeg()).suffix))
    if not Path(probe).exists():
        pytest.skip("ffprobe not available")
    out = subprocess.run(
        [probe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    streams = json.loads(out.stdout or "{}").get("streams", [])
    assert not any(s.get("codec_type") == "audio" for s in streams)
