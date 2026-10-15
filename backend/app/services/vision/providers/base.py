"""Provider adapter interface.

A provider turns a prompt plus a set of already-privacy-rendered JPEGs into
validated text. It knows nothing about segments, database rows or review
requests: keeping that boundary is what lets the same adapter serve image
semantics (M5.1) and timestamped multi-frame motion review (M5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a circular import
    from ..authorization import AuthorizedModelContext


@dataclass
class ImagePart:
    """One image bound for the model.

    ``path`` is used to read bytes; ``label`` is the human/model-visible
    identifier (e.g. ``t=12.0s``) and must never be a local absolute path.
    """

    path: str
    label: str


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
