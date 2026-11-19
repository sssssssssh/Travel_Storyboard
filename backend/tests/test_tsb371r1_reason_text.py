"""TSB-371-R1 — a plan's reasons must not be silently chopped.

A reviewer comparing two plans penalises the side whose reasons look broken. The
offline planner sliced the multiframe summary at 160 characters and the video event
description at 120, which cut English mid-word:

    "...by t=0.50s the camera has panned left "
    "...a truck that briefly fills/occludes the frame"
    "...Across al"

The reading was complete; the display made it look defective. That is a rating
contaminated by a rendering bug, so the rule here is:

* short text is shown whole;
* over-long text is cut at a word boundary **and labelled** as truncated, so the
  reader knows the difference between "the model said this much" and "we stopped
  printing".
"""

from __future__ import annotations

import pytest

from app.services import director_plan_rules as rules


def test_short_text_is_returned_unchanged():
    text = "Camera is inside a moving vehicle."
    assert rules._whole(text, 600) == text


def test_exactly_at_the_limit_is_not_marked():
    text = "x" * 50
    assert rules._whole(text, 50) == text


def test_over_long_text_is_marked_as_truncated():
    text = "word " * 200
    out = rules._whole(text, 100)
    assert out.endswith("（原文过长，已截断）")
    assert out != text


def test_truncation_happens_at_a_word_boundary():
    """Cutting mid-word is what made the reading look defective."""
    text = "The camera pans left past the pagoda and the willow branches"
    out = rules._whole(text, 30)
    body = out.replace("…（原文过长，已截断）", "").rstrip("…")
    # Whatever survives must end on a whole word, not a fragment.
    assert not body.endswith("pag")
    words = text.split()
    assert body.split()[-1] in words


def test_the_english_summary_survives_intact_when_it_fits():
    """The real readings are ~180 chars; they must not be cut at all."""
    summary = (
        "Camera is inside a moving bus/tram. From t=0.17s to t=0.33s it looks "
        "through one window at a street with cars and a tree; by t=0.50s the "
        "camera has panned left and a second window frames a white van."
    )
    out = rules._whole(summary, 600)
    assert out == summary
    assert out.endswith("white van.")


@pytest.mark.parametrize("at_limit", [False, True])
def test_a_claim_keeps_the_whole_motion_summary(at_limit):
    """End-to-end: the claim text must contain the full summary."""
    from app.services.director_plan_input import (
        HumanFacts, MotionEvidence, PlanningEntry, StaticEvidence, TechnicalFacts,
        VideoEvidence,
    )
    from app.services.director_plan_schema import EvidenceRef

    summary = ("Camera pans steadily left across the lake: the white pagoda on "
               "the right drifts rightward and exits by t=9.83s, while willow "
               "branches enter from the left edge.")
    if at_limit:
        summary = (summary * 2)[:300]
    entry = PlanningEntry(
        segment_id="seg-1", asset_id="a1", asset_display_name="c.mp4",
        source_in_sec=4.0, source_out_sec=11.0, eligibility="eligible",
        human=HumanFacts(rating="pending", rating_source="none",
                         story_role="", note=""),
        technical=TechnicalFacts(duration_sec=7.0, quality_metrics={},
                                 is_trimmed=False),
        static=StaticEvidence(state="not_analyzed", result_id=None,
                              evidence_state="", visual={}, model={},
                              limitations=[]),
        multiframe=MotionEvidence(state="current", result_id="res-m",
                                  motion_type="pan", action_summary=summary,
                                  stability_risk="low", human_verdict="",
                                  limitations=[]),
        video_native=VideoEvidence(state="not_analyzed", result_id=None,
                                   summary="", events=[], audio_analyzed=False,
                                   color_unverified=False,
                                   observed_temporal_quality="", limitations=[]),
        editorial_priority="unrated", limitations=[],
    )
    claims = rules._claims_for(entry, [
        EvidenceRef(channel="multiframe", result_id="res-m", segment_id="seg-1"),
    ])
    text = " ".join(c.text for c in claims)
    if at_limit:
        assert len(text) <= 300 and text.endswith("（原文过长，已截断）")
    else:
        assert summary in text, "the motion summary was truncated"
