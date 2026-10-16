"""Optional vision-model integration (M5).

Layered deliberately: ``providers`` talks HTTP, ``capabilities`` declares what
may be attempted, this package decides *which* provider to use. The legacy
``app.services.model_client`` remains the compatibility entry point for the old
``direct_analyze`` path and delegates here.
"""

from __future__ import annotations

from .capabilities import VisionCapabilities, declared_capabilities
from .errors import ModelError
from .providers.base import ImagePart, VisionRequest, VisionResponse

__all__ = [
    "ImagePart",
    "ModelError",
    "VisionCapabilities",
    "VisionRequest",
    "VisionResponse",
    "declared_capabilities",
    "get_provider",
    "get_video_provider",
    "is_configured",
]


def is_configured() -> bool:
    from ...config import get_settings

    s = get_settings()
    return bool(s.model_base_url and s.model_api_key)


def get_provider() -> "object":
    """Return the configured **image** provider adapter (M5.1/M5.2 path).

    Only one adapter exists today (OpenAI-compatible Chat Completions). It is a
    factory rather than a module-level singleton so tests can monkeypatch
    settings without leaking state between cases.
    """
    from .providers.openai_compatible import OpenAICompatibleVisionProvider

    return OpenAICompatibleVisionProvider()


def get_video_provider() -> "object":
    """Return the adapter for the ``video_native`` channel (TSB-334).

    Deliberately separate from :func:`get_provider`. The image adapter has no
    ``complete_video`` method, so using it for a video job raised ``AttributeError``
    that the broad handler recorded as a generic failure -- a silent dead channel.
    Keeping two explicit factories means the video call site cannot inherit the
    image adapter by accident, and the type is checkable in one place.

    The returned object still enforces every gate itself: authorization context,
    verified capability, registered protocol, MIME allow-list and payload size.
    This factory chooses an adapter; it does not grant permission to send.
    """
    from .providers.native_video import NativeVideoVisionProvider

    return NativeVideoVisionProvider()
