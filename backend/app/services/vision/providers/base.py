"""Provider adapter interface.

A provider turns a prompt plus a set of already-privacy-rendered JPEGs into
validated text. It knows nothing about segments, database rows or review
requests: keeping that boundary is what lets the same adapter serve image
semantics (M5.1) and timestamped multi-frame motion review (M5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from ..authorization import AuthorizedModelContext


@dataclass
class ImagePart:
    """One image bound for the model.

    Exactly one of ``path`` / ``data`` must be set:

    * ``path`` — a file this process wrote (the M5 renderers). Read at send time.
    * ``data`` — raw JPEG/PNG **bytes** already validated in memory. This exists
      so the legacy ``image_message()`` path can hand its decoded payload
      straight to the provider (TSB-313); before this, the compat layer spilled
      each data URI to a temp file and never deleted it, leaving a copy of the
      frame on disk after every call — including failed and cancelled ones.

    ``label`` is the human/model-visible identifier (e.g. ``t=12.0s``) and must
    never be a local absolute path.
    """

    path: str = ""
    label: str = ""
    data: bytes | None = None
    mime: str = ""

    def __post_init__(self) -> None:
        if not self.path and self.data is None:
            raise ValueError("ImagePart needs either a path or in-memory data")
        if self.path and self.data is not None:
            raise ValueError("ImagePart takes a path or data, not both")

    @property
    def size_bytes(self) -> int:
        """Byte size without reading the file twice."""
        return len(self.data) if self.data is not None else Path(self.path).stat().st_size


@dataclass
class VisionRequest:
    prompt: str
    images: list[ImagePart]
    system: str = ""
    json_object: bool = False
    max_tokens: int = 2048
    temperature: float = 0.2
    #: TSB-312: proof that this specific call was authorised to send frames.
    #: The provider refuses a request without it, so no code path can reach the
    #: network by bypassing the business entry point.
    authorization: "AuthorizedModelContext | None" = None


@dataclass
class VisionResponse:
    text: str
    model_id: str = ""
    #: Token usage when the provider reports it; empty dict otherwise (never
    #: invented — the task book requires usage to be recorded only if present).
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    request_id: str = ""
    attempts: int = 1


class VisionProvider(Protocol):
    """Contract implemented by every provider adapter."""

    name: str

    def complete(
        self,
        request: VisionRequest,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VisionResponse: ...


@dataclass
class VideoPart:
    """A short derived video clip bound for the model (TSB-326).

    Deliberately **not** an ``ImagePart``. The task book forbids forcing video
    into the image type, and the distinction is load-bearing: video carries a time
    axis, a duration and a mapping back to source coordinates, and it is orders of
    magnitude larger. Merging the two would let an image code path accidentally
    transmit a video (and vice versa) with no type error to stop it.

    ``clip_id`` is server-issued and travels in the prompt so the model's answer
    can be bound back to this exact clip; ``source_offset_sec`` records where in
    the *source asset* the clip begins, which is what makes an event window
    expressible in absolute seconds later (TSB-328).
    """

    #: Server-issued identifier for this derived clip. Never a filesystem path.
    clip_id: str
    #: In-memory bytes of the derived clip. Held in memory on purpose: TSB-313
    #: established that spilling media to a temp file leaks it on some exit path.
    data: bytes
    mime_type: str = "video/mp4"
    #: Where this clip starts inside its source asset, in absolute seconds.
    source_offset_sec: float = 0.0
    #: Duration of the clip itself.
    duration_sec: float = 0.0
    #: The segment this clip was cut from (server-issued).
    segment_id: str = ""
    #: Whether the derived clip still carries an audio track. Expected False.
    has_audio: bool = False
    #: Optional human/model-visible label. Must never be a local path.
    label: str = ""

    def __post_init__(self) -> None:
        if not self.clip_id:
            raise ValueError("VideoPart needs a clip_id")
        if not self.data:
            raise ValueError("VideoPart needs clip bytes")
        if self.has_audio:
            # Derived clips are silent by default; an audio-bearing clip would be
            # a privacy and budget surprise, so it must be opted into explicitly
            # by a caller that knows why.
            raise ValueError(
                "derived clips must be silent; refusing a VideoPart with has_audio=True"
            )
        if self.source_offset_sec < 0 or self.duration_sec < 0:
            raise ValueError("video times must not be negative")

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    @property
    def source_end_sec(self) -> float:
        return self.source_offset_sec + self.duration_sec


@dataclass
class VideoRequest:
    """One video understanding call.

    Carries the same ``authorization`` token as :class:`VisionRequest` so the
    provider layer has a single chokepoint, and the capability record that was
    actually verified, so a provider cannot send video on a ``False`` config flag
    alone.
    """

    prompt: str
    video: VideoPart
    system: str = ""
    json_object: bool = False
    max_tokens: int = 2048
    temperature: float = 0.2
    authorization: "AuthorizedModelContext | None" = None
    #: The protocol chosen by the capability probe. Empty means "not chosen",
    #: which the provider refuses rather than defaulting to a guess.
    protocol: str = ""


@dataclass
class VideoResponse:
    """The model's answer about one clip, plus what it cost."""

    text: str
    clip_id: str = ""
    model_id: str = ""
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    request_id: str = ""
    attempts: int = 1
    #: Bytes actually placed on the wire for the video payload.
    sent_bytes: int = 0
    #: True when the server told us how it sampled; never inferred from success.
    sampling_disclosed: bool = False


class VideoProvider(Protocol):
    """Contract for a native-video-capable adapter."""

    name: str

    def complete_video(
        self,
        request: VideoRequest,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> VideoResponse: ...
