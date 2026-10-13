"""System status / config endpoints.

Note: there is intentionally no unauthenticated token-issuing endpoint. The
token is written to ``<data_dir>/token.txt`` at startup and surfaced to the
browser by the launcher URL (``/?token=...``); see ``app.security``.
"""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..config import get_settings
from ..ffmpeg import ffmpeg_available, ffprobe_available
from ..schemas import HealthOut, SystemConfigOut

router = APIRouter(tags=["system"])


@router.get("/system/health", response_model=HealthOut)
def health() -> HealthOut:
    ffmpeg = ffmpeg_available()
    ffprobe = ffprobe_available()
    return HealthOut(
        status="ok",
        version=__version__,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_available=ffmpeg is not None and ffprobe is not None,
    )


@router.get("/system/config", response_model=SystemConfigOut)
def system_config() -> SystemConfigOut:
    s = get_settings()
    return SystemConfigOut(
        bind_host=s.bind_host,
        bind_port=s.bind_port,
        data_dir=str(s.data_path),
        max_concurrency=s.max_concurrency,
        thumb_width=s.thumb_width,
        proxy_width=s.proxy_width,
        model_name=s.model_name,
        model_configured=bool(s.model_base_url and s.model_api_key),
    )
