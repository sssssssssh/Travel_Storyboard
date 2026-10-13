"""Local access-token handling.

The server binds to 127.0.0.1 only. A random token is generated on first
start and persisted under the data directory; all /api/* requests must carry
it (via X-Access-Token header or ?token= query param).
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import HTTPException, Request, status

from .config import get_settings

TOKEN_FILENAME = "token.txt"


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
    return token


def get_token() -> str:
    return load_or_create_token()


def extract_token(request: Request) -> str | None:
    return request.headers.get("X-Access-Token") or request.query_params.get("token")


def require_token(request: Request) -> None:
    supplied = extract_token(request)
    if not supplied or not secrets.compare_digest(supplied, get_token()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing access token"
        )
