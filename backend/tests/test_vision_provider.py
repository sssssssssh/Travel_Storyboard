"""TSB-302 — vision provider adapter: capabilities, failure classification, retries.

These tests pin the *contract* of the adapter without touching the internet. A
local mock server (``http.server`` in a thread) returns whatever status/body a
case needs, so every branch in the failure table is exercised for real rather
than stubbed at the function-call level: that is the only way to prove the
adapter reads status codes rather than trusting its own optimism.

Coverage mirrors the task book's A03/A04/A06 rows: 401/403, 429 + Retry-After,
5xx, timeout, non-JSON body, malformed shape, empty-but-200, oversize request,
declared-capability mismatch, and cancellation.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from PIL import Image

from app.services.vision import capabilities as caps_mod
from app.services.vision import errors as err
from app.services.vision.providers.base import ImagePart, VisionRequest
from app.services.vision.providers.openai_compatible import (
    OpenAICompatibleVisionProvider,
    validate_endpoint,
)

PNG_MAGIC = b"\x89PNG"

# --------------------------------------------------------------------------
# mock provider server
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    #: Set per-test through the server instance.
    responder = None
    seen: list[dict] = []

    def log_message(self, *args):  # silence the test log
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": raw,
            }
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
    """Start a mock OpenAI-compatible endpoint; yields a control object."""

    class Control:
        url = ""
        calls: list[dict] = []

        def set(self, fn):
            _Handler.responder = fn

        def ok(self, text: str = "{}"):
            self.set(
                lambda raw, h: (
                    200,
                    {
                        "id": "chatcmpl-test",
                        "model": "GLM-test",
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": text}}
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
                    },
                    None,
                )
            )

        def fail(self, status: int, body=None, headers=None):
            payload = body if body is not None else {"error": {"message": f"status {status}"}}
            self.set(lambda raw, h: (status, payload, headers))

    # `_Handler` is shared across tests, so BOTH the responder and the call log
    # must be reset here. Leaving `responder` as None made an unrelated test
    # return a 500 from the handler's own exception, which briefly looked like a
    # product bug.
    _Handler.seen = []
    _Handler.responder = lambda raw, h: (500, {"error": {"message": "no responder set"}}, None)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    ctrl = Control()
    ctrl.url = f"http://127.0.0.1:{srv.server_port}/v1"
    ctrl.calls = _Handler.seen
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield ctrl
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def jpg(tmp_path: Path) -> Path:
    p = tmp_path / "frame.jpg"
    Image.new("RGB", (64, 36), (10, 20, 30)).save(p, format="JPEG")
    return p


def _configure(monkeypatch, url: str, *, insecure: bool = True, **extra):
    """Point settings at the mock server.

    ``get_settings`` is lru_cached and reads the env at first call, so tests
    mutate the cached object directly and restore it afterwards.
    """
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_base_url", url, raising=False)
    monkeypatch.setattr(s, "model_api_key", "test-key-1234567890", raising=False)
    monkeypatch.setattr(s, "model_name", "GLM-test", raising=False)
    monkeypatch.setattr(s, "model_allow_insecure_http", insecure, raising=False)
    monkeypatch.setattr(s, "model_max_retries", extra.pop("retries", 2), raising=False)
    monkeypatch.setattr(s, "model_max_images", extra.pop("max_images", 4), raising=False)
    monkeypatch.setattr(s, "model_max_request_bytes", extra.pop("max_bytes", 8_000_000), raising=False)
    monkeypatch.setattr(s, "model_timeout_sec", extra.pop("timeout", 10.0), raising=False)
    return s


def _provider(url: str) -> OpenAICompatibleVisionProvider:
    return OpenAICompatibleVisionProvider()


def _auth():
    """A per-call authorization token, as a real caller would supply.

    TSB-312 made the provider refuse an unguarded request, so every test that
    expects to reach the network must present one — exactly like production code.
    """
    from app.services.vision.authorization import AuthorizedModelContext

    return AuthorizedModelContext(project_id="p-test", purpose="unit-test")


def _request(jpg: Path, n: int = 1, *, authorization=...) -> VisionRequest:
    return VisionRequest(
        prompt="Describe the image.",
        images=[ImagePart(path=str(jpg), label=f"t={i}s") for i in range(n)],
        authorization=_auth() if authorization is ... else authorization,
    )


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_success_returns_text_and_usage(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url)
    mock_server.ok('{"scene": "lake"}')
    resp = _provider(mock_server.url).complete(_request(jpg))

    assert resp.text == '{"scene": "lake"}'
    assert resp.usage["total_tokens"] == 12
    assert resp.model_id == "GLM-test"
    assert resp.attempts == 1
    assert resp.latency_ms >= 0
    # The wire format must be a data URI, never a filesystem path.
    body = json.loads(mock_server.calls[-1]["body"])
    parts = body["messages"][0]["content"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls and urls[0].startswith("data:image/jpeg;base64,")
    assert str(jpg) not in mock_server.calls[-1]["body"].decode()


def test_the_local_path_never_reaches_the_request(mock_server, jpg, monkeypatch):
    """Privacy: the label is sent, the absolute path is not (C5)."""
    _configure(monkeypatch, mock_server.url)
    mock_server.ok("{}")
    _provider(mock_server.url).complete(_request(jpg, 2))
    sent = mock_server.calls[-1]["body"].decode()
    assert str(jpg) not in sent
    assert "t=0s" in sent and "t=1s" in sent


# --------------------------------------------------------------------------
# TSB-312: the provider refuses an unguarded call
# --------------------------------------------------------------------------


def test_request_without_authorization_is_refused_before_any_network_io(
    mock_server, jpg, monkeypatch
):
    """A direct provider call must not be able to bypass the consent boundary.

    This is the structural guarantee behind C5: permission is a per-call token
    produced by the business entry point, not a process-wide flag, so no future
    refactor can reach the network without an explicit decision.
    """
    _configure(monkeypatch, mock_server.url)
    mock_server.ok("{}")
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg, authorization=None))
    assert ei.value.code == err.MODEL_UPLOAD_DENIED
    assert mock_server.calls == [], "no request may be attempted without a context"


def test_require_context_rejects_missing_and_accepts_present():
    from app.services.vision.authorization import AuthorizedModelContext, require_context

    with pytest.raises(err.ModelError) as ei:
        require_context(None)
    assert ei.value.code == err.MODEL_UPLOAD_DENIED

    ctx = AuthorizedModelContext(project_id="p", purpose="test")
    assert require_context(ctx) is ctx


# --------------------------------------------------------------------------
# failure classification (A03)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, err.MODEL_AUTH_FAILED),
        (403, err.MODEL_AUTH_FAILED),
        (404, err.MODEL_NOT_FOUND),
        (408, err.MODEL_TIMEOUT),
        (429, err.MODEL_RATE_LIMITED),
        (500, err.MODEL_SERVER_ERROR),
        (502, err.MODEL_SERVER_ERROR),
        (503, err.MODEL_SERVER_ERROR),
        (504, err.MODEL_SERVER_ERROR),
        (400, err.MODEL_BAD_REQUEST),
    ],
)
def test_status_classification(mock_server, jpg, monkeypatch, status, expected):
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.fail(status)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == expected
    assert ei.value.status == status


def test_auth_failure_is_not_retried(mock_server, jpg, monkeypatch):
    """401 must fail immediately: retrying a bad key just burns budget."""
    _configure(monkeypatch, mock_server.url, retries=3)
    mock_server.fail(401)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_AUTH_FAILED
    assert ei.value.retryable is False
    assert len(mock_server.calls) == 1


def test_bad_model_id_reported_as_400_is_classified_as_not_found(mock_server, jpg, monkeypatch):
    """Gateways often answer an unknown model with 400 + 'does not exist'."""
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.fail(400, {"error": {"message": "The model `nope` does not exist."}})
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_NOT_FOUND


def test_unsupported_image_400_is_a_capability_error(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.fail(400, {"error": {"message": "image_url is not supported by this model"}})
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_CAPABILITY_UNSUPPORTED


def test_non_json_body_is_invalid_output(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.set(lambda raw, h: (200, b"<html>gateway</html>", None))
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_INVALID_OUTPUT


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "   "}}]},
    ],
)
def test_malformed_success_bodies_are_rejected(mock_server, jpg, monkeypatch, payload):
    """A 200 with nothing usable is a failure, never an empty success (C6)."""
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.set(lambda raw, h: (200, payload, None))
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_INVALID_OUTPUT


def test_reasoning_only_answer_names_the_real_cause(mock_server, jpg, monkeypatch):
    """Seen on the live GLM endpoint: content null, prose in `reasoning`."""
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.set(
        lambda raw, h: (
            200,
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning": "thinking..."},
                    }
                ]
            },
            None,
        )
    )
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_INVALID_OUTPUT
    assert "max_tokens" in ei.value.message


# --------------------------------------------------------------------------
# retries (A04)
# --------------------------------------------------------------------------


def test_transient_500_is_retried_then_succeeds(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, retries=2)
    state = {"n": 0}

    def responder(raw, headers):
        state["n"] += 1
        if state["n"] < 3:
            return (503, {"error": {"message": "try later"}}, None)
        return (
            200,
            {"choices": [{"message": {"content": "recovered"}}]},
            None,
        )

    mock_server.set(responder)
    resp = _provider(mock_server.url).complete(_request(jpg))
    assert resp.text == "recovered"
    assert resp.attempts == 3
    assert len(mock_server.calls) == 3


def test_retry_is_bounded(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, retries=1)
    mock_server.fail(500)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_SERVER_ERROR
    assert len(mock_server.calls) == 2  # 1 initial + 1 retry, then stop


def test_retry_after_is_honoured(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, retries=1)
    state = {"n": 0}

    def responder(raw, headers):
        state["n"] += 1
        if state["n"] == 1:
            return (429, {"error": {"message": "slow down"}}, {"Retry-After": "1"})
        return (200, {"choices": [{"message": {"content": "ok"}}]}, None)

    mock_server.set(responder)
    started = time.monotonic()
    resp = _provider(mock_server.url).complete(_request(jpg))
    elapsed = time.monotonic() - started
    assert resp.text == "ok"
    assert elapsed >= 1.0, "Retry-After: 1 must actually be waited out"


def test_network_error_is_classified(mock_server, jpg, monkeypatch):
    """A transport failure must surface as MODEL_NETWORK_ERROR, not a crash.

    Rather than trusting an unused port to refuse the connection (on this host
    an outbound proxy answered 502 instead), the transport itself is made to
    fail, so the assertion is about the adapter's handling and nothing else.
    """
    import httpx

    _configure(monkeypatch, mock_server.url, retries=0)

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused (simulated)")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_NETWORK_ERROR


def test_timeout_is_classified(mock_server, jpg, monkeypatch):
    import httpx

    _configure(monkeypatch, mock_server.url, retries=0)

    def slow(*args, **kwargs):
        raise httpx.ReadTimeout("too slow (simulated)")

    monkeypatch.setattr(httpx, "post", slow)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_TIMEOUT
    assert ei.value.retryable is True


# --------------------------------------------------------------------------
# capabilities and limits (A06)
# --------------------------------------------------------------------------


def test_declared_no_image_support_is_refused_before_any_request(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url)
    monkeypatch.setattr(
        caps_mod, "declared_capabilities", lambda: caps_mod.VisionCapabilities(supports_images=False)
    )
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_CAPABILITY_UNSUPPORTED
    assert mock_server.calls == [], "must not spend a request on an impossible call"


def test_multi_image_beyond_limit_is_refused(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, max_images=2)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg, 3))
    assert ei.value.code == err.MODEL_CAPABILITY_UNSUPPORTED
    assert mock_server.calls == []


def test_oversize_request_body_is_refused(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url, max_bytes=200)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_BAD_REQUEST
    assert mock_server.calls == []


def test_not_configured_raises_clear_code(mock_server, jpg, monkeypatch):
    from app.config import get_settings

    s = _configure(monkeypatch, mock_server.url)
    monkeypatch.setattr(s, "model_api_key", "", raising=False)
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert ei.value.code == err.MODEL_NOT_CONFIGURED


# --------------------------------------------------------------------------
# endpoint validation (cleartext guard)
# --------------------------------------------------------------------------


def test_cleartext_non_loopback_is_refused_by_default():
    with pytest.raises(err.ModelError) as ei:
        validate_endpoint("http://model.example.com/v1", allow_insecure=False)
    assert ei.value.code == err.MODEL_NOT_CONFIGURED


def test_cleartext_loopback_is_allowed():
    validate_endpoint("http://127.0.0.1:8000/v1", allow_insecure=False)


def test_cleartext_can_be_explicitly_accepted():
    validate_endpoint("http://model.example.com/v1", allow_insecure=True)


def test_https_is_always_allowed():
    validate_endpoint("https://model.example.com/v1", allow_insecure=False)


def test_bad_scheme_is_refused():
    with pytest.raises(err.ModelError):
        validate_endpoint("ftp://model.example.com/v1", allow_insecure=True)


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------


def test_cancel_before_send_makes_no_request(mock_server, jpg, monkeypatch):
    _configure(monkeypatch, mock_server.url)
    mock_server.ok("{}")
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg), cancel_check=lambda: True)
    assert ei.value.code == err.CANCELLED
    assert mock_server.calls == []


def test_cancel_during_backoff_stops_retrying(mock_server, jpg, monkeypatch):
    """A cancel must interrupt the wait, not queue another request behind it."""
    _configure(monkeypatch, mock_server.url, retries=3)
    mock_server.fail(429, headers={"Retry-After": "30"})
    state = {"calls": 0}

    def cancel():
        state["calls"] += 1
        return state["calls"] > 2  # let the first request out, then cancel

    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg), cancel_check=cancel)
    assert ei.value.code == err.CANCELLED
    assert len(mock_server.calls) == 1, "no second request after cancel"


# --------------------------------------------------------------------------
# privacy of logs / errors
# --------------------------------------------------------------------------


def test_error_message_redacts_the_api_key(mock_server, jpg, monkeypatch):
    """Even a provider echoing the key back must not surface it."""
    key = "test-key-1234567890"
    _configure(monkeypatch, mock_server.url, retries=0)
    mock_server.fail(400, {"error": {"message": f"bad auth header Bearer {key}"}})
    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
    assert key not in str(ei.value)
    assert "***REDACTED***" in str(ei.value)


def test_redact_helper_ignores_short_strings():
    assert err.redact("token abc", "abc") == "token abc"
    assert err.redact("token supersecret", "supersecret") == "token ***REDACTED***"
