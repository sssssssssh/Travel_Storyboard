"""Vision capability declaration.

Capabilities are *declared by configuration* and separately *verifiable* by a
call. A declared capability is what the caller is allowed to attempt; a probe
result is what was actually observed. The distinction matters because HTTP 200
on a text prompt says nothing about image support (task book §3): the only way
to claim ``supports_images`` is to send an image and get a real answer back.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from ...config import get_settings


@dataclass(frozen=True)
class VisionCapabilities:
    supports_images: bool = True
    supports_multi_image: bool = True
    supports_json_schema: bool = False
    supports_native_video: bool = False

    #: Non-capability guard rails, kept alongside so callers read one object.
    max_images: int = 4
    max_request_bytes: int = 8_000_000
    timeout_sec: float = 120.0
    max_calls_per_job: int = 200

    def to_dict(self) -> dict:
        return asdict(self)


def declared_capabilities() -> VisionCapabilities:
    """Capabilities as configured.

    ``supports_native_video`` stays ``False`` unless an operator explicitly opts
    in, because assuming every OpenAI-compatible gateway accepts MP4 is exactly
    the mistake the task book warns against (M5.2 default is timestamped
    multi-frame, not native video).
    """
    s = get_settings()
    return VisionCapabilities(
        supports_images=True,
        supports_multi_image=True,
        supports_json_schema=s.model_supports_json_schema,
        supports_native_video=s.model_supports_native_video,
        max_images=max(1, s.model_max_images),
        max_request_bytes=max(1, s.model_max_request_bytes),
        timeout_sec=max(1.0, s.model_timeout_sec),
        max_calls_per_job=max(1, s.model_max_calls_per_job),
    )


def assert_images_supported(caps: VisionCapabilities, image_count: int) -> None:
    """Raise a classified error *before* spending a request on an impossible one."""
    from .errors import MODEL_CAPABILITY_UNSUPPORTED, ModelError

    if image_count <= 0:
        raise ModelError(MODEL_CAPABILITY_UNSUPPORTED, "vision call needs at least one image")
    if not caps.supports_images:
        raise ModelError(MODEL_CAPABILITY_UNSUPPORTED, "configured provider declares no image support")
    if image_count > 1 and not caps.supports_multi_image:
        raise ModelError(
            MODEL_CAPABILITY_UNSUPPORTED,
            f"provider declares no multi-image support (asked for {image_count})",
        )
    if image_count > caps.max_images:
        raise ModelError(
            MODEL_CAPABILITY_UNSUPPORTED,
            f"image count {image_count} exceeds configured limit {caps.max_images}",
        )
