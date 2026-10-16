"""TSB-334 — the video job must obtain the *video* provider, not the image one.

The defect this file reproduces
-------------------------------
``video_analyze_task()`` called ``vision.get_provider()``, whose only adapter is
``OpenAICompatibleVisionProvider`` — an image provider with **no**
``complete_video()``. The call site then did ``provider.complete_video(...)``,
so on any run that actually reached the network the code raised
``AttributeError``. Two defects compounded here: the broad ``except Exception``
at that call site recorded it as a generic failure, and the code it recorded
(``MODEL_FAILED``) **did not exist** in the error registry, so the handler itself
raised ``NameError``. The visible result was a job that could not report why it
failed, and never produced an observation — a silent dead channel the unit tests
never noticed, because they either (a) stopped at the capability gate, or
(b) monkeypatched ``_analyze_one`` to return a fake success.

Why the existing tests missed it
--------------------------------
Gating tests assert refusals, which happen *before* the provider is built.
``test_video_job.py`` tests that do run the task monkeypatch ``plan_video_job``
to inject an unverified/unsupported capability, so the provider is never reached.
Nothing asserted **which class** the production call site instantiated.

What this file does differently
-------------------------------
It runs the real task function with a *verified* capability and a real loopback
HTTP server, and asserts:

* the provider that received ``complete_video`` is ``NativeVideoVisionProvider``;
* the loopback server received **exactly one** request carrying a base64 MP4
  data URI, with no audio;
* a ``method=video_native`` current observation row is persisted, with a temporal
  event mapped to **source-absolute** seconds;
* the response's clip/segment identity came from the real provider, not a stub.

Reverse verification (task book §6, R08) is performed separately by temporarily
restoring ``get_provider`` at the call site; see the commit message and
``docs/RELEASE_M5_3_R1.md`` for the recorded RED output.
"""

from __future__ import annotations

import json
import struct
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, Segment, SegmentVideoObservation
from app.services.vision import video_job as vj
from app.services.vision.video_capabilities import (
    VERIFIED_NATIVE_VIDEO,
    VideoCapabilities,
    record_verification,
)
from tests._media import needs_ffmpeg, require_ffmpeg

# ---------------------------------------------------------------------------
# a real loopback "model server"
# ---------------------------------------------------------------------------


class _VideoServer(BaseHTTPRequestHandler):
    """A real HTTP server that answers like a native-video chat endpoint.

    It records the raw body so the test can prove a *video* part was sent (and
    not, say, four sampled frames), and it echoes the clip id it parsed out of the
    prompt so the response is tied to the real request rather than a fixed string.
    """

    seen: list[dict] = []
    responder = None  # optional override: (status, payload_dict)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).seen.append(
            {
                "path": self.path,
                "raw": raw,
                "auth": self.headers.get("Authorization", ""),
                "content_type": self.headers.get("Content-Type", ""),
            }
        )

        if type(self).responder is not None:
            status, payload = type(self).responder
            body = json.dumps(payload).encode()
        else:
            clip_id, segment_id = _extract_ids(raw)
            content = json.dumps(
                {
                    "clip_id": clip_id,
                    "segment_id": segment_id,
                    "summary": "镜头内画面在 0.5s 处发生一次可见切换。",
                    "observed_temporal_quality": "clear",
                    "audio_analyzed": False,
                    "limitations": [],
                    "events": [
                        {
                            "event_type": "scene_change",
                            "start_rel_sec": 0.5,
                            "end_rel_sec": 1.5,
                            "description": "画面内容在 0.5 秒处发生切换。",
                            "confidence": 0.9,
                            "evidence": ["video_window"],
                        }
                    ],
                }
            )
            status = 200
            body = json.dumps(
                {
                    "choices": [{"message": {"content": content}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22},
                }
            ).encode()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def _extract_ids(raw: bytes) -> tuple[str, str]:
    """Pull the clip id and segment id the production prompt embeds.

    The prompt contains both ``clip_id=<uuid>`` and ``segment_id=<uuid>``, so this
    matches each key specifically rather than searching for the bare word --
    matching the bare word picks up ``segment_id`` for ``clip_id`` and makes the
    provider's own ``VIDEO_CLIP_MISMATCH`` check fire, which would look like a
    product bug when it is really a test-harness bug.

    Echoing both back is also what a well-behaved model does, and the validator
    binds the response to the call on both fields (C3).
    """
    import re

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - a malformed body fails the assertions instead
        return "unknown", "unknown"
    text = json.dumps(payload)
    # Strip JSON escaping so ``clip_id=`` appears literally, as in the prompt line.
    text = text.replace("\\n", "\n").replace('\\"', '"')
    clip = re.search(r"(?<![A-Za-z_])clip_id=([0-9a-fA-F-]{8,64})", text)
    segment = re.search(r"(?<![A-Za-z_])segment_id=([0-9a-fA-F-]{8,64})", text)
    return (
        clip.group(1) if clip else "unknown",
        segment.group(1) if segment else "unknown",
    )


@pytest.fixture()
def video_server():
    server = HTTPServer(("127.0.0.1", 0), _VideoServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _VideoServer.seen = []
    _VideoServer.responder = None
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# a real source clip and a real DB row set
# ---------------------------------------------------------------------------


def _make_source(path: Path, *, duration: float = 6.0) -> None:
    """A real MP4 via lavfi testsrc.

    Deliberately **no** ``drawtext``: that filter needs libfreetype and is absent
    from Homebrew builds, and the mandatory-media job must not depend on a filter
    the product itself never uses.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture()
def job_env(client, tmp_path, monkeypatch, video_server):
    """Project + asset + segment + running job, wired to the loopback server.

    Settings are pointed at the loopback server and the model switch is enabled
    **for this test only** (TSB-333 discipline), so the real ``complete_video``
    path is exercised without touching any production default.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(
        settings, "model_base_url", f"http://127.0.0.1:{video_server.server_port}",
        raising=False,
    )
    monkeypatch.setattr(settings, "model_api_key", "test-key-1234567890", raising=False)
    monkeypatch.setattr(settings, "model_name", "GLM-test-native-video", raising=False)
    monkeypatch.setattr(settings, "model_allow_insecure_http", True, raising=False)
    monkeypatch.setattr(settings, "model_max_request_bytes", 8_000_000, raising=False)
    monkeypatch.setattr(settings, "model_max_retries", 0, raising=False)

    res = client.post("/api/projects", json={"name": "e2e-video", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    source = tmp_path / "source.mp4"
    _make_source(source)

    asset_id, segment_id, job_id = (str(uuid.uuid4()) for _ in range(3))
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id, project_id=project_id, source_path=str(source),
                display_name="source.mp4", scan_status="succeeded",
                file_size_bytes=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns,
            )
        )
        s.flush()
        s.add(
            Segment(
                id=segment_id, project_id=project_id, asset_id=asset_id,
                detected_start_sec=1.0, detected_end_sec=4.0,
                start_sec=1.0, end_sec=4.0, boundary_reason="hard_cut",
                user_rating="pending",
            )
        )
        s.add(
            Job(
                id=job_id, project_id=project_id, job_type="video_analyze",
                status="running", config_snapshot={}, stats={},
                failures={"count": 0, "items": []},
            )
        )

    return {
        "project_id": project_id,
        "asset_id": asset_id,
        "segment_id": segment_id,
        "job_id": job_id,
        "source": source,
    }


def _verified_caps() -> VideoCapabilities:
    return record_verification(
        state=VERIFIED_NATIVE_VIDEO,
        protocol="video_url_data_uri",
        model_id="GLM-test-native-video",
        verification_scope="tsb-334 loopback integration",
    )


def _plan(env, caps, *, force: bool = True):
    return vj.VideoJobPlan(
        requested_mode="video_native",
        effective_mode="video_native",
        fallback_reason="",
        limit=10,
        segment_ids=[],
        force=force,
        upload_acknowledged=True,
        max_clip_sec=15.0,
        capabilities=caps,
    )


# ---------------------------------------------------------------------------
# R07 — the production call site must reach the real video provider
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_r07_the_job_uses_the_native_video_provider(job_env, monkeypatch):
    require_ffmpeg()
    import app.services.vision.video_job as module

    caps = _verified_caps()
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, caps))

    # Record which class actually receives complete_video, without replacing it:
    # the real method still runs, so the HTTP request is real too.
    from app.services.vision.providers.native_video import NativeVideoVisionProvider

    used: list[type] = []
    real_complete = NativeVideoVisionProvider.complete_video

    def _spy(self, request, **kwargs):
        used.append(type(self))
        return real_complete(self, request, **kwargs)

    monkeypatch.setattr(NativeVideoVisionProvider, "complete_video", _spy)

    module.video_analyze_task(job_env["job_id"])

    assert used == [NativeVideoVisionProvider], (
        "the production call site must instantiate NativeVideoVisionProvider; "
        f"got {used!r}"
    )


@needs_ffmpeg
def test_r07b_exactly_one_silent_video_is_sent(job_env, monkeypatch):
    """One clip, sent once, as a video data URI carrying no audio track."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    caps = _verified_caps()
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, caps))
    module.video_analyze_task(job_env["job_id"])

    assert len(_VideoServer.seen) == 1, f"expected exactly 1 request, saw {len(_VideoServer.seen)}"
    raw = _VideoServer.seen[0]["raw"]
    body = raw.decode("utf-8", errors="replace")

    # a genuine inline video part, not sampled image frames
    assert "data:video/mp4;base64," in body
    assert "image_url" not in body, "video job must not send image parts"

    # the mp4 magic bytes must survive base64 round-trip
    import base64

    marker = "data:video/mp4;base64,"
    start = body.index(marker) + len(marker)
    end = start
    while end < len(body) and body[end] in (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    ):
        end += 1
    decoded = base64.b64decode(body[start:end])
    assert struct.unpack(">I", decoded[:4])[0] + 8 >= 16
    assert decoded[4:8] == b"ftyp", "payload must be a real ISO-BMFF file"


@needs_ffmpeg
def test_r10_a_temporal_event_is_persisted_at_source_absolute_seconds(job_env, monkeypatch):
    """The event's relative seconds become source-absolute seconds on the segment."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    caps = _verified_caps()
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, caps))
    module.video_analyze_task(job_env["job_id"])

    with session_scope() as s:
        rows = [
            r
            for r in s.scalars(select(SegmentVideoObservation)).all()
            if r.segment_id == job_env["segment_id"]
        ]

    assert rows, "a verified run must persist an observation for the analysed segment"
    current = [r for r in rows if r.is_current]
    assert len(current) == 1, "exactly one current observation"
    row = current[0]
    assert row.method == "video_native"
    assert row.segment_id == job_env["segment_id"]

    events = row.events_json or []
    assert events, "the parsed temporal event must be stored"
    ev = events[0]

    # The clip starts at the segment start (1.0s); rel 0.5 -> absolute 1.5s.
    start_abs = ev.get("start_abs_sec")
    assert start_abs is not None, f"event lacks an absolute start: {ev!r}"
    assert 1.4 <= float(start_abs) <= 1.6, f"expected ~1.5s absolute, got {start_abs}"
    assert 1.0 <= float(start_abs) <= 4.0, "event must lie inside the segment"

    # Relative seconds are kept too, so the mapping is auditable rather than inferred.
    assert 0.4 <= float(ev.get("start_rel_sec", -1)) <= 0.6
    assert ev.get("event_type") == "scene_change"
    # The clip is silent, so a claim to have analysed audio must be forced false.
    assert row.audio_analyzed is False


@needs_ffmpeg
def test_r07c_the_source_file_is_never_modified(job_env, monkeypatch):
    """C1: the run derives a clip but must not touch the original."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    source = job_env["source"]
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    digest_before = __import__("hashlib").sha256(source.read_bytes()).hexdigest()

    caps = _verified_caps()
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, caps))
    module.video_analyze_task(job_env["job_id"])

    after = (source.stat().st_size, source.stat().st_mtime_ns)
    digest_after = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
    assert before == after, "size/mtime must not change"
    assert digest_before == digest_after, "content must not change"


@needs_ffmpeg
def test_r09_a_plaintext_non_loopback_endpoint_is_refused_before_sending(
    job_env, monkeypatch
):
    """An insecure remote endpoint must be refused, and nothing may reach the wire."""
    require_ffmpeg()
    import app.services.vision.video_job as module
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_allow_insecure_http", False, raising=False)
    monkeypatch.setattr(settings, "model_base_url", "http://example.invalid", raising=False)

    caps = _verified_caps()
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, caps))

    before = len(_VideoServer.seen)
    module.video_analyze_task(job_env["job_id"])

    assert len(_VideoServer.seen) == before, "refusal must happen before any send"
    with session_scope() as s:
        rows = [
            r
            for r in s.scalars(select(SegmentVideoObservation)).all()
            if r.segment_id == job_env["segment_id"]
        ]
    assert [r for r in rows if r.is_current] == [], (
        "a refused upload must not publish an observation"
    )
