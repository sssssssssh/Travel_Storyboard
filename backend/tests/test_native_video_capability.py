"""TSB-325 — native-video capability contract and protocol registry.

The task book's P0 gate says a video channel may exist only after the endpoint's
video ability is *probed*, and that a successful HTTP call returning unrelated
text is **not** evidence of video support. These tests pin that down at the level
of the data contract, because the dangerous failure is not a crash: it is a
capability object that quietly claims more than was measured.

The recurring theme is "unknown must stay unknown". Several tests exist purely to
make sure that no code path substitutes a plausible default (10 MB, 30 s, "audio
probably ignored") for a number nobody observed — because downstream budget
checks would then enforce an invented limit and look authoritative doing it.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from app.services.vision import video_capabilities as vc


# --- the four honest states -------------------------------------------------

def test_default_state_is_unverified_not_permission():
    """Nothing measured means nothing may be sent."""
    caps = vc.VideoCapabilities()
    assert caps.state == vc.UNVERIFIED
    assert caps.verified is False
    assert caps.sendable is False, "unverified must not be a licence to try"


def test_only_verified_native_video_is_sendable():
    for state in vc.CAPABILITY_STATES:
        caps = vc.VideoCapabilities(state=state)
        assert caps.sendable is (state == vc.VERIFIED_NATIVE_VIDEO), state


def test_acceptance_without_temporal_evidence_is_not_sendable():
    """V04: the server took the video but told us nothing temporal."""
    caps = vc.VideoCapabilities(state=vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN, protocol="video_url_data_uri")
    assert caps.verified is False
    assert caps.sendable is False


def test_an_unknown_state_string_is_refused():
    with pytest.raises(ValueError):
        vc.VideoCapabilities(state="probably_fine")


# --- unknown is a value, never a guessed number ------------------------------

def test_limits_default_to_unknown_not_to_a_plausible_number():
    caps = vc.VideoCapabilities()
    assert caps.max_duration_sec is None
    assert caps.max_bytes is None
    assert caps.accepts_audio is None


def test_to_dict_names_the_unknown_fields_explicitly():
    """A consumer must be able to tell null from zero without reading docs."""
    data = vc.VideoCapabilities().to_dict()
    assert "max_bytes" in data["unknown_fields"]
    assert "max_duration_sec" in data["unknown_fields"]
    assert "accepts_audio" in data["unknown_fields"]


def test_a_measured_zero_is_not_reported_as_unknown():
    """``accepts_audio=False`` is knowledge; only None is unknown."""
    caps = vc.VideoCapabilities(state=vc.UNSUPPORTED, accepts_audio=False)
    data = caps.to_dict()
    assert "accepts_audio" not in data["unknown_fields"]
    assert data["accepts_audio"] is False


def test_unknown_reason_is_only_given_for_actually_unknown_fields():
    caps = vc.VideoCapabilities(max_bytes=5_000_000)
    assert vc.unknown_reason(caps, "max_bytes") == ""
    assert vc.unknown_reason(caps, "max_duration_sec") != ""


# --- verification requires evidence -----------------------------------------

def test_verified_requires_a_protocol_and_a_scope():
    """'The call succeeded' is not a verification scope."""
    with pytest.raises(ValueError):
        vc.record_verification(
            state=vc.VERIFIED_NATIVE_VIDEO,
            protocol="",
            model_id="m",
            verification_scope="it worked",
        )
    with pytest.raises(ValueError):
        vc.record_verification(
            state=vc.VERIFIED_NATIVE_VIDEO,
            protocol="video_url_data_uri",
            model_id="m",
            verification_scope="   ",
        )


def test_record_verification_stamps_time_and_keeps_unknowns_unknown():
    caps = vc.record_verification(
        state=vc.VERIFIED_NATIVE_VIDEO,
        protocol="video_url_data_uri",
        model_id="synthetic-model",
        verification_scope="3s synthetic clip; temporal order echoed back",
        now=datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
    )
    assert caps.verified is True
    assert caps.sendable is True
    assert caps.probe_time.startswith("2026-10-16T12:00")
    # Verified does NOT mean the limits were measured.
    assert caps.max_bytes is None
    assert caps.accepts_audio is None


def test_record_verification_rejects_an_unknown_state():
    with pytest.raises(ValueError):
        vc.record_verification(
            state="totally_verified",
            protocol="p",
            model_id="m",
            verification_scope="s",
        )


def test_unsupported_is_recordable_without_a_scope():
    """A failure is evidence too, and needs no success narrative."""
    caps = vc.record_verification(
        state=vc.UNSUPPORTED,
        protocol="video_url_data_uri",
        model_id="m",
        verification_scope="",
        detail={"http_status": 415},
    )
    assert caps.state == vc.UNSUPPORTED
    assert caps.sendable is False
    assert caps.detail["http_status"] == 415


def test_unverified_helper_never_produces_a_sendable_state():
    caps = vc.unverified_capabilities(model_id="m", reason="no authorised probe yet")
    assert caps.sendable is False
    assert caps.detail["reason"] == "no authorised probe yet"


# --- public projection must not leak or overclaim ---------------------------

def test_public_dict_reports_the_state_truthfully():
    caps = vc.record_verification(
        state=vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN,
        protocol="video_url_data_uri",
        model_id="m",
        verification_scope="accepted, sampling undisclosed",
    )
    public = caps.public_dict()
    assert public["verified"] is False
    assert public["sendable"] is False
    assert public["temporal_sampling_disclosed"] is False


def test_a_capability_object_has_no_credential_fields():
    """C5: capability reports must never carry a key or an endpoint path."""
    names = {f.name for f in dataclasses.fields(vc.VideoCapabilities)}
    for forbidden in ("api_key", "key", "token", "base_url", "url", "path"):
        assert forbidden not in names


# --- protocol registry -------------------------------------------------------

@pytest.fixture()
def clean_registry():
    saved = vc.registered_protocols()
    vc._reset_registry_for_tests()
    yield
    vc._reset_registry_for_tests()
    for proto in saved.values():
        vc.register_protocol(proto, replace=True)


def test_the_builtin_protocol_is_registered_and_carries_mp4():
    proto = vc.get_protocol("video_url_data_uri")
    assert "video/mp4" in proto.allowed_mimes
    assert proto.fetches_remote is False, (
        "an inline data URI is not fetched by the server; a fetching protocol "
        "would need SSRF handling"
    )


def test_protocol_selection_is_by_registration_not_by_model_name(clean_registry):
    """The anti-pattern the task book names: sniffery on the model string."""
    vc.register_protocol(
        vc.VideoProtocol(
            name="vendor_file_id",
            allowed_mimes=frozenset({"video/mp4", "video/quicktime"}),
            transport="files_api",
            fetches_remote=False,
        )
    )
    assert vc.protocol_for_mime("video/quicktime").name == "vendor_file_id"
    assert vc.protocol_for_mime("video/mp4") is not None


def test_a_duplicate_protocol_name_is_refused(clean_registry):
    proto = vc.VideoProtocol(
        name="dup", allowed_mimes=frozenset({"video/mp4"}), transport="x"
    )
    vc.register_protocol(proto)
    with pytest.raises(ValueError):
        vc.register_protocol(proto)
    vc.register_protocol(proto, replace=True)  # explicit replacement is allowed


def test_a_protocol_without_mime_types_is_refused(clean_registry):
    with pytest.raises(ValueError):
        vc.register_protocol(
            vc.VideoProtocol(name="empty", allowed_mimes=frozenset(), transport="x")
        )


def test_an_unregistered_protocol_raises_rather_than_guessing():
    with pytest.raises(KeyError):
        vc.get_protocol("not_a_protocol")


def test_mime_lookup_is_case_and_parameter_insensitive():
    assert vc.protocol_for_mime("VIDEO/MP4; codecs=h264") is not None
    assert vc.protocol_for_mime("video/webm") is None
    assert vc.protocol_for_mime("") is None
