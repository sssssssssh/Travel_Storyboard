"""TSB-326 — native-video provider: protocol, guards, and zero-outbound refusals.

The most important tests here are the ones that assert **``httpx`` was never
called**. A provider that sends first and validates later looks identical to a
correct one on the happy path, and only differs in the case that matters: when it
should not have sent at all. So refusals are asserted with a mock transport that
fails the test if it is ever reached, rather than by checking the raised error.

The mock server is real HTTP against loopback, matching ``test_vision_provider.py``,
so status-code handling is exercised for real instead of stubbed at the call level.
"""

from __future__ import annotations

import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.vision import video_capabilities as vc
from app.services.vision.authorization import authorize_upload
from app.services.vision.errors import MODEL_BAD_REQUEST, MODEL_CAPABILITY_UNSUPPORTED, ModelError
from app.services.vision.providers.base import VideoPart, VideoRequest
from app.services.vision.providers.native_video import (
    NativeVideoVisionProvider,
    capability_gate,
    sniff_video_mime,
    validate_video_payload,
)


def mp4_bytes(payload: bytes = b"junk" * 200) -> bytes:
    """A minimal well-formed ISO-BMFF header, then arbitrary body bytes."""
    size = 8 + len(b"isom") + len(payload)
    return struct.pack(">I", size) + b"ftyp" + b"isom" + payload


# --------------------------------------------------------------------------
# no-outbound guards
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_model_switch_for_this_module(monkeypatch):
    """TSB-333: turn the model switch on for the tests in *this* module only.

    Every test below exercises a branch that sits **behind** the upload
    authorization boundary: protocol registration, MIME sniffing, payload size,
    HTTP status classification, retries, redaction. With the production default
    ``TRAVELSB_MODEL_MODE=off``, ``authorize_upload`` refuses before any of those
    branches is reached, and the suite reported 15 failures whose only real cause
    was the switched-off default.

    Enabling it here — not in ``ci.yml``, not in ``.env`` — keeps the guard intact
    for the rest of the suite while letting these specific tests reach the code
    they describe. ``monkeypatch`` restores the previous value at teardown.

    ``test_video_model_switch_scope.py`` pins this contract, including a check that
    ``ci.yml`` never exports a global ``on``. The off-path refusal itself is still
    covered by ``test_missing_authorization_sends_nothing`` and R01.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_allow_insecure_http", True, raising=False)


@pytest.fixture()
def no_network(monkeypatch):
    """Make any real HTTP attempt fail loudly.

    Patching ``httpx.post`` rather than the network stack means a refusal that
    happens *before* the call passes trivially, while any accidental send raises
    immediately and names itself.
    """
    calls: list[dict] = []

    def _explode(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("httpx.post was called but this path must not send")

    import httpx

    monkeypatch.setattr(httpx, "post", _explode)
    return calls


def _authorization():
    return authorize_upload(
        project_id="p-video", upload_acknowledged=True, purpose="video_analyze"
    )


def _request(**kwargs) -> VideoRequest:
    base = {
        "prompt": "describe",
        "video": VideoPart(
            clip_id="clip-1", data=mp4_bytes(), mime_type="video/mp4", duration_sec=3.0
        ),
        "protocol": "video_url_data_uri",
        "authorization": _authorization(),
    }
    base.update(kwargs)
    return VideoRequest(**base)


def test_missing_authorization_sends_nothing(no_network, monkeypatch):
    monkeypatch.setattr(NativeVideoVisionProvider, "__init__", lambda self: None)
    provider = NativeVideoVisionProvider()
    provider.base_url = "https://example.invalid"
    provider.api_key = "k"
    provider.allow_insecure = False
    provider.max_bytes = 10_000_000
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request(authorization=None))
    assert exc.value.code == "MODEL_UPLOAD_DENIED"
    assert no_network == []


def test_an_unregistered_protocol_sends_nothing(no_network, monkeypatch):
    _init_provider(monkeypatch)
    with pytest.raises(ModelError) as exc:
        _provider().complete_video(_request(protocol="imaginary_protocol"))
    assert exc.value.code == MODEL_CAPABILITY_UNSUPPORTED
    assert no_network == []


def test_an_empty_protocol_sends_nothing(no_network, monkeypatch):
    _init_provider(monkeypatch)
    with pytest.raises(ModelError) as exc:
        _provider().complete_video(_request(protocol=""))
    assert exc.value.code == MODEL_CAPABILITY_UNSUPPORTED
    assert no_network == []


def test_a_non_video_payload_sends_nothing(no_network, monkeypatch):
    """Declaring video/mp4 over HTML must not reach the wire."""
    _init_provider(monkeypatch)
    bad = VideoPart(clip_id="c", data=b"<html>not a video</html>" + b"x" * 64)
    with pytest.raises(ModelError) as exc:
        _provider().complete_video(_request(video=bad))
    assert exc.value.code == MODEL_BAD_REQUEST
    assert no_network == []


def test_an_oversize_payload_sends_nothing(no_network, monkeypatch):
    _init_provider(monkeypatch)
    provider = _provider()
    provider.max_bytes = 500
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request())
    assert exc.value.code == MODEL_BAD_REQUEST
    assert no_network == []


def test_a_fetching_protocol_is_refused_as_an_ssrf_surface(no_network, monkeypatch):
    """A protocol that makes the server fetch a URL must not be used silently."""
    _init_provider(monkeypatch)
    vc.register_protocol(
        vc.VideoProtocol(
            name="server_fetches_url",
            allowed_mimes=frozenset({"video/mp4"}),
            transport="url_reference",
            fetches_remote=True,
        ),
        replace=True,
    )
    try:
        with pytest.raises(ModelError) as exc:
            _provider().complete_video(_request(protocol="server_fetches_url"))
        assert exc.value.code == MODEL_CAPABILITY_UNSUPPORTED
        assert "SSRF" in str(exc.value) or "fetch" in str(exc.value)
        assert no_network == []
    finally:
        vc._reset_registry_for_tests()
        from app.services.vision.video_capabilities import VIDEO_URL_DATA_URI

        vc.register_protocol(VIDEO_URL_DATA_URI, replace=True)


def _init_provider(monkeypatch):
    """Build a provider pointed at loopback with a dummy key and no insecure opt-in."""
    import app.services.vision.providers.native_video as nv

    monkeypatch.setattr(nv, "validate_endpoint", lambda url, allow_insecure: None)


def _provider(port: int = 1) -> NativeVideoVisionProvider:
    provider = NativeVideoVisionProvider()
    provider.base_url = f"http://127.0.0.1:{port}"
    provider.api_key = "test-key"
    provider.allow_insecure = True
    return provider


# --------------------------------------------------------------------------
# payload validation
# --------------------------------------------------------------------------


def test_sniff_accepts_a_well_formed_mp4():
    assert sniff_video_mime(mp4_bytes()) == "video/mp4"


def test_sniff_rejects_a_short_or_non_ftyp_payload():
    assert sniff_video_mime(b"") is None
    assert sniff_video_mime(b"\x00\x00\x00\x20ftyp") is None  # too short
    assert sniff_video_mime(b"\x00\x00\x00\x20junkisom" + b"x" * 20) is None
    assert sniff_video_mime(struct.pack(">I", 0) + b"ftypisom" + b"x" * 20) is None


def test_a_declared_mime_must_match_the_content():
    """A PNG masquerading as MP4 must be caught by content, not by the label."""
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 200
    with pytest.raises(ModelError) as exc:
        validate_video_payload(png, "video/mp4", max_bytes=10_000_000)
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "not a recognised" in str(exc.value)


def test_a_non_mp4_declared_type_is_refused_by_the_allow_list():
    """QuickTime is deliberately not in this build's allow-list (fail closed)."""
    with pytest.raises(ModelError) as exc:
        validate_video_payload(mp4_bytes(), "video/quicktime", max_bytes=10_000_000)
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "unsupported video type" in str(exc.value)


def test_an_unlisted_container_is_refused():
    with pytest.raises(ModelError) as exc:
        validate_video_payload(mp4_bytes(), "video/webm", max_bytes=10_000_000)
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "unsupported video type" in str(exc.value)


def test_an_empty_payload_is_refused():
    with pytest.raises(ModelError):
        validate_video_payload(b"", "video/mp4", max_bytes=1000)


def test_valid_payload_returns_the_sniffed_mime():
    assert validate_video_payload(mp4_bytes(), "video/mp4", max_bytes=10_000_000) == "video/mp4"
    assert validate_video_payload(mp4_bytes(), "", max_bytes=10_000_000) == "video/mp4"


# --------------------------------------------------------------------------
# VideoPart contract
# --------------------------------------------------------------------------


def test_video_part_requires_a_server_issued_clip_id():
    with pytest.raises(ValueError):
        VideoPart(clip_id="", data=mp4_bytes())


def test_video_part_refuses_an_audio_bearing_clip():
    """Derived clips are silent by default; audio must be an explicit decision."""
    with pytest.raises(ValueError):
        VideoPart(clip_id="c", data=mp4_bytes(), has_audio=True)


def test_video_part_maps_source_coordinates():
    part = VideoPart(
        clip_id="c", data=mp4_bytes(), source_offset_sec=12.5, duration_sec=5.0
    )
    assert part.source_end_sec == 17.5
    assert part.size_bytes == len(part.data)


def test_video_part_rejects_negative_times():
    with pytest.raises(ValueError):
        VideoPart(clip_id="c", data=mp4_bytes(), source_offset_sec=-1)


# --------------------------------------------------------------------------
# capability gate
# --------------------------------------------------------------------------


def test_capability_gate_refuses_none_and_unverified():
    with pytest.raises(ModelError) as exc:
        capability_gate(None)
    assert exc.value.code == MODEL_CAPABILITY_UNSUPPORTED
    with pytest.raises(ModelError) as exc:
        capability_gate(vc.unverified_capabilities())
    assert "VIDEO_CAPABILITY_UNVERIFIED" in str(exc.value)


def test_capability_gate_refuses_sampling_unknown():
    """Accepted-but-opaque is not permission to spend a user's footage."""
    caps = vc.VideoCapabilities(
        state=vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN, protocol="video_url_data_uri"
    )
    with pytest.raises(ModelError):
        capability_gate(caps)


def test_capability_gate_allows_a_verified_record():
    caps = vc.record_verification(
        state=vc.VERIFIED_NATIVE_VIDEO,
        protocol="video_url_data_uri",
        model_id="m",
        verification_scope="synthetic clip, temporal order echoed",
    )
    capability_gate(caps)  # must not raise


# --------------------------------------------------------------------------
# real HTTP against a loopback mock
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    responder = None
    seen: list[dict] = []

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": raw}
        )
        status, payload, headers = type(self).responder(raw, self.headers)
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.seen = []
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _ok_body(text: str = '{"events": []}') -> dict:
    return {
        "id": "req-1",
        "model": "mock-video",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_a_successful_call_sends_a_video_url_data_uri(mock_server, monkeypatch):
    _Handler.responder = lambda raw, headers: (200, _ok_body(), None)
    provider = _provider(mock_server.server_address[1])
    resp = provider.complete_video(_request())

    assert resp.clip_id == "clip-1"
    assert resp.sent_bytes == len(mp4_bytes())
    assert resp.usage["total_tokens"] == 15

    sent = json.loads(_Handler.seen[-1]["body"])
    parts = sent["messages"][-1]["content"]
    video_parts = [p for p in parts if p["type"] == "video_url"]
    assert len(video_parts) == 1
    assert video_parts[0]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_the_uploaded_body_carries_no_local_path(mock_server, monkeypatch):
    """C5: the prompt may carry identifiers, never a filesystem path."""
    _Handler.responder = lambda raw, headers: (200, _ok_body(), None)
    provider = _provider(mock_server.server_address[1])
    provider.complete_video(_request())

    blob = _Handler.seen[-1]["body"].decode("utf-8", "replace")
    for forbidden in ("C:\\", "C:/", "/home/", "/Users/", "\\Temp\\"):
        assert forbidden not in blob


def test_an_empty_content_response_is_a_failure_not_a_success(mock_server, monkeypatch):
    """C6: 200 with no usable content must not be recorded as an observation."""
    _Handler.responder = lambda raw, headers: (
        200,
        {"choices": [{"message": {"content": None, "reasoning": "thinking..."},
                      "finish_reason": "length"}]},
        None,
    )
    provider = _provider(mock_server.server_address[1])
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request())
    assert exc.value.code == "MODEL_INVALID_OUTPUT"
    assert "max_tokens" in str(exc.value)


@pytest.mark.parametrize(
    "status,expected",
    [(401, "MODEL_AUTH_FAILED"), (404, "MODEL_NOT_FOUND"), (413, "MODEL_BAD_REQUEST")],
)
def test_error_statuses_are_classified(mock_server, monkeypatch, status, expected):
    _Handler.responder = lambda raw, headers: (status, {"error": "nope"}, None)
    provider = _provider(mock_server.server_address[1])
    provider.max_retries = 0
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request())
    assert exc.value.code == expected


def test_the_api_key_is_never_in_the_error_message(mock_server, monkeypatch):
    secret = "sk-super-secret-value-1234567890"
    _Handler.responder = lambda raw, headers: (400, {"error": f"bad key {secret}"}, None)
    provider = _provider(mock_server.server_address[1])
    provider.api_key = secret
    provider.max_retries = 0
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request())
    assert secret not in str(exc.value)
    assert "REDACTED" in str(exc.value)


def test_a_cancel_before_send_makes_no_request(mock_server, monkeypatch):
    _Handler.responder = lambda raw, headers: (200, _ok_body(), None)
    provider = _provider(mock_server.server_address[1])
    with pytest.raises(ModelError) as exc:
        provider.complete_video(_request(), cancel_check=lambda: True)
    assert exc.value.code == "CANCELLED"
    assert _Handler.seen == [], "a cancelled call must not reach the wire"
    assert "remote_cost_uncertain" in str(exc.value), (
        "the message must not claim the remote cost was avoided"
    )


def test_sampling_disclosure_is_only_reported_when_the_server_says_so(mock_server, monkeypatch):
    _Handler.responder = lambda raw, headers: (200, _ok_body(), None)
    provider = _provider(mock_server.server_address[1])
    resp = provider.complete_video(_request())
    assert resp.sampling_disclosed is False

    body = _ok_body()
    body["video_sampling"] = {"fps": 2}
    _Handler.responder = lambda raw, headers: (200, body, None)
    resp = provider.complete_video(_request())
    assert resp.sampling_disclosed is True


def test_retry_after_is_honoured_without_sleeping_too_long(mock_server, monkeypatch):
    """Bounded retry with an explicit small Retry-After."""
    calls = {"n": 0}

    def responder(raw, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            return (429, {"error": "slow down"}, {"Retry-After": "0.1"})
        return (200, _ok_body(), None)

    _Handler.responder = responder
    provider = _provider(mock_server.server_address[1])
    provider.max_retries = 1
    resp = provider.complete_video(_request())
    assert calls["n"] == 2
    assert resp.attempts == 2
