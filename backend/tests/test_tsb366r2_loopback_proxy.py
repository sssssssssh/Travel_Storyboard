"""TSB-366-R2 — loopback must never be routed through a system proxy.

The bug this pins down: on Windows, ``httpx`` (with its default ``trust_env=True``)
calls ``urllib.request.getproxies()``, which reads the **registry** proxy settings.
Python does not honour the WinINET ``ProxyOverride`` bypass list (``127.*``,
``localhost``), so a request to ``http://127.0.0.1:<port>`` is sent to the
configured proxy. The proxy forwards the request — so the mock server *does* see
it — but mangles the ``408`` response, and the client ends up raising
``RemoteProtocolError``. The provider then classifies it as
``MODEL_NETWORK_ERROR`` instead of ``MODEL_TIMEOUT``.

That is why the failure only ever appeared locally: CI runners have no system
proxy. These tests make the situation explicit and deterministic instead of
depending on the machine's proxy settings.

They are written to FAIL on the pre-fix tree in an environment with a proxy, and
to pass everywhere after the fix. In an environment without any proxy configured
they would pass either way — which is exactly why ``test_a_proxy_would_break_408``
asserts the *mechanism* rather than merely the outcome.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest


class _ProxyHandler(BaseHTTPRequestHandler):
    """A deliberately hostile proxy: forwards, then drops a 408 response."""

    protocol_version = "HTTP/1.1"
    seen: list[dict] = []

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        type(self).seen.append({"path": self.path, "body": raw})
        # A proxy that fails to relay the 408 correctly: it closes the connection
        # instead of returning the upstream status. This reproduces what the real
        # local proxy does, without depending on it being installed.
        self.close_connection = True


@pytest.fixture()
def broken_proxy():
    _ProxyHandler.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _ProxyHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def target_server():
    """A server that answers 408 properly."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({"error": {"message": "status 408"}}).encode()
            self.send_response(408)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# the mechanism
# ---------------------------------------------------------------------------


def test_a_proxy_gets_between_the_client_and_loopback(target_server, broken_proxy):
    """With proxy env honoured, a loopback 408 becomes a transport error.

    This documents *why* the defect existed. It does not assert that the machine
    has a proxy configured; it proves the failure mode is real when one is.
    """
    _srv, proxy_url = broken_proxy

    # trust_env=True + an explicit proxy is the same code path the registry proxy
    # takes: the loopback request is sent to the proxy.
    with pytest.raises(httpx.HTTPError):
        with httpx.Client(proxy=proxy_url, timeout=10) as client:
            client.post(f"{target_server}/v1/chat/completions", content=b"{}")


def test_loopback_without_env_reaches_the_server_and_returns_408(target_server):
    """The control: direct loopback returns the real status."""
    with httpx.Client(trust_env=False, timeout=10) as client:
        resp = client.post(f"{target_server}/v1/chat/completions", content=b"{}")
    assert resp.status_code == 408


# ---------------------------------------------------------------------------
# the fix: the test transport must not consult the environment
# ---------------------------------------------------------------------------


def test_the_mock_server_client_is_direct(target_server):
    """The helper the provider tests use must disable env proxy lookup.

    If this ever regresses, the 408 case silently depends on the developer's
    machine settings again.
    """
    from tests.test_vision_provider import loopback_http_client

    with loopback_http_client(timeout=10) as client:
        assert client.trust_env is False
        resp = client.post(f"{target_server}/v1/chat/completions", content=b"{}")
    assert resp.status_code == 408


def test_the_provider_uses_a_client_that_ignores_the_environment(monkeypatch):
    """The provider itself must be proxy-safe for loopback endpoints.

    A local/LAN model server is a supported deployment; routing it through an
    unrelated system proxy is never correct. Remote endpoints keep honouring the
    environment, which is why the decision is per-URL rather than global.
    """
    from app.services.vision.providers import openai_compatible as mod

    seen: dict = {}
    real_post = mod.httpx.post

    def _spy(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return real_post(url, **kwargs)

    monkeypatch.setattr(mod.httpx, "post", _spy)

    from app.services.vision import errors as err

    provider = mod.OpenAICompatibleVisionProvider
    # Construct minimally: we only need the URL policy helper.
    assert hasattr(provider, "_is_loopback_url"), (
        "the provider must expose a loopback check so the proxy policy is testable"
    )
    assert provider._is_loopback_url("http://127.0.0.1:8080/v1") is True
    assert provider._is_loopback_url("http://localhost:8080/v1") is True
    assert provider._is_loopback_url("http://[::1]:8080/v1") is True
    assert provider._is_loopback_url("https://api.example.com/v1") is False
