"""TSB-328 — temporal-event schema validation.

Pure logic, no database and no network: this is where a model's answer stops
being prose and becomes a candidate window attached to a person's footage. The
cases that matter are the ones where a *plausible* answer is wrong -- an event
about a different clip, a window just past the edit boundary, a confident claim
about audio in a silent file.

Task book rows V12 (fabricated ids / out of range / NaN) and V21 (no audio
capability) are covered here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import (
    CLIP_MISMATCH,
    INVALID_OUTPUT,
    NO_EVENTS,
    VIDEO_SCHEMA_VERSION,
    VideoSchemaError,
    build_prompt,
    extract_json_object,
    validate_temporal_events,
)


def _clip(*, start: float = 10.0, duration: float = 5.0, segment_id: str = "seg-1") -> ClipArtifact:
    return ClipArtifact(
        path=Path("clip.mp4"),
        segment_id=segment_id,
        asset_id="asset-1",
        source_clip_start_sec=start,
        source_clip_end_sec=start + duration,
        duration_sec=duration,
        width=1280, height=720, rotation=0, vfr=False, has_audio=False,
        size_bytes=1000, sha256="abc", container="mp4", color_unverified=False,
    )


def _response(**overrides) -> dict:
    base = {
        "segment_id": "seg-1",
        "clip_id": "seg-1",
        "summary": "A brief movement then stillness.",
        "events": [
            {
                "event_type": "subject_action",
                "start_rel_sec": 1.0,
                "end_rel_sec": 2.5,
                "description": "a visible object moves quickly",
                "confidence": 0.7,
                "evidence": ["video_window"],
                "limitations": [],
            }
        ],
        "observed_temporal_quality": "limited",
        "audio_analyzed": False,
        "limitations": ["clip is silent"],
    }
    base.update(overrides)
    return base


def _validate(item, clip=None, **kwargs):
    clip = clip or _clip()
    return validate_temporal_events(
        item,
        clip=clip,
        segment_start_sec=kwargs.pop("segment_start_sec", clip.source_clip_start_sec),
        segment_end_sec=kwargs.pop("segment_end_sec", clip.source_clip_end_sec),
        **kwargs,
    )


# --- happy path -------------------------------------------------------------

def test_a_well_formed_response_is_accepted():
    result = _validate(_response())
    assert result.segment_id == "seg-1"
    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "subject_action"
    assert event.start_rel_sec == 1.0
    assert event.confidence == 0.7


def test_events_carry_both_relative_and_absolute_times():
    """The absolute window is what the UI and M5 will need."""
    result = _validate(_response())
    event = result.events[0]
    assert event.start_abs_sec == pytest.approx(11.0)
    assert event.end_abs_sec == pytest.approx(12.5)


def test_the_stored_dict_labels_confidence_as_uncalibrated():
    payload = _validate(_response()).to_dict()
    assert payload["events"][0]["confidence_is_calibrated"] is False
    assert payload["schema_version"] == VIDEO_SCHEMA_VERSION


# --- identity binding (C3) --------------------------------------------------

def test_a_response_about_another_clip_is_rejected():
    """V12: never re-map a model's answer onto a different shot."""
    with pytest.raises(VideoSchemaError) as exc:
        _validate(_response(clip_id="some-other-clip"))
    assert exc.value.code == CLIP_MISMATCH
    assert "some-other-clip" in str(exc.value)


def test_a_response_about_another_segment_is_rejected():
    with pytest.raises(VideoSchemaError) as exc:
        _validate(_response(segment_id="seg-999"))
    assert exc.value.code == CLIP_MISMATCH


def test_an_omitted_clip_id_is_accepted_because_the_prompt_bound_it():
    """Some models echo no ids; the call itself scoped the answer."""
    item = _response()
    item.pop("clip_id")
    item.pop("segment_id")
    assert _validate(item).clip_id == "seg-1"


# --- envelope failures ------------------------------------------------------

def test_a_non_object_response_is_rejected():
    with pytest.raises(VideoSchemaError) as exc:
        _validate("not json at all")
    assert exc.value.code == INVALID_OUTPUT


def test_a_response_with_nothing_usable_is_rejected():
    with pytest.raises(VideoSchemaError) as exc:
        _validate({"summary": "", "events": []})
    assert exc.value.code == INVALID_OUTPUT


def test_a_non_list_events_field_is_rejected():
    with pytest.raises(VideoSchemaError) as exc:
        _validate(_response(events="nope"))
    assert exc.value.code == INVALID_OUTPUT


def test_a_response_where_every_event_is_invalid_is_rejected_not_emptied():
    """V12/C6: an all-invalid batch must not become a silent empty success."""
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 99, "end_rel_sec": 100,
         "description": "way outside the clip"},
    ])
    with pytest.raises(VideoSchemaError) as exc:
        _validate(item)
    assert exc.value.code == NO_EVENTS
    assert exc.value.detail["dropped"]


# --- individual event failures are dropped and reported ---------------------

def test_an_out_of_range_event_is_dropped_and_reported():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "fine"},
        {"event_type": "subject_action", "start_rel_sec": 40, "end_rel_sec": 41,
         "description": "beyond the clip"},
    ])
    result = _validate(item)
    assert len(result.events) == 1
    assert len(result.dropped) == 1
    # The reason is a stable code, not a prose sentence, so callers can branch on it.
    assert result.dropped[0]["reason"] in {"SEGMENT_RANGE_INVALID", "out of range"}


def test_a_nan_event_is_dropped():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": float("nan"), "end_rel_sec": 2,
         "description": "nan start"},
    ])
    with pytest.raises(VideoSchemaError):
        _validate(item)


def test_an_inverted_event_is_dropped():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 3, "end_rel_sec": 1,
         "description": "backwards"},
    ])
    with pytest.raises(VideoSchemaError):
        _validate(item)


def test_an_unknown_event_type_is_dropped():
    """A closed vocabulary keeps downstream rules interpretable."""
    item = _response(events=[
        {"event_type": "definitely_not_real", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "invented type"},
    ])
    with pytest.raises(VideoSchemaError) as exc:
        _validate(item)
    assert exc.value.code == NO_EVENTS
    assert "unknown event_type" in exc.value.detail["dropped"][0]["reason"]


def test_an_event_without_a_description_is_dropped():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "   "},
    ])
    with pytest.raises(VideoSchemaError):
        _validate(item)


def test_an_identity_claim_in_an_event_is_dropped():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "this is John Smith on holiday"},
    ])
    with pytest.raises(VideoSchemaError):
        _validate(item)


def test_a_reasonable_description_survives_the_identity_filter():
    """The filter must not be so eager that real observations vanish."""
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "a red kite lifts off the sand"},
    ])
    assert len(_validate(item).events) == 1


def test_a_non_object_event_is_dropped():
    item = _response(events=["not an event", {"event_type": "subject_action",
                                              "start_rel_sec": 1, "end_rel_sec": 2,
                                              "description": "real one"}])
    result = _validate(item)
    assert len(result.events) == 1
    assert len(result.dropped) == 1


def test_an_empty_events_list_with_a_summary_is_acceptable():
    """Saying 'nothing visible changed' is a legitimate answer."""
    result = _validate(_response(events=[], summary="nothing changes in this clip"))
    assert result.events == []
    assert result.summary


# --- honesty about audio and confidence ------------------------------------

def test_audio_analysis_is_forced_false_even_if_the_model_claims_it():
    """V21: the clip is silent, so any audio claim is false by construction."""
    item = _response(audio_analyzed=True)
    result = _validate(item)
    assert result.audio_analyzed is False
    assert any("claimed audio" in text for text in result.limitations), (
        "the false claim must be recorded, not silently discarded"
    )


def test_confidence_is_clamped_into_range():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "very confident", "confidence": 7.5},
        {"event_type": "camera_motion", "start_rel_sec": 2, "end_rel_sec": 3,
         "description": "negative", "confidence": -3},
    ])
    result = _validate(item)
    assert result.events[0].confidence == 1.0
    assert result.events[1].confidence == 0.0


def test_a_non_numeric_confidence_becomes_zero_not_a_guess():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "text confidence", "confidence": "very high"},
    ])
    assert _validate(item).events[0].confidence == 0.0


def test_an_unknown_temporal_quality_word_is_not_promoted():
    item = _response(observed_temporal_quality="excellent_beyond_doubt")
    assert _validate(item).observed_temporal_quality == "unknown"


def test_a_known_temporal_quality_is_kept():
    item = _response(observed_temporal_quality="good")
    assert _validate(item).observed_temporal_quality == "good"


def test_an_event_without_evidence_defaults_to_video_window():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "no evidence field"},
    ])
    assert _validate(item).events[0].evidence == ["video_window"]


def test_unknown_evidence_kinds_are_discarded():
    item = _response(events=[
        {"event_type": "subject_action", "start_rel_sec": 1, "end_rel_sec": 2,
         "description": "with junk evidence", "evidence": ["telepathy", "video_window"]},
    ])
    assert _validate(item).events[0].evidence == ["video_window"]


def test_the_event_cap_is_enforced():
    events = [
        {"event_type": "subject_action", "start_rel_sec": 0.1 * i,
         "end_rel_sec": 0.1 * i + 0.05, "description": f"event {i}"}
        for i in range(60)
    ]
    result = _validate(_response(events=events))
    assert len(result.events) <= 30


# --- JSON recovery ----------------------------------------------------------

def test_json_is_extracted_from_a_fenced_block():
    raw = 'Here you go:\n```json\n{"summary": "x"}\n```\nDone.'
    assert extract_json_object(raw) == {"summary": "x"}


def test_json_is_extracted_from_prose():
    raw = 'Sure: {"summary": "x", "events": []} hope that helps'
    assert extract_json_object(raw) == {"summary": "x", "events": []}


def test_nested_braces_are_handled():
    raw = 'prefix {"a": {"b": 1}, "c": 2} suffix'
    assert extract_json_object(raw) == {"a": {"b": 1}, "c": 2}


def test_garbage_yields_an_empty_object():
    assert extract_json_object("no json here") == {}
    assert extract_json_object("") == {}
    assert extract_json_object("[1,2,3]") == {}


def test_a_json_string_containing_braces_does_not_confuse_the_scanner():
    raw = '{"summary": "a } brace inside a string", "events": []}'
    assert extract_json_object(raw)["summary"] == "a } brace inside a string"


# --- prompt -----------------------------------------------------------------

def test_the_prompt_states_the_clip_origin_for_times():
    text = build_prompt(clip_id="c1", segment_id="s1", duration_sec=5.0)
    assert "FROM THE START OF THE CLIP" in text
    assert "c1" in text and "s1" in text


def test_the_prompt_forbids_audio_claims_and_identity():
    text = build_prompt(clip_id="c1", segment_id="s1", duration_sec=5.0)
    assert "NO audio track" in text
    assert "identify people" in text.lower() or "Do not identify" in text


def test_the_prompt_lists_the_closed_event_vocabulary():
    text = build_prompt(clip_id="c1", segment_id="s1", duration_sec=5.0)
    assert "subject_action" in text and "other" in text


def test_the_prompt_permits_an_empty_answer():
    """Otherwise the model invents events to satisfy the schema."""
    text = build_prompt(clip_id="c1", segment_id="s1", duration_sec=5.0)
    assert "empty events list is an acceptable answer" in text


def test_the_prompt_says_confidence_is_uncalibrated():
    text = build_prompt(clip_id="c1", segment_id="s1", duration_sec=5.0)
    assert "uncalibrated" in text
