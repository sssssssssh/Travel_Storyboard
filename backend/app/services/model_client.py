"""Minimal OpenAI-compatible chat client for optional direct model analysis.

Disabled by default; activates only when TRAVELSB_MODEL_BASE_URL and
TRAVELSB_MODEL_API_KEY are configured.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from ..config import get_settings


def is_configured() -> bool:
    s = get_settings()
    return bool(s.model_base_url and s.model_api_key)


def _image_url(path: str | Path, mime: str = "image/jpeg") -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def chat(
    messages: list[dict[str, Any]],
    temperature: float = 0.4,
    max_tokens: int = 4000,
) -> str:
    s = get_settings()
    if not is_configured():
        raise RuntimeError(
            "Model not configured. Set TRAVELSB_MODEL_BASE_URL / TRAVELSB_MODEL_API_KEY."
        )
    url = s.model_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": s.model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {s.model_api_key}",
        "Content-Type": "application/json",
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=300.0)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected model response: {data}") from exc


def image_message(text: str, image_paths: list[str | Path]) -> dict[str, Any]:
    """Build a vision chat message with up to several images."""
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _image_url(p)}})
    return {"role": "user", "content": content}
