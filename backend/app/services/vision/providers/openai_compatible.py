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
import ipaddress
import mimetypes
import re
import ssl
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
    MODEL_TLS_FAILED,
    MODEL_UPLOAD_DENIED,
    ModelError,
    classify_status,
    redact,
)
from .base import VisionRequest, VisionResponse

logger = logging.getLogger(__name__)


class _EndpointLogFilter(logging.Filter):
    def filter(self, record):
        # httpx logs full request URLs at INFO even when our own logger is quiet.
        record.msg = re.sub(r'https?://[^\s"\']+', '[model-endpoint]', record.getMessage())
        record.args = ()
        return True


logging.getLogger('httpx').addFilter(_EndpointLogFilter())


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_endpoint(base_url: str, *, allow_insecure: bool) -> None:
    """Reject endpoints that would leak images or credentials in cleartext.

    ``http://`` is only tolerated for a loopback address (a local model server)
    or when the operator explicitly accepts it.
    """
    try:
        parsed = urlparse(base_url)
        valid = (parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ModelError(MODEL_NOT_CONFIGURED, 'invalid model endpoint; use a URL without embedded credentials/query')
    if parsed.scheme not in {"http", "https"}:
        raise ModelError(MODEL_NOT_CONFIGURED, "unsupported model endpoint scheme")
    if parsed.scheme == "http" and not allow_insecure and not _is_loopback(parsed.hostname):
        raise ModelError(
            MODEL_NOT_CONFIGURED,
            "refusing cleartext http:// to a non-loopback host; "
            "HTTPS or an authenticated encrypted tunnel is required; insecure HTTP is a development-only exception",
        )


#: Accepted image MIME types. Anything else is refused before encoding
#: (TSB-313: a forged MIME must not be smuggled through as an image).
ALLOWED_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})

#: Magic-byte prefixes, so a mislabelled payload is caught by content rather than
#: by the declared type alone.
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),  # WEBP is RIFF....WEBP; the container check suffices here
)

#: Hard per-image ceiling for in-memory payloads. The request-body limit is
#: enforced separately; this catches an oversized single image earlier and with a
#: clearer message.
MAX_INLINE_IMAGE_BYTES = 12_000_000


def sniff_mime(data: bytes) -> str | None:
    """Return the MIME implied by the payload's magic bytes, or None."""
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return None


def validate_inline_image(data: bytes, declared_mime: str = "") -> str:
    """Validate an in-memory image and return the MIME to send.

    Raises :class:`ModelError` with ``MODEL_BAD_REQUEST`` when the payload is
    empty, oversized, not an image, or its declared type disagrees with its
    content. A forged type must never reach the wire.
    """
    if not data:
        raise ModelError(MODEL_BAD_REQUEST, "image payload is empty")
    if len(data) > MAX_INLINE_IMAGE_BYTES:
        raise ModelError(
            MODEL_BAD_REQUEST,
            f"image payload {len(data)}B exceeds the inline limit {MAX_INLINE_IMAGE_BYTES}B",
        )

    actual = sniff_mime(data)
    if actual is None:
        raise ModelError(
            MODEL_BAD_REQUEST,
            "image payload is not a recognised JPEG/PNG/WEBP (magic bytes do not match)",
        )
    if actual not in ALLOWED_IMAGE_MIMES:
        raise ModelError(MODEL_BAD_REQUEST, f"unsupported image type: {actual}")

    declared = (declared_mime or "").split(";")[0].strip().lower()
    if declared and declared != actual:
        raise ModelError(
            MODEL_BAD_REQUEST,
            f"declared image type {declared!r} does not match its content ({actual})",
        )
    return actual


def _data_uri(path: str | Path) -> tuple[str, int]:
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", len(data)


def _inline_data_uri(image) -> tuple[str, int]:
    """Encode an in-memory :class:`ImagePart` without touching the filesystem."""
    mime = validate_inline_image(image.data or b"", image.mime)
    return f"data:{mime};base64,{base64.b64encode(image.data or b'').decode('ascii')}", len(image.data or b"")


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
        self, request: VisionRequest, *, cancel_check: Callable[[], bool] | None = None,
    ) -> VisionResponse:
        try:
            return self._complete(request, cancel_check=cancel_check)
        except ModelError as exc:
            # Failures before the wire (policy/encoding/capability) cost no calls.
            if exc.attempts is None:
                exc.attempts = 0
            if exc.images_sent is None:
                exc.images_sent = 0
            raise

    def _complete(
        self,
        request: VisionRequest,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionResponse:
        # TSB-312: refuse before any network work if this call was not authorised
        # by the business entry point. This is the chokepoint that makes an
        # unguarded outbound request impossible rather than merely discouraged.
        from ..authorization import require_context

        require_context(request.authorization)
        self._require_mode_on()

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
            if img.data is not None:
                # In-memory payload (legacy compat layer): nothing is written to
                # disk, so there is no temp copy to leak on any exit path.
                uri, size = _inline_data_uri(img)
            else:
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
            try:
                self._raise_if_cancelled(cancel_check)
                self._require_mode_on()
            except ModelError as exc:
                exc.attempts, exc.images_sent = attempt - 1, len(request.images) * (attempt - 1)
                raise
            try:
                resp = httpx.post(
                    url,
                    content=body_bytes,
                    headers=headers,
                    timeout=caps.timeout_sec,
                )
            except httpx.TimeoutException as exc:
                last = ModelError(MODEL_TIMEOUT, "model request timed out", request_id=request_id)
            except httpx.HTTPError as exc:
                cause = exc
                tls = False
                seen = set()
                while cause is not None and id(cause) not in seen:
                    seen.add(id(cause))
                    if isinstance(cause, ssl.SSLError) or 'certificate_verify_failed' in str(cause).lower():
                        tls = True
                    cause = cause.__cause__ or cause.__context__
                last = ModelError(
                    MODEL_TLS_FAILED if tls else MODEL_NETWORK_ERROR,
                    "model TLS certificate verification failed" if tls else "model network connection failed",
                    request_id=request_id,
                )
            else:
                if resp.status_code == 200:
                    # A cancel that arrived while the request was in flight must
                    # not be allowed to publish its answer.
                    try:
                        self._raise_if_cancelled(cancel_check)
                        return self._parse(resp, request_id, attempt, started)
                    except ModelError as exc:
                        exc.attempts, exc.images_sent = attempt, len(request.images) * attempt
                        raise

                code = classify_status(resp.status_code, resp.text[:500])
                last = ModelError(
                    code,
                    f"provider returned {resp.status_code}: details ***REDACTED***",
                    status=resp.status_code,
                    retry_after=_retry_after(resp),
                    request_id=request_id,
                )

            if last is not None:
                last.attempts, last.images_sent = attempt, len(request.images) * attempt
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
            try:
                self._sleep(delay, cancel_check)
            except ModelError as exc:
                exc.attempts, exc.images_sent = attempt, len(request.images) * attempt
                raise

        raise last or ModelError(MODEL_NETWORK_ERROR, "vision call failed", request_id=request_id)

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _require_mode_on():
        if get_settings().model_mode != 'on':
            raise ModelError(MODEL_UPLOAD_DENIED, 'model uploads are disabled')

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
                "provider returned a non-JSON body",
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

        raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        usage = {k: v for k, v in raw_usage.items()
                 if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                 and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
        return VisionResponse(
            text=text,
            model_id=self.model_id,
            usage=usage or {},
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=request_id,
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
