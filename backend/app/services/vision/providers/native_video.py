"""TSB-326 — native-video provider (pluggable, default-off, capability-gated).

This adapter can speak exactly one protocol: ``video_url_data_uri``, a
``video_url`` content part carrying an inline base64 data URI. That choice is
registered in :mod:`app.services.vision.video_capabilities`, not sniffed from a
model name.

Three refusals define the module, and all three happen **before** any network
call:

1. **No authorization context, no send.** Same chokepoint as the image path
   (TSB-312), so a future refactor cannot quietly add an unguarded video path.
2. **No verified capability, no send.** ``model_supports_native_video=true`` in
   the environment is an *intent*, not evidence; sending on it alone is precisely
   the mistake the task book warns about. The caller must present a capability
   record that is actually sendable.
3. **No registered protocol / disallowed MIME / oversize payload, no send.**
   Checked against the registry so an unknown format fails closed.

On SSRF: this protocol is inline-only, so no URL is ever fetched by the server on
our behalf. A protocol that *did* fetch (``fetches_remote=True``) would need its
own host-allow-list handling; rather than half-implement that, this adapter
refuses to construct such a payload at all and points at the registry.

Honesty about cancellation: once the request is in flight the remote may have
already decoded and billed it. We stop waiting and refuse to publish the result,
but we do **not** claim the cost was avoided — ``remote_cost_uncertain`` exists to
say so explicitly.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Callable

import httpx

from ....config import get_settings
from ..errors import (
    MODEL_BAD_REQUEST,
    MODEL_CAPABILITY_UNSUPPORTED,
    MODEL_INVALID_OUTPUT,
    MODEL_NETWORK_ERROR,
    MODEL_NOT_CONFIGURED,
    MODEL_TIMEOUT,
    ModelError,
    classify_status,
    redact,
)
from ..video_capabilities import get_protocol
from .base import VideoRequest, VideoResponse
from .openai_compatible import validate_endpoint

logger = logging.getLogger(__name__)

#: Media types this adapter will transmit. Kept beside the registry's own
#: allow-list as a second, independent gate: a registry mistake must not be able
#: to open the wire.
ALLOWED_VIDEO_MIMES = frozenset({"video/mp4"})

#: Magic bytes for ISO-BMFF (MP4/MOV). The declared MIME is checked *and* the
#: content, because a mislabelled payload is how a non-video file would otherwise
#: be smuggled into an upload.
_MP4_MAGIC = (b"\x00\x00\x00",)  # ftyp box size; the brand check follows


def sniff_video_mime(data: bytes) -> str | None:
    """Return the container implied by magic bytes, or None.

    MP4/MOV both use the ISO base media format: a 4-byte box size followed by
    ``ftyp``. We check that shape rather than trusting the extension, which also
    means a truncated or non-video payload is rejected here.
    """
    if len(data) < 12:
        return None
    if data[4:8] != b"ftyp":
        return None
    if data[0:4] == b"\x00\x00\x00\x00":
        return None  # a zero box size is malformed
    return "video/mp4"


def validate_video_payload(data: bytes, declared_mime: str, *, max_bytes: int) -> str:
    """Validate an in-memory clip and return the MIME to send.

    Raises ``ModelError`` with ``MODEL_BAD_REQUEST`` on empty, oversize,
    unrecognised or mislabelled content. The declared type must agree with the
    content, so a forged MIME never reaches the wire.
    """
    if not data:
        raise ModelError(MODEL_BAD_REQUEST, "video payload is empty")
    if len(data) > max_bytes:
        raise ModelError(
            MODEL_BAD_REQUEST,
            f"video payload {len(data)}B exceeds the limit {max_bytes}B",
        )

    actual = sniff_video_mime(data)
    if actual is None:
        raise ModelError(
            MODEL_BAD_REQUEST,
            "video payload is not a recognised ISO-BMFF (MP4/MOV) container; "
            "the declared type alone is not evidence",
        )

    declared = (declared_mime or "").split(";")[0].strip().lower()
    if declared and declared not in ALLOWED_VIDEO_MIMES:
        raise ModelError(MODEL_BAD_REQUEST, f"unsupported video type: {declared}")
    if declared and declared != actual:
        raise ModelError(
            MODEL_BAD_REQUEST,
            f"declared video type {declared!r} does not match its content ({actual})",
        )
    return actual


class NativeVideoVisionProvider:
    """Provider for the ``video_native`` channel."""

    name = "native_video"

    def __init__(self) -> None:
        s = get_settings()
        self.base_url = s.model_base_url.rstrip("/")
        self.api_key = s.model_api_key
        self.model_id = s.model_name
        self.max_retries = max(0, s.model_max_retries)
        self.allow_insecure = s.model_allow_insecure_http
        self.timeout_sec = max(1.0, s.model_timeout_sec)
        self.max_bytes = max(1, s.model_max_request_bytes)

    # -- public ------------------------------------------------------------
    def complete_video(
        self,
        request: VideoRequest,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VideoResponse:
        # Gate 1: authorization (TSB-312 chokepoint, shared with the image path).
        from ..authorization import require_context

        require_context(request.authorization)

        if not (self.base_url and self.api_key):
            raise ModelError(
                MODEL_NOT_CONFIGURED,
                "vision model not configured (base URL / API key missing)",
            )
        validate_endpoint(self.base_url, allow_insecure=self.allow_insecure)

        # Gate 2: the protocol must be registered and must not require the server
        # to fetch a URL on our behalf.
        if not request.protocol:
            raise ModelError(
                MODEL_CAPABILITY_UNSUPPORTED,
                "no video protocol chosen; the capability probe must select one "
                "rather than defaulting to a guess",
            )
        try:
            protocol = get_protocol(request.protocol)
        except KeyError as exc:
            raise ModelError(MODEL_CAPABILITY_UNSUPPORTED, str(exc)) from None
        if protocol.fetches_remote:
            raise ModelError(
                MODEL_CAPABILITY_UNSUPPORTED,
                f"protocol {protocol.name!r} requires the server to fetch a URL; "
                "this adapter only sends inline payloads (no SSRF surface)",
            )

        # Gate 3: media type and size, checked on content not just declaration.
        video = request.video
        mime = validate_video_payload(
            video.data, video.mime_type, max_bytes=self.max_bytes
        )
        if mime not in protocol.allowed_mimes:
            raise ModelError(
                MODEL_CAPABILITY_UNSUPPORTED,
                f"protocol {protocol.name!r} does not carry {mime}",
            )

        encoded = base64.b64encode(video.data).decode("ascii")
        content: list[dict] = [{"type": "text", "text": request.prompt}]
        if video.label:
            content.append({"type": "text", "text": f"[{video.label}]"})
        content.append(
            {
                "type": "video_url",
                "video_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )

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
        if len(body_bytes) > self.max_bytes:
            raise ModelError(
                MODEL_BAD_REQUEST,
                f"request body {len(body_bytes)}B exceeds limit {self.max_bytes}B",
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
                    timeout=self.timeout_sec,
                )
            except httpx.TimeoutException as exc:
                last = ModelError(MODEL_TIMEOUT, f"request timed out: {exc}", request_id=request_id)
            except httpx.HTTPError as exc:
                last = ModelError(
                    MODEL_NETWORK_ERROR, redact(str(exc), self.api_key), request_id=request_id
                )
            else:
                if resp.status_code == 200:
                    # A cancel that landed while the request was in flight must not
                    # publish its answer.
                    self._raise_if_cancelled(cancel_check)
                    return self._parse(
                        resp, request_id, attempt, started, video.clip_id, len(video.data)
                    )

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
                "video attempt %d failed [%s]; retrying in %.1fs (request_id=%s)",
                attempt,
                last.code,
                delay,
                request_id,
            )
            self._sleep(delay, cancel_check)

        raise last or ModelError(MODEL_NETWORK_ERROR, "video call failed", request_id=request_id)

    # -- internals ---------------------------------------------------------
    @staticmethod
    def _raise_if_cancelled(cancel_check: Callable[[], bool] | None) -> None:
        if cancel_check is not None and cancel_check():
            from ..errors import CANCELLED

            raise ModelError(
                CANCELLED,
                "video call cancelled locally; note the remote may already have "
                "processed and billed this request (remote_cost_uncertain)",
            )

    def _sleep(self, delay: float, cancel_check: Callable[[], bool] | None) -> None:
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
        self,
        resp: httpx.Response,
        request_id: str,
        attempt: int,
        started: float,
        clip_id: str,
        sent_bytes: int,
    ) -> VideoResponse:
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
            # Same rule as the image path: an unusable body is a failure, not an
            # empty success (C6). Reasoning models hit this when max_tokens is too
            # small for reasoning + answer.
            finish = choice.get("finish_reason")
            reasoning = message.get("reasoning")
            detail = "empty content"
            if isinstance(reasoning, str) and reasoning.strip():
                detail = "model returned reasoning without content (raise max_tokens)"
            elif finish == "length":
                detail = "model hit the token limit before producing content"
            raise ModelError(
                MODEL_INVALID_OUTPUT, detail, status=resp.status_code, request_id=request_id
            )

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        # Only the server can tell us how it sampled; we never infer it.
        sampling_disclosed = bool(
            isinstance(data.get("video_sampling"), dict) or data.get("sampling_fps")
        )
        return VideoResponse(
            text=text,
            clip_id=clip_id,
            model_id=str(data.get("model") or self.model_id),
            usage=usage or {},
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=str(data.get("id") or request_id),
            attempts=attempt,
            sent_bytes=sent_bytes,
            sampling_disclosed=sampling_disclosed,
        )


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def capability_gate(caps) -> None:
    """Refuse the video channel unless the capability record is sendable.

    Callers must run this before constructing a :class:`VideoRequest`, so the
    "video is not available" decision happens before any media is rendered — the
    task book requires a 0-byte footprint when the channel is unavailable.
    """
    if caps is None:
        raise ModelError(
            MODEL_CAPABILITY_UNSUPPORTED,
            "VIDEO_CAPABILITY_UNVERIFIED: no capability record; video is not enabled",
        )
    if not getattr(caps, "sendable", False):
        state = getattr(caps, "state", "unverified")
        raise ModelError(
            MODEL_CAPABILITY_UNSUPPORTED,
            f"VIDEO_CAPABILITY_{str(state).upper()}: the video channel is not "
            "allowed to send (verified capability required)",
        )
