"""TSB-313 - the legacy ``model_client`` compat path must not leak frames.

Before this change, ``model_client._flatten`` handled an inline ``data:`` URI by
writing the decoded bytes to ``<tempdir>/tsb-vision-<uuid>.jpg`` and handing the
**path** to the provider. That file was never deleted, so every
``POST /story/analyze`` call left a copy of a user's frame on disk - in ``/tmp``
or ``%TEMP%`` - on the success path *and* on every failure path. It survived the
process that made it (C5: frames are only supposed to travel inside an explicit,
acknowledged upload).

The regression is observed, not assumed: the first test counts the files the
temp directory actually gains across a real ``chat()`` call, and it fails against
the old implementation, which gained exactly one ``tsb-vision-*.jpg``.

The remaining tests pin the contract that replaced it, because "no temp file" is
only an improvement if the image still reaches the wire - and reaches it as the
right bytes, with a type that matches its content.
"""

from __future__ import annotations

import ast
import base64
import glob
import inspect
import os
import tempfile
import textwrap

import pytest

from app.services import model_client
from app.services import vision as vision_pkg
from app.services.vision.authorization import authorize_upload
from app.services.vision.errors import MODEL_BAD_REQUEST, MODEL_UPLOAD_DENIED, ModelError
from app.services.vision.providers.base import ImagePart, VisionRequest

try:  # TSB-313 helpers; absent on the pre-fix revision, which is the point.
    from app.services.vision.providers.openai_compatible import (
        MAX_INLINE_IMAGE_BYTES,
        sniff_mime,
        validate_inline_image,
    )
except ImportError:  # pragma: no cover - only on the pre-fix revision
    MAX_INLINE_IMAGE_BYTES = None
    sniff_mime = validate_inline_image = None

JPEG = b"\xff\xd8\xff\xe0" + b"jpegbody" * 4
PNG = b"\x89PNG\r\n\x1a\n" + b"pngbody" * 4


def _data_uri(payload: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _temp_leftovers() -> set[str]:
    """Frame copies the compat layer would have left behind."""
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "tsb-vision-*")))


class _Captured:
    """A provider stub that records the request it was handed."""

    name = "capture"

    def __init__(self) -> None:
        self.requests: list[VisionRequest] = []

    def complete(self, request, *, cancel_check=None):
        self.requests.append(request)

        class _R:
            text = "{}"
            model_id = "stub"
            usage: dict = {}
            latency_ms = 0
            request_id = "stub"
            attempts = 1

        return _R()


@pytest.fixture()
def wired(monkeypatch):
    """Wire a capturing provider and enable the compat path's config gate."""
    cap = _Captured()
    monkeypatch.setattr(model_client, "is_configured", lambda: True)
    monkeypatch.setattr(vision_pkg, "get_provider", lambda: cap)
    return cap


def _authorization():
    return authorize_upload(
        project_id="p-313", upload_acknowledged=True, purpose="story_analyze"
    )


def _enable_model(monkeypatch):
    """Satisfy the shared boundary: global switch on + endpoint + key."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(settings, "model_api_key", "test-key", raising=False)


# --- the regression ---------------------------------------------------------

def test_chat_leaves_no_frame_copy_in_the_temp_directory(monkeypatch, wired):
    """The observable defect: a decoded data URI used to become a temp file.

    Counting the temp directory before and after is the point - asserting only
    that the request "succeeded" cannot see this bug, because both the old and
    the new implementation succeed.
    """
    _enable_model(monkeypatch)
    before = _temp_leftovers()

    messages = [
        {"role": "system", "content": "json only"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": _data_uri(JPEG)}},
            ],
        },
    ]
    out = model_client.chat(messages, authorization=_authorization())

    assert out == "{}"
    assert _temp_leftovers() == before, (
        "the compat path wrote a copy of the frame into the temp directory; "
        "it must never touch the filesystem"
    )
    # And the image did reach the provider (no silent drop, C6).
    assert len(wired.requests) == 1
    assert len(wired.requests[0].images) == 1


def test_the_image_survives_as_in_memory_bytes(monkeypatch, wired):
    """Not writing a temp file is only correct if the payload still travels."""
    _enable_model(monkeypatch)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": _data_uri(JPEG)}},
            ],
        }
    ]
    model_client.chat(messages, authorization=_authorization())

    image = wired.requests[0].images[0]
    assert image.data == JPEG
    assert image.path == ""
    assert image.mime == "image/jpeg"


def test_an_error_path_also_leaves_nothing_behind(monkeypatch):
    """The old leak happened on failure too, so failure is asserted separately."""
    _enable_model(monkeypatch)
    before = _temp_leftovers()

    class _Boom:
        name = "boom"

        def complete(self, request, *, cancel_check=None):
            raise ModelError(MODEL_BAD_REQUEST, "provider rejected the image")

    monkeypatch.setattr(vision_pkg, "get_provider", lambda: _Boom())

    messages = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": _data_uri(JPEG)}}],
        }
    ]
    with pytest.raises(ModelError):
        model_client.chat(messages, authorization=_authorization())

    assert _temp_leftovers() == before


# --- the contract that replaced it ------------------------------------------

def test_a_malformed_data_uri_is_reported_not_silently_dropped():
    """A dropped image would send a prompt the model cannot answer (C6)."""
    with pytest.raises(ModelError) as exc:
        model_client.decode_data_uri("data:image/jpeg;base64,!!!not-base64!!!")
    assert exc.value.code == MODEL_BAD_REQUEST

    with pytest.raises(ModelError) as exc:
        model_client.decode_data_uri("data:image/jpeg,raw-not-base64")
    assert exc.value.code == MODEL_BAD_REQUEST

    with pytest.raises(ModelError) as exc:
        model_client.decode_data_uri("https://example.com/a.jpg")
    assert exc.value.code == MODEL_BAD_REQUEST


def test_an_oversized_data_uri_is_refused_before_decoding():
    """The ceiling is checked on the string, so decoding cannot balloon memory."""
    huge = "data:image/jpeg;base64," + "A" * (model_client.MAX_DATA_URI_CHARS + 4)
    with pytest.raises(ModelError) as exc:
        model_client.decode_data_uri(huge)
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "too large" in str(exc.value)


def test_a_plain_path_is_still_accepted_as_a_path():
    """Regression guard: only inline data URIs changed transport."""
    _system, _text, images = model_client._flatten(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "C:/frames/a.jpg"}}
                ],
            }
        ]
    )
    assert len(images) == 1
    assert images[0].path == "C:/frames/a.jpg"
    assert images[0].data is None


def test_an_empty_image_url_is_skipped_without_inventing_an_image():
    _system, _text, images = model_client._flatten(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]
    )
    assert images == []


# --- ImagePart now has two mutually exclusive carriers ----------------------

def test_image_part_refuses_a_part_with_no_payload():
    with pytest.raises(ValueError):
        ImagePart(label="t=1s")


def test_image_part_refuses_both_carriers_at_once():
    with pytest.raises(ValueError):
        ImagePart(path="C:/a.jpg", data=JPEG, label="t=1s")


def test_image_part_keeps_the_old_positional_constructor_working():
    """``ImagePart(path, label)`` is used by the M5 renderers; it must not break."""
    part = ImagePart("C:/a.jpg", "t=1s")
    assert part.path == "C:/a.jpg"
    assert part.label == "t=1s"
    assert part.data is None


def test_image_part_size_comes_from_memory_when_there_is_no_file():
    assert ImagePart(data=JPEG, label="t=1s").size_bytes == len(JPEG)


# --- content-based validation ----------------------------------------------

@pytest.mark.parametrize(
    "payload,expected",
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ],
)
def test_sniff_mime_reads_the_magic_bytes(payload, expected):
    assert sniff_mime(payload) == expected


def test_sniff_mime_returns_none_for_a_non_image():
    assert sniff_mime(b"GIF89a" + b"x" * 20) is None
    assert sniff_mime(b"<html>nope</html>") is None
    assert sniff_mime(b"") is None


def test_a_forged_mime_is_refused_by_content():
    """Declaring PNG while sending a JPEG must not reach the wire."""
    with pytest.raises(ModelError) as exc:
        validate_inline_image(JPEG, "image/png")
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "does not match" in str(exc.value)


def test_a_non_image_payload_is_refused():
    with pytest.raises(ModelError) as exc:
        validate_inline_image(b"<html>not an image</html>", "image/jpeg")
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "magic bytes" in str(exc.value)


def test_an_empty_payload_is_refused():
    with pytest.raises(ModelError) as exc:
        validate_inline_image(b"", "image/jpeg")
    assert exc.value.code == MODEL_BAD_REQUEST


def test_an_oversized_inline_payload_is_refused():
    big = JPEG + b"x" * MAX_INLINE_IMAGE_BYTES
    with pytest.raises(ModelError) as exc:
        validate_inline_image(big, "image/jpeg")
    assert exc.value.code == MODEL_BAD_REQUEST
    assert "exceeds the inline limit" in str(exc.value)


def test_the_declared_type_is_returned_when_it_agrees():
    assert validate_inline_image(JPEG, "image/jpeg; charset=binary") == "image/jpeg"
    assert validate_inline_image(PNG) == "image/png"


# --- authorization wiring ---------------------------------------------------

def test_the_compat_entry_point_cannot_be_used_without_a_context(monkeypatch, wired):
    """A direct call must not be a way around the business entry point."""
    _enable_model(monkeypatch)
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    with pytest.raises(ModelError) as exc:
        model_client.chat(messages)
    assert exc.value.code == MODEL_UPLOAD_DENIED
    assert wired.requests == [], "no request may reach the provider unguarded"


def test_the_flattened_request_carries_the_authorization(monkeypatch, wired):
    _enable_model(monkeypatch)
    context = _authorization()
    model_client.chat(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        authorization=context,
    )
    assert wired.requests[0].authorization is context


def test_direct_analyze_passes_the_token_through_to_chat():
    """Wiring test: the caller must actually forward it (the TSB-253 lesson).

    Asserting that the helper accepts an argument is not enough - a parameter the
    production call site never passes changes nothing.
    """
    from app.services import story_planner

    source = inspect.getsource(story_planner.direct_analyze)
    assert source.count("authorization=authorization") >= 2, (
        "both the first call and the JSON retry must forward the authorization token"
    )


def test_the_legacy_route_builds_the_context_once_and_forwards_it():
    """The route must authorise before calling, and hand the token over."""
    from app.api import story

    source = inspect.getsource(story.analyze_story)
    # Strip the docstring: it explains the old behaviour in prose, so scanning
    # the raw source would match text that is not executable code.
    tree = ast.parse(textwrap.dedent(source))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))

    assert "authorize_upload(" in code, "the route must use the shared boundary"
    assert "authorization=context" in code, "the context must reach direct_analyze"
    assert "is_configured" not in code, (
        "the route must not re-derive permission from configuration"
    )


def test_the_route_reports_a_stable_code_and_a_migration_hint(monkeypatch):
    """A refusal must be actionable: stable code plus where to migrate."""
    _enable_model(monkeypatch)
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "off", raising=False)

    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import get_token

    with TestClient(
        app,
        base_url="http://127.0.0.1:8737",
        headers={"X-Access-Token": get_token()},
    ) as client:
        created = client.post("/api/projects", json={"name": "tsb313", "source_roots": []})
        pid = created.json()["id"]

        res = client.post(
            f"/api/projects/{pid}/story/analyze",
            json={"upload_acknowledged": True},
        )

    assert res.status_code == 403
    detail = res.json()["detail"]
    assert detail["error_code"] == MODEL_UPLOAD_DENIED
    assert "vision_analyze" in detail["migration_hint"]


def test_no_production_call_site_reads_the_removed_temp_helper():
    """The old helper must be gone, not merely unused."""
    assert not hasattr(model_client, "_materialize_data_uri")
