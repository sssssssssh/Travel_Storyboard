"""OpenAI-compatible Chat Completions vision adapter.

Covers the well-trodden ``POST {base_url}/chat/completions`` shape with
``image_url`` content parts carrying base64 data URIs. This is the format the
task book selects as the M5 default (multi-image, no vendor lock-in).

Deliberate behaviours:

* bounded backoff only for genuinely transient failures, honouring
  ``Retry-After``; 401/403 and unsupported-image 400s fail immediately;
* no request body, base64 payload, key or raw model prose is ever logged;
* cancellation is checked before each attempt *and* before returning a result,
  so a cancelled job cannot commit a late-arriving answer.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from ....config import get_settings
from .. import capabilities as _capabilities
from ..capabilities import assert_images_supported
from ..errors import (
    MODEL_BAD_REQUEST,
    MODEL_INVALID_OUTPUT,
    MODEL_NETWORK_ERROR,
    MODEL_NOT_CONFIGURED,
    MODEL_TIMEOUT,
    ModelError,
    classify_status,
    redact,
)
from .base import VisionRequest, VisionResponse

logger = logging.getLogger(__name__)


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def validate_endpoint(base_url: str, *, allow_insecure: bool) -> None:
    """Reject endpoints that would leak images or credentials in cleartext.

    ``http://`` is only tolerated for a loopback address (a local model server)
    or when the operator explicitly accepts it.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ModelError(MODEL_NOT_CONFIGURED, f"unsupported URL scheme: {parsed.scheme!r}")
    if parsed.scheme == "http" and not allow_insecure and not _is_loopback(parsed.hostname):
        raise ModelError(
            MODEL_NOT_CONFIGURED,
            "refusing cleartext http:// to a non-loopback host; "
            "set TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=1 to accept it explicitly",
        )


def _data_uri(path: str | Path) -> tuple[str, int]:
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", len(data)


class OpenAICompatibleVisionProvider:
    name = "openai_compatible"

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.model_base_url.rstrip("/")
        self.api_key = s.model_api_key
        self.model_id = s.model_name
        self.max_retries = max(0, s.model_max_retries)
        self.allow_insecure = s.model_allow_insecure_http

    # -- public ------------------------------------------------------------
    def complete(
        self,
        request: VisionRequest,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionResponse:
        if not (self.base_url and self.api_key):
            raise ModelError(
                MODEL_NOT_CONFIGURED,
                "vision model not configured (base URL / API key missing)",
            )
        validate_endpoint(self.base_url, allow_insecure=self.allow_insecure)

        caps = _capabilities.declared_capabilities()
        assert_images_supported(caps, len(request.images))

        content: list[dict] = [{"type": "text", "text": request.prompt}]
        for img in request.images:
            # The label travels as text, never as a filesystem path.
            uri, size = _data_uri(img.path)
            if img.label:
                content.append({"type": "text", "text": f"[{img.label}]"})
            content.append({"type": "image_url", "image_url": {"url": uri}})
            logger.debug("vision image %s: %d bytes", img.label, size)

        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": content})

        payload: dict = {
            "model": self.model_id,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_object:
            payload["response_format"] = {"type": "json_object"}

        body_bytes = json.dumps(payload).encode("utf-8")
        if len(body_bytes) > caps.max_request_bytes:
            raise ModelError(
                MODEL_BAD_REQUEST,
                f"request body {len(body_bytes)}B exceeds limit {caps.max_request_bytes}B",
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        request_id = uuid.uuid4().hex[:16]
        last: ModelError | None = None
        started = time.monotonic()

        for attempt in range(1, self.max_retries + 2):
            self._raise_if_cancelled(cancel_check)
            try:
                resp = httpx.post(
                    url,
                    content=body_bytes,
                    headers=headers,
                    timeout=caps.timeout_sec,
                )
            except httpx.TimeoutException as exc:
                last = ModelError(MODEL_TIMEOUT, f"request timed out: {exc}", request_id=request_id)
            except httpx.HTTPError as exc:
                last = ModelError(
                    MODEL_NETWORK_ERROR,
                    redact(str(exc), self.api_key),
                    request_id=request_id,
                )
            else:
                if resp.status_code == 200:
                    # A cancel that arrived while the request was in flight must
                    # not be allowed to publish its answer.
                    self._raise_if_cancelled(cancel_check)
                    return self._parse(resp, request_id, attempt, started)

                text = redact(resp.text[:500], self.api_key)
                code = classify_status(resp.status_code, text)
                last = ModelError(
                    code,
                    f"provider returned {resp.status_code}: {text}",
                    status=resp.status_code,
                    retry_after=_retry_after(resp),
                    request_id=request_id,
                )

            if last is None or not last.retryable or attempt > self.max_retries:
                raise last
            delay = self._backoff(attempt, last.retry_after)
            logger.warning(
                "vision attempt %d failed [%s]; retrying in %.1fs (request_id=%s)",
                attempt,
                last.code,
                delay,
                request_id,
            )
            self._sleep(delay, cancel_check)

        raise last or ModelError(MODEL_NETWORK_ERROR, "vision call failed", request_id=request_id)

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
        if cancel_check is not None and cancel_check():
            from ..errors import CANCELLED

            raise ModelError(CANCELLED, "vision call cancelled")

    def _sleep(self, delay: float, cancel_check: Callable[[], bool] | None) -> None:
        """Sleep in slices so a cancel is honoured promptly."""
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            self._raise_if_cancelled(cancel_check)
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after is not None and retry_after > 0:
            return min(retry_after, 30.0)
        return min(0.5 * (2 ** (attempt - 1)), 8.0)

    def _parse(
        self, resp: httpx.Response, request_id: str, attempt: int, started: float
    ) -> VisionResponse:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelError(
                MODEL_INVALID_OUTPUT,
                f"provider returned non-JSON body: {exc}",
                status=resp.status_code,
                request_id=request_id,
            ) from exc

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(
                MODEL_INVALID_OUTPUT,
                "provider response is missing choices[0].message",
                status=resp.status_code,
                request_id=request_id,
            ) from exc

        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            # Some reasoning models answer into a separate field and leave
            # `content` null when the token budget is spent on reasoning. That
            # is an unusable result, not a success: report it as invalid output
            # (C6) rather than silently persisting an empty observation.
            finish = choice.get("finish_reason")
            reasoning = message.get("reasoning")
            detail = "empty content"
            if isinstance(reasoning, str) and reasoning.strip():
                detail = "model returned reasoning without content (raise max_tokens)"
            elif finish == "length":
                detail = "model hit the token limit before producing content"
            raise ModelError(
                MODEL_INVALID_OUTPUT,
                detail,
                status=resp.status_code,
                request_id=request_id,
            )

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return VisionResponse(
            text=text,
            model_id=str(data.get("model") or self.model_id),
            usage=usage or {},
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=str(data.get("id") or request_id),
            attempts=attempt,
        )


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
