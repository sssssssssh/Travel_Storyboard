"""System status / config endpoints.

Note: there is intentionally no unauthenticated token-issuing endpoint. The
token is written to ``<data_dir>/token.txt`` at startup and surfaced to the
browser by the launcher URL (``/?token=...``); see ``app.security``.

Both endpoints below are in the *token-exempt* allow-list (so a liveness probe
and the launcher can reach them), therefore they must not disclose anything
about this machine's filesystem or configuration: only boolean availability and
tool names are reported, never absolute paths.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

from .. import __version__
from ..config import get_settings
from ..ffmpeg import ffmpeg_available, ffprobe_available
from ..schemas import HealthOut, SystemConfigOut

router = APIRouter(tags=["system"])


def _tool_name(path: str | None) -> str | None:
    """Basename of a resolved tool path — never the directory it lives in."""
    return os.path.basename(path) if path else None


@router.get("/system/health", response_model=HealthOut)
def health() -> HealthOut:
    ffmpeg = ffmpeg_available()
    ffprobe = ffprobe_available()
    return HealthOut(
        status="ok",
        version=__version__,
        ffmpeg=_tool_name(ffmpeg),
        ffprobe=_tool_name(ffprobe),
        ffmpeg_available=ffmpeg is not None and ffprobe is not None,
    )


@router.get("/system/config", response_model=SystemConfigOut)
def system_config() -> SystemConfigOut:
    s = get_settings()
    return SystemConfigOut(
        bind_host=s.bind_host,
        bind_port=s.bind_port,
        # Presence only: the absolute data directory is not disclosed here.
        data_dir_name=s.data_path.name,
        max_concurrency=s.max_concurrency,
        thumb_width=s.thumb_width,
        proxy_width=s.proxy_width,
        model_name=s.model_name,
        model_configured=bool(s.model_base_url and s.model_api_key),
    )
