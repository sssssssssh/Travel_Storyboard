"""TSB-303 — shot-semantics schema, prompt and evidence validation.

The important property under test is that the model is treated as untrusted
input. Every case here is something a real model has been observed to do:
invent ids, wrap JSON in prose, over-claim, or answer a question it cannot
possibly know (a person's name). Each must be rejected or normalised *visibly*.
"""

from __future__ import annotations

import json

import pytest

from app.services.vision import schema as S
from app.services.vision.errors import MODEL_INVALID_OUTPUT


def _allowed() -> dict[str, S.AllowedSegment]:
    return {
        "seg-1": S.AllowedSegment(
            segment_id="seg-1",
            asset_id="asset-1",
            start_sec=10.0,
            end_sec=20.0,
            frame_ids=["frame-a", "frame-b"],
        ),
        "seg-2": S.AllowedSegment(
            segment_id="seg-2",
            asset_id="asset-1",
            start_sec=30.0,
            end_sec=33.0,
            frame_ids=["frame-c"],
        ),
    }


def _payload(**over) -> str:
    item = {
        "segment_id": "seg-1",
        "visual": {
            "scene_tags": ["water", "nature"],
            "subjects": ["wooden pier", "pedal boats"],
            "visible_actions": ["people walking"],
            "lighting": "daylight",
            "composition": "wide",
            "mood": ["calm"],
        },
        "evidence": {
            "state": "observed",
            "observed_confidence": 0.72,
            "frame_ids": ["frame-a", "frame-b"],
            "limitations": ["single_frame_cannot_prove_camera_motion"],
        },
        "review": {"needs_motion_review": True, "reasons": ["insufficient_temporal_evidence"]},
    }
    item.update(over)
    return json.dumps({"results": [item]})


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_valid_response_is_accepted_and_keeps_server_ids():
    accepted, problems = S.validate_response(_payload(), _allowed())
    assert problems == []
    assert len(accepted) == 1
    got = accepted[0]
    assert got.segment_id == "seg-1"
    # asset_id is taken from the server's whitelist, not from the model output.
    assert got.asset_id == "asset-1"
    assert got.visual["scene_tags"] == ["water", "nature"]
    assert got.evidence["state"] == "observed"
    assert got.review["needs_motion_review"] is True


def test_model_supplied_asset_id_is_ignored():
    """A model must not be able to retarget a shot to another asset."""
    payload = _payload(asset_id="asset-EVIL")
    accepted, _ = S.validate_response(payload, _allowed())
    assert accepted[0].asset_id == "asset-1"


# --------------------------------------------------------------------------
# fabricated identifiers (A05)
# --------------------------------------------------------------------------


def test_fabricated_segment_id_is_rejected():
    accepted, problems = S.validate_response(_payload(segment_id="seg-invented"), _allowed())
    assert accepted == []
    assert len(problems) == 1
    assert problems[0]["code"] == MODEL_INVALID_OUTPUT
    assert "not in the supplied manifest" in problems[0]["message"]


def test_missing_segment_id_is_rejected():
    payload = json.dumps({"results": [{"visual": {}}]})
    accepted, problems = S.validate_response(payload, _allowed())
    assert accepted == []
    assert "missing segment_id" in problems[0]["message"]


def test_unknown_frame_ids_are_dropped_not_trusted():
    payload = _payload()
    data = json.loads(payload)
    data["results"][0]["evidence"]["frame_ids"] = ["frame-a", "frame-FAKE"]
    accepted, problems = S.validate_response(json.dumps(data), _allowed())
    assert problems == []
    assert accepted[0].evidence["frame_ids"] == ["frame-a"]
    assert "dropped_unknown_frame_ids" in accepted[0].warnings


def test_all_unknown_frame_ids_fall_back_to_server_frames():
    data = json.loads(_payload())
    data["results"][0]["evidence"]["frame_ids"] = ["nope-1", "nope-2"]
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert accepted[0].evidence["frame_ids"] == ["frame-a", "frame-b"]


def test_duplicate_segment_output_is_flagged():
    item = json.loads(_payload())["results"][0]
    accepted, problems = S.validate_response(json.dumps({"results": [item, item]}), _allowed())
    assert len(accepted) == 1
    assert len(problems) == 1
    assert "duplicate" in problems[0]["message"]


def test_one_bad_entry_does_not_discard_the_good_ones():
    good = json.loads(_payload())["results"][0]
    bad = {"segment_id": "inv-1", "visual": {}}
    accepted, problems = S.validate_response(json.dumps({"results": [good, bad]}), _allowed())
    assert [a.segment_id for a in accepted] == ["seg-1"]
    assert len(problems) == 1


# --------------------------------------------------------------------------
# JSON recovery / malformed output
# --------------------------------------------------------------------------


def test_json_fences_are_recovered():
    fenced = "```json\n" + _payload() + "\n```"
    accepted, _ = S.validate_response(fenced, _allowed())
    assert len(accepted) == 1


def test_prose_before_json_is_recovered():
    noisy = "Here is my analysis:\n" + _payload() + "\nHope that helps!"
    accepted, _ = S.validate_response(noisy, _allowed())
    assert len(accepted) == 1


@pytest.mark.parametrize("raw", ["", "   ", "no json here at all", "[1,2,3]"])
def test_unusable_output_is_rejected(raw):
    with pytest.raises(S.SemanticValidationError) as ei:
        S.validate_response(raw, _allowed())
    assert ei.value.code == MODEL_INVALID_OUTPUT


def test_results_missing_entirely_is_rejected():
    with pytest.raises(S.SemanticValidationError) as ei:
        S.validate_response(json.dumps({"report": "I could not tell"}), _allowed())
    assert "results" in ei.value.message


def test_bare_single_object_is_tolerated():
    item = json.loads(_payload())["results"][0]
    accepted, _ = S.validate_response(json.dumps(item), _allowed())
    assert len(accepted) == 1


# --------------------------------------------------------------------------
# closed vocabularies / normalisation
# --------------------------------------------------------------------------


def test_off_vocabulary_values_become_safe_defaults():
    data = json.loads(_payload())
    v = data["results"][0]["visual"]
    v["lighting"] = "late afternoon magic glow"
    v["composition"] = "cinematic"
    v["scene_tags"] = ["water", "not_a_real_tag", "URBAN"]
    v["mood"] = ["calm", "bogus"]
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    visual = accepted[0].visual
    assert visual["lighting"] == "unknown"
    assert visual["composition"] == "unknown"
    assert visual["scene_tags"] == ["water", "urban"]  # case-folded, bogus dropped
    assert visual["mood"] == ["calm"]


def test_oversize_strings_are_truncated():
    data = json.loads(_payload())
    data["results"][0]["visual"]["subjects"] = ["x" * 5000]
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert len(accepted[0].visual["subjects"][0]) == S.MAX_SUBJECT_LEN


def test_confidence_is_clamped_and_nan_safe():
    for raw, expected in ((1.7, 1.0), (-3, 0.0), ("nonsense", 0.0), (0.456, 0.456)):
        data = json.loads(_payload())
        data["results"][0]["evidence"]["observed_confidence"] = raw
        accepted, _ = S.validate_response(json.dumps(data), _allowed())
        assert accepted[0].evidence["observed_confidence"] == expected


def test_explicit_unknown_state_is_preserved():
    """'I cannot see this' is a valid answer and must not be normalised away."""
    data = json.loads(_payload())
    data["results"][0]["evidence"]["state"] = "not_visible"
    data["results"][0]["visual"] = {}
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert accepted[0].evidence["state"] == "not_visible"


def test_observed_without_content_is_downgraded():
    data = json.loads(_payload())
    data["results"][0]["visual"] = {}
    data["results"][0]["evidence"]["state"] = "observed"
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert accepted[0].evidence["state"] == "uncertain"
    assert "downgraded_observed_without_content" in accepted[0].warnings


# --------------------------------------------------------------------------
# identity / place claims must not be stored as fact
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim",
    [
        "this is Maria",
        "named Giovanni",
        "his wife",
        "located in Venice",
        "the city of Kyoto",
    ],
)
def test_identity_and_place_claims_are_dropped(claim):
    data = json.loads(_payload())
    data["results"][0]["visual"]["subjects"] = [claim, "wooden pier"]
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert claim not in accepted[0].visual["subjects"]
    assert "wooden pier" in accepted[0].visual["subjects"]
    assert "dropped_identity_claim:subjects" in accepted[0].warnings


def test_ordinary_visible_descriptions_survive():
    """The filter must not be so aggressive that real observations are lost."""
    data = json.loads(_payload())
    data["results"][0]["visual"]["subjects"] = [
        "a woman in a red coat",
        "green willow trees",
        "a white pagoda on a distant hill",
    ]
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert len(accepted[0].visual["subjects"]) == 3


# --------------------------------------------------------------------------
# single-frame truthfulness
# --------------------------------------------------------------------------


def test_single_frame_shot_is_forced_into_motion_review():
    """A still cannot prove motion, so the flag is set with the reason recorded."""
    data = json.loads(_payload())
    data["results"][0]["segment_id"] = "seg-2"
    data["results"][0]["evidence"]["frame_ids"] = ["frame-c"]
    data["results"][0]["review"] = {"needs_motion_review": False, "reasons": []}
    accepted, _ = S.validate_response(json.dumps(data), _allowed())
    assert accepted[0].review["needs_motion_review"] is True
    assert "single_frame_cannot_prove_camera_motion" in accepted[0].review["reasons"]


# --------------------------------------------------------------------------
# prompt contract
# --------------------------------------------------------------------------


def test_prompt_contains_the_rules_the_task_book_requires():
    prompt = S.build_prompt(
        [
            {
                "segment_id": "seg-1",
                "asset_id": "asset-1",
                "start_sec": 1.0,
                "end_sec": 2.0,
                "frame_ids": ["frame-a"],
                "story_role": "opening",
            }
        ],
        coverage={"images_sent": 1, "segments_considered": 1, "eligible": 46},
    )
    assert "ONLY what is visible" in prompt
    assert "never invent" in prompt.lower()
    assert "not knowable from a photograph" in prompt
    assert "do not generalise about footage you were not shown" in prompt
    assert "segment_id=seg-1" in prompt
    assert "1 of 46" in prompt


def test_prompt_never_leaks_a_filesystem_path():
    prompt = S.build_prompt(
        [
            {
                "segment_id": "s",
                "asset_id": "a",
                "start_sec": 0.0,
                "end_sec": 1.0,
                "frame_ids": [],
                "story_role": "",
            }
        ]
    )
    assert "C:\\" not in prompt and "/Users/" not in prompt and "\\\\" not in prompt
