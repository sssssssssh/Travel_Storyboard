"""TSB-325 — native-video capability contract.

The task book's P0 gate is blunt: **the endpoint's video ability must be probed
before any video channel is allowed to exist**, and "HTTP 2xx and some unrelated
text came back" does not count as support.

This module holds the *shape* of that answer plus the protocol registry that lets
a platform adapter be registered explicitly. Two rules drive the design:

1. **Unknown is a value, not a zero.** A field nobody measured is ``None``
   ("unknown"), never a guessed number. Filling in a plausible 10 MB / 30 s
   because those feel reasonable is fabrication, and downstream budget checks
   would then enforce a limit that was never observed.
2. **Protocols are registered, not sniffed.** Selecting the wire format by
   testing ``"glm" in model_name`` is the anti-pattern the task book names; each
   platform adapter declares the protocol it speaks.

Note the separation this keeps from :mod:`app.services.vision.capabilities`:
``VisionCapabilities`` describes what the *operator declared* (and what the
image path is allowed to attempt). :class:`VideoCapabilities` describes what was
*observed* about video. A declared flag never upgrades itself into a verified
one — only :func:`record_verification` does that.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

#: The only four honest answers to "can this endpoint take video?".
#:
#: * ``verified_native_video`` — a real video was accepted *and* the response
#:   showed usable temporal information attributable to that video.
#: * ``video_accepted_sampling_unknown`` — the request succeeded but we cannot
#:   say how the server handled the video (e.g. it may have sampled frames
#:   server-side, or ignored it entirely).
#: * ``unsupported`` — the endpoint rejected video, or cannot accept it.
#: * ``unverified`` — nobody has run an authorised probe yet. **This is the
#:   default**, and it is not permission to try.
VERIFIED_NATIVE_VIDEO = "verified_native_video"
VIDEO_ACCEPTED_SAMPLING_UNKNOWN = "video_accepted_sampling_unknown"
UNSUPPORTED = "unsupported"
UNVERIFIED = "unverified"

CAPABILITY_STATES = frozenset(
    {
        VERIFIED_NATIVE_VIDEO,
        VIDEO_ACCEPTED_SAMPLING_UNKNOWN,
        UNSUPPORTED,
        UNVERIFIED,
    }
)

#: States in which the video channel may actually send bytes. Deliberately
#: excludes ``video_accepted_sampling_unknown``: acceptance without a
#: demonstrable temporal reading is not enough to spend a user's footage on.
SENDABLE_STATES = frozenset({VERIFIED_NATIVE_VIDEO})


@dataclass(frozen=True)
class VideoCapabilities:
    """What is known about the endpoint's video input.

    Every *limit* defaults to ``None``, meaning "not measured". A ``None`` limit
    must be surfaced as unknown rather than substituted with a default, because a
    budget enforced against an invented ceiling is worse than no ceiling: it
    looks authoritative and is wrong.
    """

    state: str = UNVERIFIED
    #: Wire protocol the probe actually used, e.g. ``video_url_data_uri``. Empty
    #: when nothing has been verified.
    protocol: str = ""
    max_duration_sec: float | None = None
    max_bytes: int | None = None
    #: ``None`` = not established. ``False`` = the server demonstrably drops it.
    accepts_audio: bool | None = None
    #: True only when the server *told us* how it samples (fps/frame cap). The
    #: task book forbids inferring this from a successful call.
    temporal_sampling_disclosed: bool = False
    model_id: str = ""
    probe_time: str = ""
    #: What exactly was proven — free-form but non-sensitive, e.g.
    #: "synthetic 3s clip, single frame sequence, temporal order echoed".
    verification_scope: str = ""
    #: Non-sensitive identifiers only (server type, version string). Never a key,
    #: URL or file path.
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in CAPABILITY_STATES:
            raise ValueError(f"unknown video capability state: {self.state!r}")

    @property
    def verified(self) -> bool:
        return self.state == VERIFIED_NATIVE_VIDEO

    @property
    def sendable(self) -> bool:
        """May the video channel put bytes on the wire?"""
        return self.state in SENDABLE_STATES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Derived flags are materialised so a reader never has to re-derive them
        # (and cannot get the rule wrong).
        data["verified"] = self.verified
        data["sendable"] = self.sendable
        # Make the "unknown is unknown" rule explicit on the wire so a consumer
        # cannot mistake null for "no limit".
        data["unknown_fields"] = sorted(
            k for k, v in data.items() if v is None and k != "unknown_fields"
        )
        return data

    def public_dict(self) -> dict[str, Any]:
        """Serialisable summary that is safe to show in the UI / reports."""
        return {
            "state": self.state,
            "verified": self.verified,
            "sendable": self.sendable,
            "protocol": self.protocol,
            "model_id": self.model_id,
            "probe_time": self.probe_time,
            "verification_scope": self.verification_scope,
            "temporal_sampling_disclosed": self.temporal_sampling_disclosed,
            "max_duration_sec": self.max_duration_sec,
            "max_bytes": self.max_bytes,
            "accepts_audio": self.accepts_audio,
        }


#: A field name -> honest summary of why it is still unknown.
_DEFAULT_UNKNOWN_REASONS = {
    "max_duration_sec": "服务端未返回时长上限；未做边界压测",
    "max_bytes": "服务端未返回字节上限；未做边界压测",
    "accepts_audio": "未测试含音轨输入（默认静音派生，故未测）",
}


def unverified_capabilities(*, model_id: str = "", reason: str = "") -> VideoCapabilities:
    """The honest starting state: nothing has been proven.

    Callers must treat this as "video is not available", not as "video untried,
    so let us try it".
    """
    return VideoCapabilities(
        state=UNVERIFIED,
        model_id=model_id,
        detail={"reason": reason} if reason else {},
    )


def record_verification(
    *,
    state: str,
    protocol: str,
    model_id: str,
    verification_scope: str,
    max_duration_sec: float | None = None,
    max_bytes: int | None = None,
    accepts_audio: bool | None = None,
    temporal_sampling_disclosed: bool = False,
    detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> VideoCapabilities:
    """Build a capability record from an *actually performed* probe.

    This is the only function that can produce a sendable state, and it demands
    the evidence that justifies it. ``verified_native_video`` without a stated
    ``verification_scope`` is refused: a claim with no scope is exactly the
    "HTTP 200 means it works" mistake.
    """
    if state not in CAPABILITY_STATES:
        raise ValueError(f"unknown video capability state: {state!r}")
    if state == VERIFIED_NATIVE_VIDEO:
        if not protocol:
            raise ValueError("verified video requires the protocol that was proven")
        if not verification_scope.strip():
            raise ValueError(
                "verified video requires a stated verification_scope; "
                "'the call succeeded' is not evidence of temporal understanding"
            )

    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return VideoCapabilities(
        state=state,
        protocol=protocol,
        model_id=model_id,
        verification_scope=verification_scope,
        max_duration_sec=max_duration_sec,
        max_bytes=max_bytes,
        accepts_audio=accepts_audio,
        temporal_sampling_disclosed=temporal_sampling_disclosed,
        probe_time=stamp,
        detail=dict(detail or {}),
    )


def unknown_reason(caps: VideoCapabilities, fieldname: str) -> str:
    """Why ``fieldname`` is still unknown, for an honest UI/report line."""
    if getattr(caps, fieldname, None) is not None:
        return ""
    return _DEFAULT_UNKNOWN_REASONS.get(fieldname, "未测")


# ---------------------------------------------------------------------------
# Protocol registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoProtocol:
    """One wire protocol a provider adapter knows how to speak.

    Registration is explicit so that choosing a protocol is a decision recorded
    in code, not a string match against a model name.
    """

    name: str
    #: MIME types this protocol will transmit. Enforced as an allow-list.
    allowed_mimes: frozenset[str]
    #: How the video reaches the server. Kept descriptive so reports can state
    #: what was actually used.
    transport: str
    #: Whether the payload can carry a URL the server fetches. Such protocols are
    #: the SSRF-sensitive ones and must never accept a user-supplied URL.
    fetches_remote: bool = False
    description: str = ""


_REGISTRY: dict[str, VideoProtocol] = {}


def register_protocol(protocol: VideoProtocol, *, replace: bool = False) -> None:
    """Register a protocol adapter. Duplicate names are refused by default."""
    if not protocol.name:
        raise ValueError("protocol needs a name")
    if not protocol.allowed_mimes:
        raise ValueError(f"protocol {protocol.name!r} must allow at least one MIME type")
    if protocol.name in _REGISTRY and not replace:
        raise ValueError(f"protocol already registered: {protocol.name!r}")
    _REGISTRY[protocol.name] = protocol


def get_protocol(name: str) -> VideoProtocol:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unregistered video protocol {name!r}; registered: {sorted(_REGISTRY)}"
        ) from None


def registered_protocols() -> dict[str, VideoProtocol]:
    return dict(_REGISTRY)


def _reset_registry_for_tests() -> None:
    _REGISTRY.clear()


#: The one protocol this build can speak, matching the existing
#: OpenAI-compatible adapter's content-part shape. ``video_url`` carrying an
#: inline data URI is the vLLM-documented form; whether *this* endpoint accepts
#: it is precisely what the probe must establish.
VIDEO_URL_DATA_URI = VideoProtocol(
    name="video_url_data_uri",
    allowed_mimes=frozenset({"video/mp4"}),
    transport="chat_completions_content_part",
    fetches_remote=False,
    description="video_url content part carrying a base64 data URI (no server fetch)",
)

register_protocol(VIDEO_URL_DATA_URI)


def protocol_for_mime(mime: str) -> VideoProtocol | None:
    """The registered protocol that carries ``mime``, or None."""
    normalised = (mime or "").split(";")[0].strip().lower()
    for protocol in _REGISTRY.values():
        if normalised in protocol.allowed_mimes:
            return protocol
    return None
