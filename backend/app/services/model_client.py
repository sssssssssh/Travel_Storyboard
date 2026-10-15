"""Minimal OpenAI-compatible chat client for optional direct model analysis.

Retained as the **compatibility entry point** for the M3 ``direct_analyze``
path. Since M5 (TSB-302) the real work is done by
:mod:`app.services.vision.providers`; this module delegates so that the old
route keeps working unchanged while gaining the adapter's bounded retries,
classified error codes and cancellation support.

Disabled by default; activates only when TRAVELSB_MODEL_BASE_URL and
TRAVELSB_MODEL_API_KEY are configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import get_settings
from .vision.errors import MODEL_NOT_CONFIGURED, ModelError
from .vision.providers.base import ImagePart, VisionRequest


def is_configured() -> bool:
    s = get_settings()
    return bool(s.model_base_url and s.model_api_key)


def image_message(text: str, image_paths: list[str | Path]) -> dict[str, Any]:
    """Build a vision chat message with up to several images.

    Kept for callers that assemble raw messages themselves (and for the tests
    that assert the wire format). New code should use the provider adapter.
    """
    import base64

    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for p in image_paths:
        data = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
    return {"role": "user", "content": content}


def _flatten(messages: list[dict[str, Any]]) -> tuple[str, str, list[ImagePart]]:
    """Split raw chat messages into (system, text, images) for the adapter."""
    system_parts: list[str] = []
    user_text: list[str] = []
    images: list[ImagePart] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            (system_parts if role == "system" else user_text).append(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                (system_parts if role == "system" else user_text).append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url") or ""
                if url.startswith("data:") and ";base64," in url:
                    # Inline data URIs have no path; the adapter reads from disk,
                    # so spill to a temp file only when a real path is absent.
                    images.append(ImagePart(path=_materialize_data_uri(url), label=""))
    return "\n".join(system_parts), "\n".join(user_text), images


def _materialize_data_uri(url: str) -> str:
    import base64
    import tempfile
    import uuid

    header, _, payload = url.partition(";base64,")
    suffix = ".jpg"
    if "/png" in header:
        suffix = ".png"
    elif "/webp" in header:
        suffix = ".webp"
    tmp = Path(tempfile.gettempdir()) / f"tsb-vision-{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(base64.b64decode(payload))
    return str(tmp)


def chat(
    messages: list[dict[str, Any]],
    temperature: float = 0.4,
    max_tokens: int = 4000,
    cancel_check=None,
) -> str:
    """Compatibility wrapper around the provider adapter.

    Raises :class:`ModelError` (a ``RuntimeError`` subclass) with a stable
    ``code`` on failure, so existing ``except RuntimeError`` callers keep
    working while new callers can discriminate.
    """
    if not is_configured():
        raise ModelError(
            MODEL_NOT_CONFIGURED,
            "Model not configured. Set TRAVELSB_MODEL_BASE_URL / TRAVELSB_MODEL_API_KEY.",
        )

    from .vision import get_provider

    system, text, images = _flatten(messages)
    request = VisionRequest(
        prompt=text,
        images=images,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    provider = get_provider()
    return provider.complete(request, cancel_check=cancel_check).text
