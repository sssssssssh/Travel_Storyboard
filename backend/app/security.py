"""Local access-token handling and loopback request validation.

Threat model: this is a single-user, local-desktop app. The HTTP server binds
to loopback only. Any *local* process can reach a loopback port, so the token is
the authorization boundary — it must never be handed out by an unauthenticated
endpoint.

Bootstrap flow (no unauthenticated token endpoint):
1. The server writes the token to ``<data_dir>/token.txt`` on startup.
2. The launcher (``python run.py`` / the printed banner) opens the browser at
   ``http://127.0.0.1:<port>/?token=<token>``.
3. The SPA reads ``token`` from the query string **once**, keeps it in
   ``sessionStorage``, and strips it from the address bar via
   ``history.replaceState``.

Requests carrying ``?token=`` are still accepted for direct downloads (video/img
tags cannot set headers), but the token is never *disclosed* by the API.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import HTTPException, Request, status

from .config import get_settings

TOKEN_FILENAME = "token.txt"

# Hostnames accepted in the Host header / Origin. Anything else is rejected so a
# malicious page cannot drive the local API through DNS rebinding.
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}

# Host header values allowed to reach /api/*.
TOKEN_HEADER = "X-Access-Token"
TOKEN_QUERY_PARAM = "token"


def _token_path() -> Path:
    return get_settings().data_path / TOKEN_FILENAME


def load_or_create_token() -> str:
    path = _token_path()
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    # Best-effort: restrict to the owner on POSIX. On Windows the data dir
    # already sits under the user profile.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def get_token() -> str:
    return load_or_create_token()


def extract_token(request: Request) -> str | None:
    return request.headers.get(TOKEN_HEADER) or request.query_params.get(TOKEN_QUERY_PARAM)


def token_matches(supplied: str | None) -> bool:
    if not supplied:
        return False
    return secrets.compare_digest(supplied, get_token())


def require_token(request: Request) -> None:
    if not token_matches(extract_token(request)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access token",
        )


def _hostname_of(value: str | None) -> str:
    """Extract the hostname from a Host header or Origin/Referer URL."""
    if not value:
        return ""
    text = value.strip()
    if "://" in text:
        # Origin / Referer: http://127.0.0.1:8737
        text = text.split("://", 1)[1]
    # Strip userinfo, port and path.
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if text.startswith("["):  # IPv6 literal
        return text.split("]", 1)[0] + "]"
    return text.split(":", 1)[0]


def is_loopback_host(value: str | None) -> bool:
    return _hostname_of(value).lower() in ALLOWED_HOSTNAMES


def check_request_origin(request: Request) -> None:
    """Reject requests whose Host/Origin is not loopback (anti DNS-rebinding).

    Raises HTTPException(403) when the request looks cross-site.
    """
    host = request.headers.get("host")
    # HTTP/1.0 clients may omit Host; a loopback bind makes that acceptable.
    if host and not is_loopback_host(host):
        raise HTTPException(status_code=403, detail="Unexpected Host header")

    origin = request.headers.get("origin")
    if origin and not is_loopback_host(origin):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    referer = request.headers.get("referer")
    if referer and not is_loopback_host(referer):
        raise HTTPException(status_code=403, detail="Cross-site request rejected")
