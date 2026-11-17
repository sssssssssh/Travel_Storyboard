"""TSB-371-R1 — a model must be told the window rules, and its breaches must fail.

A real call against the owner's endpoint returned plans where most shots had
``source_in_sec == source_out_sec`` — a zero-length window that cuts nothing. The
validator caught them, which is why no invalid plan reached a storyboard, but the
plan was useless.

Two gaps made that happen:

1. **The prompt never stated the rule.** ``SCHEMA_HINT`` said only
   ``"source_in_sec":num``; nothing told the model that the window must be
   non-empty and inside the shot it names.

2. **The parser coerced instead of rejecting.** It clamped out-of-range values into
   the shot's window — a reasonable repair for a small overshoot, but when the
   model sent ``32.0-32.0`` the clamp preserved the empty window and the failure
   surfaced much later as a generic validation error.

The tests below pin both: the prompt states the constraints, and an empty window is
refused with a precise code rather than silently passed on.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.services import director_plan_model as dpm
from app.services.director_plan_input import PlanningInput
from app.services.director_plan_schema import CoverageSummary, PlanningBrief


# ---------------------------------------------------------------------------
# The prompt must state the window rules
# ---------------------------------------------------------------------------

def test_the_prompt_states_the_window_rules():
    text = dpm.SYSTEM_PROMPT + "\n" + dpm.SCHEMA_HINT
    lowered = text.lower()
    # Non-empty.
    assert "greater" in lowered or "> source_in_sec" in text or "non-empty" in lowered
    # Inside the shot the model names.
    assert "within" in lowered or "inside" in lowered or "不超过" in text
    # A concrete minute second figure so "num" is not read as "anything goes".
    assert "source_out_sec" in text and "source_in_sec" in text


def test_the_prompt_forbids_inventing_a_segment():
    text = dpm.SYSTEM_PROMPT + "\n" + dpm.SCHEMA_HINT
    lowered = text.lower()
    assert "segment_id" in text
    assert ("only" in lowered or "never invent" in lowered or "listed" in lowered)


# ---------------------------------------------------------------------------
# An empty window is refused, with a precise code
# ---------------------------------------------------------------------------

def _planning_input() -> PlanningInput:
    from app.services.director_plan_input import (
        HumanFacts, MotionEvidence, PlanningEntry, StaticEvidence, TechnicalFacts,
        VideoEvidence,
    )

    seg = str(uuid.uuid4())
    entry = PlanningEntry(
        segment_id=seg, asset_id="asset-1", asset_display_name="c.mp4",
        source_in_sec=31.5, source_out_sec=38.0,
        eligibility="eligible",
        human=HumanFacts(rating="pending", rating_source="none",
                         story_role="", note=""),
        technical=TechnicalFacts(duration_sec=6.5, quality_metrics={},
                                 is_trimmed=False),
        static=StaticEvidence(state="current", result_id="res-1",
                              evidence_state="observed",
                              visual={"scene_tags": ["lake"]},
                              model={"provider": "x", "id": "m",
                                     "prompt_version": "1"},
                              limitations=[]),
        multiframe=MotionEvidence(state="not_analyzed", result_id=None,
                                  motion_type="", action_summary="",
                                  stability_risk="", human_verdict="",
                                  limitations=[]),
        video_native=VideoEvidence(state="not_analyzed", result_id=None,
                                   summary="", events=[], audio_analyzed=False,
                                   color_unverified=False,
                                   observed_temporal_quality="", limitations=[]),
        editorial_priority="unrated", limitations=[],
    )
    return PlanningInput(
        project_id="p1", source_input_digest="d" * 64,
        coverage=CoverageSummary(library_segments=1,
                                 segments_with_any_valid_reading=1,
                                 no_valid_model_reading=0,
                                 video_native_valid=0),
        withheld={}, limitations=[],
        base_storyboard_version_id=None, storyboard_absent=True,
        entries=[entry],
    )


def _payload(*, in_sec: float, out_sec: float, seg_id: str) -> dict:
    return {
        "variants": [{
            "title": "t", "narrative_angle": "chronological", "logline": "l",
            "sections": [{"section_id": "s1", "kind": "body", "title": "b"}],
            "shots": [{
                "segment_id": seg_id,
                "source_in_sec": in_sec, "source_out_sec": out_sec,
                "section_id": "s1", "narrative_role": "development",
                "selection_reason": "r",
                "claims": [{"text": "c", "kind": "observed_signal",
                            "evidence_refs": [{"channel": "static",
                                               "result_id": "res-1"}]}],
                "evidence_refs": [{"channel": "static", "result_id": "res-1"}],
            }],
        }]
    }


def test_an_empty_window_is_named_in_the_plan_warnings():
    """Reported precisely, but still handed to the validator (defence in depth)."""
    pi = _planning_input()
    seg = pi.entries[0].segment_id
    plan = dpm.parse_model_plan(_payload(in_sec=32.0, out_sec=32.0, seg_id=seg),
                                pi, PlanningBrief(theme="t", target_duration_sec=60),
                                model_id="GLM", provider_name="openai_compatible")
    assert any("MODEL_EMPTY_SHOT_WINDOW" in w for w in plan.warnings), plan.warnings
    # The validator must also catch it independently.
    from app.services import director_plan_validation as val
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "INVALID_SHOT_WINDOW" in codes


def test_an_inverted_window_is_named_too():
    pi = _planning_input()
    seg = pi.entries[0].segment_id
    plan = dpm.parse_model_plan(_payload(in_sec=35.0, out_sec=33.0, seg_id=seg),
                                pi, PlanningBrief(theme="t", target_duration_sec=60),
                                model_id="GLM", provider_name="openai_compatible")
    assert any("MODEL_EMPTY_SHOT_WINDOW" in w for w in plan.warnings)


def test_a_window_outside_the_shot_is_still_clamped_not_rejected():
    """A small overshoot is a repair, not a failure: the shot is identifiable."""
    pi = _planning_input()
    seg = pi.entries[0].segment_id
    plan = dpm.parse_model_plan(_payload(in_sec=30.0, out_sec=40.0, seg_id=seg),
                                pi, PlanningBrief(theme="t", target_duration_sec=60),
                             model_id="GLM", provider_name="openai_compatible")
    shot = plan.variants[0].shots[0]
    assert shot.source_in_sec == pytest.approx(31.5)
    assert shot.source_out_sec == pytest.approx(38.0)


def test_a_valid_window_is_kept_exactly():
    pi = _planning_input()
    seg = pi.entries[0].segment_id
    plan = dpm.parse_model_plan(_payload(in_sec=33.0, out_sec=36.0, seg_id=seg),
                                pi, PlanningBrief(theme="t", target_duration_sec=60),
                             model_id="GLM", provider_name="openai_compatible")
    shot = plan.variants[0].shots[0]
    assert shot.source_in_sec == pytest.approx(33.0)
    assert shot.source_out_sec == pytest.approx(36.0)


