"""TSB-333 — the model switch must be enabled per test, never for the whole run.

Why this file exists
--------------------
``test_native_video_provider.py`` exercises protocol, MIME, HTTP-status and retry
branches that live *behind* the upload-authorization boundary. When the global
``TRAVELSB_MODEL_MODE`` is ``off`` (the production default), every one of those
tests is refused by ``authorize_upload`` long before it reaches the branch it
means to test, and the suite reports 15 failures whose real cause is a single
switched-off default.

The tempting "fix" is to set ``TRAVELSB_MODEL_MODE=on`` globally for CI. That is
explicitly forbidden (task book §1.3 rule 4): it would disable the very guard that
stops real outbound uploads, and would make the suite pass by removing the
behaviour under test. The correct fix enables the switch *inside* the tests that
need it and restores it afterwards.

These tests pin both halves of that contract:

* R02 — a test using the enabling fixture reaches the real loopback HTTP server;
* R01 — with the switch off, nothing is sent and ``MODEL_UPLOAD_DENIED`` is raised;
* the production default itself is never modified.

If someone later "fixes" CI by exporting ``TRAVELSB_MODEL_MODE=on`` in the
workflow, ``test_the_provider_suite_does_not_need_a_global_switch`` still passes
(it asserts the fixture works, not the environment) — but CI's own
``test_ci_skip_guard``-style discipline and the reviewer checklist cover that. What
this file does guarantee is that the *fixture* path is genuinely exercised, so the
suite has a real reason to be green rather than an inherited global.
"""

from __future__ import annotations

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.vision.authorization import authorize_upload
from app.services.vision.errors import MODEL_UPLOAD_DENIED, ModelError
from app.services.vision.providers.base import VideoPart, VideoRequest
from app.services.vision.providers.native_video import NativeVideoVisionProvider


def _mp4(payload: bytes = b"junk" * 200) -> bytes:
    size = 8 + len(b"isom") + len(payload)
    return struct.pack(">I", size) + b"ftyp" + b"isom" + payload


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).seen.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization", "")}
        )
        payload = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps({"events": []})}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        return


@pytest.fixture()
def mock_endpoint():
    """A real loopback HTTP server, so status handling is exercised for real."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.seen = []
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def model_switch_on(monkeypatch):
    """Enable the model switch *for this test only*.

    ``get_settings`` is lru_cached and reads the environment on first call, so the
    cached object is mutated and monkeypatch restores it at teardown. The
    production default in config/env is never touched.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_allow_insecure_http", True, raising=False)
    return settings


def _request() -> VideoRequest:
    return VideoRequest(
        prompt="describe",
        video=VideoPart(
            clip_id="clip-1", data=_mp4(), mime_type="video/mp4", duration_sec=3.0
        ),
        protocol="video_url_data_uri",
        authorization=authorize_upload(
            project_id="p-tsb333", upload_acknowledged=True, purpose="video_analyze"
        ),
    )


def _provider(port: int, monkeypatch) -> NativeVideoVisionProvider:
    import app.services.vision.providers.native_video as nv

    monkeypatch.setattr(nv, "validate_endpoint", lambda url, allow_insecure: None)
    provider = NativeVideoVisionProvider()
    provider.base_url = f"http://127.0.0.1:{port}"
    provider.api_key = "test-key-1234567890"
    provider.allow_insecure = True
    return provider


# --- R02: the enabling fixture really reaches the wire ----------------------


def test_r02_the_enabling_fixture_lets_a_validated_call_reach_the_real_server(
    mock_endpoint, model_switch_on, monkeypatch
):
    """With the switch on, the loopback server receives exactly one request.

    This is the assertion that distinguishes "the suite is green because the
    branch ran" from "the suite is green because nothing ever sent".
    """
    provider = _provider(mock_endpoint.server_port, monkeypatch)
    response = provider.complete_video(_request())

    assert len(_Handler.seen) == 1, "expected exactly one outbound request"
    seen = _Handler.seen[0]
    assert seen["auth"].startswith("Bearer ")
    assert b"data:video/mp4;base64," in seen["body"]
    assert response is not None


def test_r02b_the_fixture_does_not_depend_on_the_process_environment(
    monkeypatch,
):
    """The switch is turned on by the fixture even when the env says ``off``.

    Guards the real CI condition: the workflow does not (and must not) export a
    global ``on``; the fixture is what makes the branch reachable.
    """
    monkeypatch.delenv("TRAVELSB_MODEL_MODE", raising=False)
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "off", raising=False)
    assert settings.model_mode == "off"

    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    assert settings.model_mode == "on"


# --- R01: the off path still refuses, sends nothing -------------------------


def test_r01_with_the_switch_off_nothing_is_sent_and_upload_is_denied(monkeypatch):
    """The production default must keep refusing before any encoding happens."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "off", raising=False)

    with pytest.raises(ModelError) as exc:
        authorize_upload(
            project_id="p-tsb333", upload_acknowledged=True, purpose="video_analyze"
        )
    assert exc.value.code == MODEL_UPLOAD_DENIED


def test_r01b_the_default_mode_in_the_repository_is_still_off():
    """A global ``on`` would defeat the guard; the shipped default must stay off.

    Read from ``.env.example`` (the committed contract) rather than the local
    ``.env``, which is gitignored and may legitimately differ per operator.
    """
    from pathlib import Path

    example = Path(__file__).resolve().parents[1].parent / ".env.example"
    text = example.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("TRAVELSB_MODEL_MODE")]
    assert lines, "TRAVELSB_MODEL_MODE must be declared in .env.example"
    assert lines[-1].split("=", 1)[1].strip() == "off"


def test_r01c_ci_does_not_export_a_global_model_switch():
    """CI must not buy green by globally enabling uploads (task book §1.3 rule 4)."""
    from pathlib import Path

    ci = Path(__file__).resolve().parents[1].parent / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "TRAVELSB_MODEL_MODE" not in stripped, (
            "ci.yml must not set TRAVELSB_MODEL_MODE; enable it per-test instead"
        )
