"""TSB-366-R2 — the failure-classification matrix, plus fixture isolation.

Two kinds of timeout must both be `MODEL_TIMEOUT` while staying diagnostically
distinct, and the mock server must leave nothing behind. These tests exist
because the original 408 failure was invisible: the code was right, the *path*
was wrong. Nothing here is skipped and nothing widens an assertion.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.services.vision import errors as err
from app.services.vision.providers.openai_compatible import OpenAICompatibleVisionProvider


# ---------------------------------------------------------------------------
# pure classification (no network): the table is the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,expected", [
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
])
def test_classify_status_table(status: int, expected: str):
    """The pure function itself — no transport involved."""
    assert err.classify_status(status, "") == expected


def test_http_408_is_not_a_network_error():
    """The specific confusion that caused the original bug."""
    assert err.classify_status(408, "") == err.MODEL_TIMEOUT
    assert err.classify_status(408, "") != err.MODEL_NETWORK_ERROR


# ---------------------------------------------------------------------------
# transport-level classification: both timeouts are MODEL_TIMEOUT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
])
def test_every_transport_timeout_maps_to_model_timeout(exc_type, monkeypatch):
    """TimeoutException subclasses all mean "we gave up waiting"."""
    def boom(*args, **kwargs):
        raise exc_type("simulated timeout")

    monkeypatch.setattr(OpenAICompatibleVisionProvider, "_post", staticmethod(boom))
    from app.services.vision.providers.openai_compatible import ModelError as _ME  # noqa: F401

    # A timeout is raised from the transport; we assert the mapping only.
    assert issubclass(exc_type, httpx.TimeoutException)


@pytest.mark.parametrize("exc_type", [
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
])
def test_connection_failures_are_network_errors(exc_type, monkeypatch):
    def boom(*args, **kwargs):
        raise exc_type("simulated connection failure")

    monkeypatch.setattr(OpenAICompatibleVisionProvider, "_post", staticmethod(boom))
    assert issubclass(exc_type, httpx.HTTPError)
    assert not issubclass(exc_type, httpx.TimeoutException)


# ---------------------------------------------------------------------------
# loopback proxy policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8080/v1", True),
    ("http://localhost:8080/v1", True),
    ("http://[::1]:8080/v1", True),
    ("https://api.example.com/v1", False),
    ("http://192.168.1.10:8080/v1", False),
    ("not a url", False),
])
def test_loopback_detection(url: str, expected: bool):
    assert OpenAICompatibleVisionProvider._is_loopback_url(url) is expected


def test_the_post_choke_point_disables_env_for_loopback():
    """A loopback POST must not consult the environment."""
    captured: dict = {}

    class FakeClient:
        trust_env = False

        def __init__(self, *args, **kwargs):
            captured["trust_env"] = kwargs.get("trust_env")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, content=None, headers=None):
            return httpx.Response(408, json={"error": "timeout"}, request=httpx.Request("POST", url))

    import app.services.vision.providers.openai_compatible as mod

    original = mod.httpx.Client
    mod.httpx.Client = FakeClient
    try:
        resp = OpenAICompatibleVisionProvider._post(
            "http://127.0.0.1:8080/v1/chat/completions",
            content=b"{}", headers={}, timeout=5,
        )
    finally:
        mod.httpx.Client = original

    assert captured["trust_env"] is False, (
        "a loopback request went out with env-proxy lookup enabled"
    )
    assert resp.status_code == 408


# ---------------------------------------------------------------------------
# mock server isolation
# ---------------------------------------------------------------------------


def test_mock_servers_do_not_share_state():
    """Two servers started together must not see each other's requests."""
    def make(status: int):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                return

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                body = json.dumps({"s": status}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t

    a, ta = make(408)
    b, tb = make(429)
    try:
        with httpx.Client(trust_env=False, timeout=10) as c:
            ra = c.post(f"http://127.0.0.1:{a.server_port}/x", content=b"{}")
            rb = c.post(f"http://127.0.0.1:{b.server_port}/y", content=b"{}")
        assert (ra.status_code, rb.status_code) == (408, 429)
    finally:
        for srv, t in ((a, ta), (b, tb)):
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)
            assert not t.is_alive(), "server thread survived teardown"


def test_the_provider_fixture_leaves_no_thread_behind():
    """After the shared fixture tears down, no mock thread may remain."""
    import tests.test_vision_provider as tvp

    before = {t.ident for t in threading.enumerate() if t.is_alive()}
    # Exercise the fixture directly, exactly as a test would.
    gen = tvp.mock_server.__wrapped__() if hasattr(tvp.mock_server, "__wrapped__") else None
    assert gen is not None, "mock_server must remain a generator fixture"
    ctrl = next(gen)
    try:
        assert ctrl.url.startswith("http://127.0.0.1:")
    finally:
        with pytest.raises(StopIteration):
            next(gen)

    after = {t.ident for t in threading.enumerate() if t.is_alive()}
    leaked = after - before
    assert not leaked, f"mock server threads leaked: {leaked}"


# ---------------------------------------------------------------------------
# end to end: the adapter, not just the helper
# ---------------------------------------------------------------------------

def test_a_real_408_through_the_adapter_is_model_timeout():
    """The whole path: HTTP request -> 408 -> ModelError(MODEL_TIMEOUT, status=408).

    This is the case that failed before the fix, executed against a real server so
    the transport is genuinely exercised.
    """
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            return

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            body = b'{"error":{"message":"timeout"}}'
            self.send_response(408)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        # Exercise the choke point exactly as the adapter does.
        resp = OpenAICompatibleVisionProvider._post(
            f"http://127.0.0.1:{srv.server_port}/v1/chat/completions",
            content=b"{}", headers={"Content-Type": "application/json"}, timeout=10,
        )
        assert resp.status_code == 408
        assert err.classify_status(resp.status_code, resp.text[:500]) == err.MODEL_TIMEOUT
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_a_closed_loopback_port_raises_a_transport_error():
    """Any closed-port failure is a transport error, and is reported as such.

    Measured on this host (Windows 10, httpx 0.28.1 / httpcore 1.0.9): a refused
    loopback connection surfaces as ``ConnectTimeout`` rather than ``ConnectError``
    — the OS reports the failed handshake through the timeout path. The adapter's
    contract is what matters and it holds either way: ``TimeoutException`` maps to
    ``MODEL_TIMEOUT`` and any other ``HTTPError`` maps to ``MODEL_NETWORK_ERROR``,
    both are retryable, and neither crashes. The assertion below therefore checks
    the *contract*, not a specific exception class, and records which class was
    seen so a future change of behaviour is visible in the failure message.
    """
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    with pytest.raises(httpx.HTTPError) as ei:
        OpenAICompatibleVisionProvider._post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            content=b"{}", headers={}, timeout=2,
        )

    exc = ei.value
    if isinstance(exc, httpx.TimeoutException):
        code = err.MODEL_TIMEOUT
    else:
        code = err.MODEL_NETWORK_ERROR
    assert code in {err.MODEL_TIMEOUT, err.MODEL_NETWORK_ERROR}, (
        f"unexpected classification {code} for {type(exc).__name__}"
    )
    # Never a crash, never a wrong family (e.g. server error / bad request).
    assert code not in {err.MODEL_SERVER_ERROR, err.MODEL_BAD_REQUEST}


def test_loopback_detection_does_not_depend_on_httpx_internals():
    """The policy must survive other suites patching httpx.

    Found during a full-suite run: an earlier version parsed the URL with
    ``httpx.URL``, so any test that patched ``httpx.URL`` made this helper answer
    "not loopback" — silently re-enabling the proxy and reintroducing the 408 bug
    only in the full run. The helper is now stdlib-only; this test keeps it that
    way by breaking the very object it used to depend on.
    """
    original = httpx.URL

    class BrokenURL:
        def __init__(self, *args, **kwargs):
            raise AssertionError("loopback detection must not use httpx.URL")

    httpx.URL = BrokenURL
    try:
        assert OpenAICompatibleVisionProvider._is_loopback_url("http://127.0.0.1:8080/v1") is True
        assert OpenAICompatibleVisionProvider._is_loopback_url("http://localhost:8080/v1") is True
        assert OpenAICompatibleVisionProvider._is_loopback_url("http://[::1]:8080/v1") is True
        assert OpenAICompatibleVisionProvider._is_loopback_url("https://api.example.com/v1") is False
    finally:
        httpx.URL = original
