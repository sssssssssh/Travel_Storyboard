"""TSB-330 — A/B evaluation scaffold.

The metrics are ordinary set arithmetic; the *discipline* is what these tests
protect. Two failure modes would be worse than having no harness at all:

1. reporting a number when there is no human ground truth, which reads as an
   accuracy figure and will eventually be quoted as one; and
2. filling arm C's slot from arm B when the endpoint cannot do video, which would
   make an image reading look like evidence for native video.

So most of this file asserts what the harness *refuses* to say.
Task book row V19 (missing human labels) is the central case.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.services.vision import video_ab as ab
from app.services.vision.video_ab import (
    ARM_A,
    ARM_B,
    ARM_C,
    BLOCKED_BY_ENDPOINT,
    NOT_ANNOTATED,
    NO_READING,
    SegmentArms,
)
from app.services.vision.video_capabilities import (
    VERIFIED_NATIVE_VIDEO,
    VideoCapabilities,
    record_verification,
)


def _verified() -> VideoCapabilities:
    return record_verification(
        state=VERIFIED_NATIVE_VIDEO, protocol="video_url_data_uri",
        model_id="m", verification_scope="s",
    )


def _arms(*, with_c: bool = True, with_a: bool = True, with_b: bool = True,
          segment_id: str = "seg-1", asset_id: str = "asset-1",
          events: list[dict] | None = None) -> list[SegmentArms]:
    arm = SegmentArms(segment_id=segment_id, asset_id=asset_id,
                      start_sec=0.0, end_sec=20.0)
    if with_a:
        arm.arm_a = {"summary": "a lake", "events": [], "event_count": 0}
    if with_b:
        arm.arm_b = {"summary": "pan left", "motion_type": "pan",
                     "stability_risk": "low", "event_count": None}
    if with_c:
        arm.arm_c = {
            "summary": "boats move then stop",
            "events": events if events is not None else [
                {"event_type": "subject_action", "start_abs_sec": 5.0,
                 "end_abs_sec": 8.0, "description": "a boat moves", "confidence": 0.6},
            ],
            "event_count": 1,
            "observed_temporal_quality": "limited",
            "audio_analyzed": False, "color_unverified": False,
        }
    if not with_c:
        arm.missing_reasons[ARM_C] = BLOCKED_BY_ENDPOINT
    return [arm]


def _labels(*, segment_id: str = "seg-1", n: int = 1) -> dict[str, list[dict]]:
    return {
        segment_id: [
            {"start_abs_sec": 5.0 + i, "end_abs_sec": 8.0 + i, "description": f"event {i}"}
            for i in range(n)
        ]
    }


# ---------------------------------------------------------------------------
# metrics: plain arithmetic, pinned so a later refactor cannot drift
# ---------------------------------------------------------------------------

def test_iou_of_identical_windows_is_one():
    assert ab.temporal_iou((0.0, 10.0), (0.0, 10.0)) == pytest.approx(1.0)


def test_iou_of_half_overlap():
    assert ab.temporal_iou((0.0, 10.0), (5.0, 15.0)) == pytest.approx(1 / 3)


def test_iou_of_disjoint_windows_is_zero():
    assert ab.temporal_iou((0.0, 5.0), (6.0, 10.0)) == 0.0


def test_iou_of_a_contained_window():
    assert ab.temporal_iou((0.0, 10.0), (2.0, 4.0)) == pytest.approx(0.2)


def test_iou_with_a_zero_width_window_is_zero_not_a_crash():
    assert ab.temporal_iou((5.0, 5.0), (5.0, 5.0)) == 0.0


def test_matching_counts_a_hit_and_a_spurious_event():
    result = ab.match_events(
        [{"start_abs_sec": 1.0, "end_abs_sec": 3.0},
         {"start_abs_sec": 10.0, "end_abs_sec": 12.0}],
        [{"start_abs_sec": 1.0, "end_abs_sec": 3.0}],
    )
    assert result["true_positives"] == 1
    assert result["false_positives"] == 1
    assert result["false_negatives"] == 0


def test_a_label_is_never_matched_twice():
    """Double-counting a label would inflate recall."""
    result = ab.match_events(
        [{"start_abs_sec": 1.0, "end_abs_sec": 3.0},
         {"start_abs_sec": 1.1, "end_abs_sec": 3.1}],
        [{"start_abs_sec": 1.0, "end_abs_sec": 3.0}],
    )
    assert result["true_positives"] == 1
    assert result["false_positives"] == 1


def test_matching_respects_the_iou_threshold():
    far = ab.match_events(
        [{"start_abs_sec": 1.0, "end_abs_sec": 2.0}],
        [{"start_abs_sec": 1.5, "end_abs_sec": 6.0}],
        iou_threshold=0.5,
    )
    assert far["true_positives"] == 0
    assert far["false_negatives"] == 1


def test_events_without_absolute_times_are_ignored_not_guessed():
    result = ab.match_events(
        [{"start_rel_sec": 1.0, "end_rel_sec": 2.0}],
        [{"start_abs_sec": 1.0, "end_abs_sec": 2.0}],
    )
    assert result["true_positives"] == 0
    assert result["false_positives"] == 1


# ---------------------------------------------------------------------------
# V19: no labels means no accuracy figure
# ---------------------------------------------------------------------------

def test_without_labels_every_arm_reports_null_metrics():
    """The headline rule: absent ground truth, we report coverage, not accuracy."""
    scored = ab.score_arms(_arms(), {})
    assert scored["evaluated"] is False
    for arm in (ARM_A, ARM_B, ARM_C):
        assert scored["arms"][arm]["evaluated"] is False
        assert scored["arms"][arm]["event_recall"] is None
        assert scored["arms"][arm]["event_precision"] is None
        assert scored["arms"][arm]["reason"] == NOT_ANNOTATED


def test_without_labels_adoption_is_not_decided():
    scored = ab.score_arms(_arms(), {})
    assert scored["meets_adoption_criteria"]["decided"] is False
    assert scored["meets_adoption_criteria"]["adopt_video_as_preferred"] is False
    assert scored["meets_adoption_criteria"]["missing"]


def test_a_report_without_labels_is_marked_not_annotated():
    report = ab.build_report(project_id="p", arms=_arms(), labels_by_segment={},
                             capabilities=_verified())
    assert report["status"] == NOT_ANNOTATED
    assert report["evaluated"] is False


def test_a_perfect_arm_scores_one():
    scored = ab.score_arms(_arms(), _labels())
    assert scored["arms"][ARM_C]["event_recall"] == pytest.approx(1.0)
    assert scored["arms"][ARM_C]["event_precision"] == pytest.approx(1.0)
    assert scored["arms"][ARM_C]["temporal_iou_ge_threshold"] == pytest.approx(1.0)


def test_a_missed_event_lowers_recall():
    """One predicted event, two real ones -> recall 0.5."""
    scored = ab.score_arms(_arms(), _labels(n=2))
    assert scored["arms"][ARM_C]["event_recall"] == pytest.approx(0.5)


def test_an_arm_that_ran_but_found_nothing_scores_zero_not_null():
    """A real (poor) result: the channel ran and reported no events.

    ``0.0`` means "ran, found nothing"; ``None`` means "never ran". Conflating them
    would let a never-executed arm look like a thorough one that simply found no
    events -- or, worse in the other direction, let a blocked arm look like it ran.
    """
    scored = ab.score_arms(_arms(events=[]), _labels())
    assert scored["arms"][ARM_C]["evaluated"] is True
    assert scored["arms"][ARM_C]["event_recall"] == 0.0
    assert scored["arms"][ARM_C]["reason"] == "no_events_detected"
    assert scored["arms"][ARM_C]["predicted_events"] == 0


def test_an_arm_with_no_reading_at_all_is_null_not_zero():
    """The mirror case: arm C absent must not be scored as a zero.
    """
    scored = ab.score_arms(_arms(with_c=False), _labels())
    assert scored["arms"][ARM_C]["evaluated"] is False
    assert scored["arms"][ARM_C]["event_recall"] is None
    assert scored["arms"][ARM_C]["reason"] == NO_READING


# ---------------------------------------------------------------------------
# arm C is blocked, and must stay blocked
# ---------------------------------------------------------------------------

def test_collecting_arms_marks_c_blocked_when_the_endpoint_cannot_do_video(
    client, tmp_path
):
    """The reason must distinguish 'cannot' from 'has not run yet'."""
    from app.database import session_scope
    from app.models import Asset, Project, Segment

    res = client.post("/api/projects", json={"name": "ab", "source_roots": []})
    project_id = res.json()["id"]
    source = tmp_path / "a.mp4"
    source.write_bytes(b"ab-source")
    asset_id, segment_id = str(uuid.uuid4()), str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path=str(source),
                    display_name="a.mp4", scan_status="succeeded"))
        s.flush()
        s.add(Segment(id=segment_id, project_id=project_id, asset_id=asset_id,
                      start_sec=0.0, end_sec=20.0, detected_start_sec=0.0,
                      detected_end_sec=20.0, boundary_reason="hard_cut"))

    arms = ab.collect_arms(project_id, capabilities=VideoCapabilities(state="unverified"))
    assert len(arms) == 1
    assert arms[0].arm_c is None
    assert arms[0].missing_reasons[ARM_C] == BLOCKED_BY_ENDPOINT


def test_a_blocked_run_reports_blocked_by_endpoint():
    report = ab.build_report(
        project_id="p", arms=_arms(with_c=False), labels_by_segment=_labels(),
        capabilities=VideoCapabilities(state="unsupported"),
    )
    assert report["status"] == BLOCKED_BY_ENDPOINT
    assert report["evaluated"] is False
    assert report["arm_c_available"] == 0
    assert report["metrics"]["arms"][ARM_C]["event_recall"] is None


def test_arm_c_is_never_filled_from_arm_b():
    """The specific substitution this harness must never perform."""
    report = ab.build_report(
        project_id="p", arms=_arms(with_c=False), labels_by_segment=_labels(),
        capabilities=VideoCapabilities(state="unsupported"),
    )
    per_segment = report["per_segment"][0]
    assert per_segment["arms"][ARM_C] is None, (
        "arm C must stay absent when video was never sent; copying arm B into it "
        "would present a multi-frame reading as native-video evidence"
    )
    assert ARM_C not in per_segment["available_arms"]


def test_mock_results_are_not_accepted_as_arm_c_evidence():
    """Even a full set of numbers cannot make a blocked run 'evaluated'."""
    report = ab.build_report(
        project_id="p", arms=_arms(with_c=False), labels_by_segment=_labels(),
        capabilities=VideoCapabilities(state="unverified"),
    )
    assert report["evaluated"] is False


# ---------------------------------------------------------------------------
# sampling honesty
# ---------------------------------------------------------------------------

def test_the_report_states_how_many_distinct_videos_the_segments_came_from():
    """20 segments from 6 videos is not '20 different videos'."""
    arms = [
        SegmentArms(segment_id=f"seg-{i}", asset_id=f"asset-{i % 3}",
                    start_sec=0.0, end_sec=10.0)
        for i in range(9)
    ]
    report = ab.build_report(project_id="p", arms=arms, labels_by_segment={},
                             capabilities=_verified())
    assert report["segments_in_run"] == 9
    assert report["distinct_assets"] == 3
    assert "3 source videos" in report["sampling_note"]
    assert "not independent samples" in report["sampling_note"]


def test_the_report_refuses_to_compare_token_costs_across_shapes():
    report = ab.build_report(project_id="p", arms=_arms(), labels_by_segment={},
                             capabilities=_verified())
    assert "NOT comparable" in report["cost_note"]


def test_the_adoption_thresholds_are_pre_registered():
    """Thresholds live in code so they cannot be chosen after seeing results."""
    criteria = ab.ADOPTION_CRITERIA
    assert criteria["min_event_recall_gain_points"] == 10.0
    assert criteria["temporal_iou_threshold"] == 0.5
    assert criteria["requires_real_human_labels"] is True


def test_a_clear_gain_meets_the_criteria():
    """A strong arm C over a weak arm A decides in favour, when data allows."""
    a_events: list[dict] = []
    arms = _arms(events=[
        {"event_type": "subject_action", "start_abs_sec": 5.0, "end_abs_sec": 8.0,
         "description": "moves", "confidence": 0.8},
        {"event_type": "subject_action", "start_abs_sec": 12.0, "end_abs_sec": 14.0,
         "description": "moves again", "confidence": 0.8},
    ])
    arms[0].arm_a = {"summary": "still", "events": a_events, "event_count": 0}
    scored = ab.score_arms(arms, {"seg-1": [
        {"start_abs_sec": 5.0, "end_abs_sec": 8.0},
        {"start_abs_sec": 12.0, "end_abs_sec": 14.0},
    ]})
    assert scored["arms"][ARM_A]["event_recall"] == 0.0
    assert scored["arms"][ARM_C]["event_recall"] == pytest.approx(1.0)
    assert scored["recall_gain_points_C_over_A"] == pytest.approx(100.0)
    assert scored["meets_adoption_criteria"]["decided"] is True
    assert scored["meets_adoption_criteria"]["adopt_video_as_preferred"] is True


def test_no_gain_does_not_adopt():
    arms = _arms(events=[
        {"event_type": "subject_action", "start_abs_sec": 5.0, "end_abs_sec": 8.0,
         "description": "moves", "confidence": 0.8},
    ])
    arms[0].arm_a = {"summary": "same", "events": [
        {"event_type": "subject_action", "start_abs_sec": 5.0, "end_abs_sec": 8.0,
         "description": "moves", "confidence": 0.8},
    ], "event_count": 1}
    scored = ab.score_arms(arms, {"seg-1": [{"start_abs_sec": 5.0, "end_abs_sec": 8.0}]})
    assert scored["recall_gain_points_C_over_A"] == pytest.approx(0.0)
    assert scored["meets_adoption_criteria"]["adopt_video_as_preferred"] is False


# ---------------------------------------------------------------------------
# labels file and report writing
# ---------------------------------------------------------------------------

def test_a_missing_label_file_yields_no_labels(tmp_path):
    assert ab.load_labels(tmp_path / "nope.json") == {}


def test_a_malformed_label_file_yields_no_labels_rather_than_zero_scores(tmp_path):
    """Malformed input must not become 'scored with 0 events'."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ab.load_labels(bad) == {}


def test_labels_with_inverted_or_non_numeric_windows_are_dropped(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({
        "seg-1": [
            {"start_abs_sec": 5, "end_abs_sec": 3},        # inverted
            {"start_abs_sec": "x", "end_abs_sec": 8},      # non-numeric
            {"start_abs_sec": 1, "end_abs_sec": 4},        # valid
        ],
        "seg-2": "not a list",
    }), encoding="utf-8")
    labels = ab.load_labels(path)
    assert list(labels) == ["seg-1"]
    assert len(labels["seg-1"]) == 1


def test_the_written_report_contains_no_local_paths(tmp_path):
    report = ab.build_report(project_id="p", arms=_arms(), labels_by_segment={},
                             capabilities=_verified())
    out = tmp_path / "AB_RESULTS.json"
    ab.write_report(report, out)
    assert out.is_file()
    blob = out.read_text(encoding="utf-8")
    for forbidden in ("C:\\", "/Users/", "/tmp/"):
        assert forbidden not in blob


def test_writing_refuses_a_report_that_leaked_a_path(tmp_path):
    """A guard, so a future field cannot smuggle an absolute path into evidence."""
    out = tmp_path / "leaky.json"
    with pytest.raises(ValueError):
        ab.write_report({"note": r"source was C:\footage\a.mp4"}, out)
    assert not out.exists(), "nothing should be written when the guard trips"


def test_the_report_carries_the_honesty_statements():
    report = ab.build_report(project_id="p", arms=_arms(), labels_by_segment={},
                             capabilities=_verified())
    joined = " ".join(report["honesty"])
    assert "不填造准确率" in joined
    assert "blocked_by_endpoint" in joined
