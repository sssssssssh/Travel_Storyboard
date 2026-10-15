"""Minimal OpenAI-compatible chat client for optional direct model analysis.

Retained as the **compatibility entry point** for the M3 ``direct_analyze``
path. Since M5 (TSB-302) the real work is done by
:mod:`app.services.vision.providers`; this module delegates so the old route
keeps working while gaining the adapter's bounded retries, classified error
codes, cancellation and authorization boundary.

TSB-313 changed how inline images are handled: a ``data:`` URI from
:func:`image_message` used to be decoded into a file under the system temp
directory, and that file was **never deleted** — every call, successful or not,
left a copy of a frame on disk in ``/tmp`` or ``%TEMP%``. The payload now travels
in memory as ``ImagePart.data``, so there is no temp file to leak on any exit
path (success, error, timeout, cancel).

Disabled by default; activates only when TRAVELSB_MODEL_BASE_URL and
TRAVELSB_MODEL_API_KEY are configured.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from ..config import get_settings
from .vision.authorization import require_context
from .vision.errors import MODEL_BAD_REQUEST, MODEL_NOT_CONFIGURED, ModelError
from .vision.providers.base import ImagePart, VisionRequest

#: Ceiling on a single inline data URI, mirroring the provider's inline limit.
#: Checked before base64 decoding so a hostile payload cannot balloon memory
#: (base64 inflates by ~4/3, and decoding an unbounded string is the risk).
MAX_DATA_URI_CHARS = 18_000_000


def is_configured() -> bool:
    s = get_settings()
    return bool(s.model_base_url and s.model_api_key)


def image_message(text: str, image_paths: list[str | Path]) -> dict[str, Any]:
    """Build a vision chat message with up to several images.

    Kept for callers that assemble raw messages themselves (and for the tests
    that assert the wire format). New code should use the provider adapter.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for p in image_paths:
        data = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
    return {"role": "user", "content": content}


def decode_data_uri(url: str) -> tuple[bytes, str]:
    """Decode a ``data:image/...;base64,...`` URI into ``(bytes, mime)``.

    Everything happens in memory. Failures are classified rather than swallowed,
    because a silently dropped image would send a prompt the model cannot answer
    (C6).
    """
    if not url.startswith("data:"):
        raise ModelError(MODEL_BAD_REQUEST, "not a data URI")
    if len(url) > MAX_DATA_URI_CHARS:
        raise ModelError(
            MODEL_BAD_REQUEST,
            f"inline image is too large ({len(url)} chars > {MAX_DATA_URI_CHARS})",
        )

    header, sep, payload = url.partition(";base64,")
    if not sep:
        raise ModelError(MODEL_BAD_REQUEST, "data URI is not base64-encoded")

    mime = header[len("data:") :].strip().lower()
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ModelError(MODEL_BAD_REQUEST, f"data URI payload is not valid base64: {exc}") from exc
    return data, mime


def _flatten(messages: list[dict[str, Any]]) -> tuple[str, str, list[ImagePart]]:
    """Split raw chat messages into (system, text, images) for the adapter.

    Inline data URIs become in-memory :class:`ImagePart` values; nothing is
    written to the filesystem.
    """
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
                if url.startswith("data:"):
                    data, mime = decode_data_uri(url)
                    images.append(ImagePart(data=data, mime=mime, label=""))
                elif url:
                    # A plain path/URL is passed through as a path.
                    images.append(ImagePart(path=url, label=""))
    return "\n".join(system_parts), "\n".join(user_text), images


def chat(
    messages: list[dict[str, Any]],
    temperature: float = 0.4,
    max_tokens: int = 4000,
    cancel_check=None,
    *,
    authorization=None,
) -> str:
    """Compatibility wrapper around the provider adapter.

    TSB-312: ``authorization`` is the per-call token from
    :func:`app.services.vision.authorization.authorize_upload`. It is required so
    this layer cannot be used to reach the network without the business entry
    point having decided that this upload is allowed.

    Raises :class:`ModelError` (a ``RuntimeError`` subclass) with a stable
    ``code`` on failure, so existing ``except RuntimeError`` callers keep
    working while new callers can discriminate.
    """
    require_context(authorization)

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
        authorization=authorization,
    )
    provider = get_provider()
    return provider.complete(request, cancel_check=cancel_check).text
