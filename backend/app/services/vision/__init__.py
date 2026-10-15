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
    "is_configured",
]


def is_configured() -> bool:
    from ...config import get_settings

    s = get_settings()
    return bool(s.model_base_url and s.model_api_key)


def get_provider() -> "object":
    """Return the configured provider adapter.

    Only one adapter exists today (OpenAI-compatible Chat Completions). It is a
    factory rather than a module-level singleton so tests can monkeypatch
    settings without leaking state between cases.
    """
    from .providers.openai_compatible import OpenAICompatibleVisionProvider

    return OpenAICompatibleVisionProvider()
