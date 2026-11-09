"""TSB-368-A — director candidate aggregation contracts.

These are the "negative" cases the task book demands: a shot with no AI result
must still be a candidate, a still-frame reading must never be reported as video
understanding, and human judgement must never be overwritten by machine output.

Every test builds real rows through the ORM and reads them back through the real
service — no mocked query layer, because the bugs this guards against live in the
joins and the freshness comparisons.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
    UserAnnotation,
)
from app.services import director_candidates as dc


def _uid() -> str:
    return str(uuid.uuid4())


def _project(s, name: str = "tsb368") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project: Project, name: str = "clip.mp4") -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name=name, file_size_bytes=1000, mtime_ns=1, quick_fingerprint="fp",
        duration_sec=60.0, fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360,
        rotation=0, codec="h264", pixel_format="yuv420p", color_primaries="", color_transfer="",
        color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
        analysis_version="", analyzed_fingerprint="", analyzed_source_signature="",
        analyzed_detect_config="", analyzed_at=None, scanned_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _segment(
    s, project: Project, asset: Asset, start: float, end: float,
    *, status: str = "candidate", rating: str = "pending", story_role: str = "",
    metrics: dict | None = None,
) -> Segment:
    g = Segment(
        id=_uid(), asset_id=asset.id, project_id=project.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics=metrics if metrics is not None else {
            "blur_avg": 120.0, "blur_min": 90.0, "brightness_avg": 110.0,
            "overexposed_ratio": 0.01, "underexposed_ratio": 0.0,
            "frame_count": 3, "near_duplicate_segments": [],
        },
        content_tags={}, story_role=story_role, user_rating=rating,
        status=status, frame_refs=[], proxy_ref="", parent_segment_id="",
        algorithm_version="1", created_at=None,
    )
    s.add(g)
    s.flush()
    return g


def _semantic(
    s, project: Project, asset: Asset, seg: Segment, *,
    is_current: bool = True, status: str = "succeeded", signature: str = "sig-1",
    visual: dict | None = None, evidence_state: str = "observed",
) -> SegmentSemanticResult:
    r = SegmentSemanticResult(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status=status, model_provider="mock", model_id="m1", prompt_version="p1",
        schema_version="1.0", source_signature=signature, frame_manifest_hash="h1",
        analysis_json={"visual": visual if visual is not None else {
            "scene_tags": ["nature"], "subjects": ["a tree"], "visible_actions": ["swaying"],
            "lighting": "daylight", "composition": "wide", "mood": ["calm"],
        }},
        evidence_json={
            "state": evidence_state, "observed_confidence": 0.82,
            "frame_ids": ["f1", "f2"], "limitations": [],
        },
        request_usage={"total_tokens": 999}, is_current=is_current,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _motion(
    s, project: Project, asset: Asset, seg: Segment, *,
    is_current: bool = True, start: float | None = None, end: float | None = None,
) -> SegmentMotionReview:
    r = SegmentMotionReview(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        method="multiframe", status="succeeded", model_provider="mock", model_id="m1",
        prompt_version="p1", start_sec=seg.start_sec if start is None else start,
        end_sec=seg.end_sec if end is None else end,
        frame_manifest_hash="h1", input_signature="input-sig",
        motion_type="pan", stability_risk="low", action_summary="camera pans right",
        analysis_json={}, evidence_json={"confidence": 0.7}, request_usage={},
        is_current=is_current, human_verdict="", human_note="", human_at=None,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _video(
    s, project: Project, asset: Asset, seg: Segment, *,
    is_current: bool = True,
    seg_start: float | None = None, seg_end: float | None = None,
    events: list | None = None,
) -> SegmentVideoObservation:
    r = SegmentVideoObservation(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", method="video_native", model_provider="mock", model_id="m1",
        prompt_version="p1", schema_version="1.0", protocol="native",
        source_signature="v-sig", clip_sha256="clip-hash", version_key="vk",
        clip_start_sec=seg.start_sec, clip_end_sec=seg.end_sec,
        clip_duration_sec=seg.end_sec - seg.start_sec, clip_size_bytes=1000,
        segment_start_sec=seg.start_sec if seg_start is None else seg_start,
        segment_end_sec=seg.end_sec if seg_end is None else seg_end,
        analysis_json={"summary": "a person walks past a fountain"},
        events_json=events if events is not None else [
            {"start_sec": seg.start_sec + 0.2, "end_sec": seg.start_sec + 1.0,
             "label": "walking", "confidence": 0.6},
        ],
        request_usage={}, audio_analyzed=False, color_unverified=True,
        observed_temporal_quality="partial", is_current=is_current,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _collect(project_id: str, *, verify: bool = False) -> list[dc.Candidate]:
    with session_scope() as s:
        return dc.collect_candidates(s, project_id, verify_freshness=verify)


# ---------------------------------------------------------------------------
# 1. A shot with no AI result is still a candidate
# ---------------------------------------------------------------------------

def test_a_shot_without_any_analysis_is_eligible_not_analyzed():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        pid, gid = p.id, g.id

    cands = _collect(pid)
    assert len(cands) == 1
    c = cands[0]
    assert c.segment_id == gid
    assert c.eligibility == dc.ELIGIBLE, "no model reading must not exclude a shot"
    assert c.static.state == dc.STATE_NOT_ANALYZED
    assert c.multiframe.state == dc.STATE_NOT_ANALYZED
    assert c.video_native.state == dc.STATE_NOT_ANALYZED
    assert "no_valid_model_reading" in c.limitations
    # And it must not invent content.
    assert c.static.payload == {}
    assert c.video_native.payload == {}
    assert c.evidence_refs() == []
    assert c.editorial["priority"] == "unrated"


def test_coverage_counts_unanalyzed_shots_in_the_denominator():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g1 = _segment(s, p, a, 0.0, 5.0)
        _segment(s, p, a, 5.0, 10.0)
        _segment(s, p, a, 10.0, 15.0)
        _semantic(s, p, a, g1)
        pid = p.id

    cov = dc.coverage(_collect(pid))
    assert cov["library_segments"] == 3
    assert cov["static_valid"] == 1
    assert cov["no_valid_model_reading"] == 2, "two shots have no model reading at all"
    assert cov["coverage_complete"] is False


# ---------------------------------------------------------------------------
# 2/3. Channels stay separate
# ---------------------------------------------------------------------------

def test_static_only_never_claims_video_understanding():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g)
        pid = p.id

    c = _collect(pid)[0]
    assert c.static.result_id is not None
    assert c.video_native.state == dc.STATE_NOT_ANALYZED
    assert c.video_native.result_id is None
    assert "video_native_not_analyzed" in c.video_native.limitations
    # A still reading is explicitly a still reading.
    assert c.static.method == "static"


def test_multiframe_does_not_impersonate_native_video():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _motion(s, p, a, g)
        pid = p.id

    c = _collect(pid)[0]
    assert c.multiframe.result_id is not None
    assert c.video_native.state == dc.STATE_NOT_ANALYZED
    assert c.multiframe.method == "multiframe"


def test_all_three_channels_are_reported_side_by_side():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g)
        _motion(s, p, a, g)
        _video(s, p, a, g)
        pid = p.id

    c = _collect(pid)[0]
    assert c.static.result_id is not None
    assert c.multiframe.result_id is not None
    assert c.video_native.result_id is not None
    kinds = {r["type"] for r in c.evidence_refs()}
    assert kinds == {"static", "multiframe", "video_native"}

    cov = dc.coverage(_collect(pid))
    assert cov["static_valid"] == 1
    assert cov["multiframe_valid"] == 1
    assert cov["video_native_valid"] == 1
    # Three channels, one shot: the "any reading" total must not be 3.
    assert cov["segments_with_any_valid_reading"] == 1
    assert cov["no_valid_model_reading"] == 0


# ---------------------------------------------------------------------------
# 4. Staleness
# ---------------------------------------------------------------------------

def test_a_superseded_result_is_stale_not_current():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g, is_current=False)
        pid = p.id

    c = _collect(pid)[0]
    assert c.static.state == dc.STATE_STALE
    assert c.static.result_id is not None, "the row is still traceable"
    cov = dc.coverage(_collect(pid))
    assert cov["static_valid"] == 0
    assert cov["no_valid_model_reading"] == 1


def test_video_observation_for_a_different_window_is_stale():
    """The shot was trimmed after the video was analysed."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _video(s, p, a, g, seg_start=0.0, seg_end=8.0)   # recorded window differs
        pid = p.id

    c = _collect(pid)[0]
    assert c.video_native.state == dc.STATE_STALE
    assert "analysis_window_differs_from_current_shot" in c.video_native.limitations


def test_video_observation_with_matching_window_is_unverified_not_falsely_current():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _video(s, p, a, g)
        pid = p.id

    c = _collect(pid)[0]
    # Window matches, but the signature cannot be re-derived without re-analysis.
    assert c.video_native.state == dc.STATE_UNVERIFIED
    assert "source_signature_not_recomputable_without_reanalysis" in c.video_native.limitations


def test_motion_review_for_a_different_window_is_stale():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _motion(s, p, a, g, start=1.0, end=6.0)
        pid = p.id

    c = _collect(pid)[0]
    assert c.multiframe.state == dc.STATE_STALE


def test_a_failed_result_is_failed_not_silently_missing():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g, status="failed")
        pid = p.id

    c = _collect(pid)[0]
    assert c.static.state == dc.STATE_FAILED
    assert "static_analysis_failed" in c.limitations


# ---------------------------------------------------------------------------
# 5. Human judgement wins
# ---------------------------------------------------------------------------

def test_human_favorite_survives_poor_technical_quality():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(
            s, p, a, 0.0, 5.0, rating="favorite",
            metrics={"blur_avg": 1.0, "blur_min": 0.5, "brightness_avg": 3.0,
                     "overexposed_ratio": 0.9, "underexposed_ratio": 0.8, "frame_count": 1},
        )
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["rating"] == "favorite"
    assert c.editorial["priority"] == "preferred"
    assert "human_rating_favorite" in c.editorial["reasons"]
    # Low quality must not silently exclude or demote it.
    assert c.eligibility == dc.ELIGIBLE


def test_human_exclude_is_respected_and_still_counted_in_coverage():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0, rating="exclude")
        _semantic(s, p, a, g)           # even with a good AI reading
        pid = p.id

    c = _collect(pid)[0]
    assert c.eligibility == dc.EXCLUDED
    assert "human_excluded" in c.exclusion_reasons
    cov = dc.coverage(_collect(pid))
    assert cov["excluded_segments"] == 1
    assert cov["library_segments"] == 1, "excluded shots must stay in the denominator"


def test_annotation_rating_overrides_the_segment_column():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0, rating="pending")
        s.add(UserAnnotation(
            id=_uid(), segment_id=g.id, rating="favorite", note="keeper",
            in_sec=None, out_sec=None, created_at=None, updated_at=None,
        ))
        s.flush()
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["rating"] == "favorite"
    assert c.human["rating_source"] == "annotation"
    assert c.human["note"] == "keeper"


def test_ai_output_never_overwrites_human_story_role():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0, story_role="高潮")
        _semantic(s, p, a, g, visual={"scene_tags": ["urban"], "subjects": [],
                                      "visible_actions": [], "lighting": "night",
                                      "composition": "wide", "mood": ["dramatic"]})
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["story_role"] == "高潮"
    # The AI reading is present but in its own channel, not merged into `human`.
    assert c.static.payload["visual"]["scene_tags"] == ["urban"]


# ---------------------------------------------------------------------------
# 6. needs_review / invalid statuses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["needs_review", "archived", "invalid"])
def test_invalid_segment_statuses_are_needs_review_not_eligible(status):
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 5.0, status=status)
        pid = p.id

    c = _collect(pid)[0]
    assert c.eligibility == dc.NEEDS_REVIEW
    assert f"segment_status_{status}" in c.exclusion_reasons


def test_invalid_time_window_is_excluded_with_a_reason():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 5.0, 5.0)     # zero length
        pid = p.id

    c = _collect(pid)[0]
    assert c.eligibility == dc.EXCLUDED
    assert "invalid_time_window" in c.exclusion_reasons


# ---------------------------------------------------------------------------
# 7. Cross-project isolation
# ---------------------------------------------------------------------------

def test_results_from_another_project_never_leak_in():
    with session_scope() as s:
        p1 = _project(s, "p1")
        p2 = _project(s, "p2")
        a1 = _asset(s, p1)
        a2 = _asset(s, p2)
        g1 = _segment(s, p1, a1, 0.0, 5.0)
        g2 = _segment(s, p2, a2, 0.0, 5.0)
        # A semantic row that claims to belong to p1's segment but p2's project.
        bad = _semantic(s, p1, a1, g1)
        bad.project_id = p2.id
        s.flush()
        _video(s, p2, a2, g2)
        pid1, pid2 = p1.id, p2.id

    c1 = _collect(pid1)
    assert len(c1) == 1
    # The injected row must not be picked up as evidence for p1's shot.
    assert c1[0].static.result_id is None
    assert c1[0].static.state == dc.STATE_NOT_ANALYZED

    c2 = _collect(pid2)
    assert len(c2) == 1
    assert c2[0].video_native.result_id is not None


def test_another_projects_segments_are_not_returned():
    with session_scope() as s:
        p1 = _project(s, "p1")
        p2 = _project(s, "p2")
        a1 = _asset(s, p1)
        a2 = _asset(s, p2)
        _segment(s, p1, a1, 0.0, 5.0)
        _segment(s, p2, a2, 0.0, 5.0)
        pid1 = p1.id

    assert len(_collect(pid1)) == 1


# ---------------------------------------------------------------------------
# 8. Confidence is never presented as calibrated accuracy
# ---------------------------------------------------------------------------

def test_model_confidence_is_labelled_uncalibrated():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g)
        _video(s, p, a, g)
        pid = p.id

    c = _collect(pid)[0]
    assert c.static.payload["confidence_calibration"] == "uncalibrated"
    for ev in c.video_native.payload["events"]:
        assert ev["confidence_calibration"] == "uncalibrated"


# ---------------------------------------------------------------------------
# 9. Events are bounds-checked against the shot
# ---------------------------------------------------------------------------

def test_events_outside_the_shot_window_are_dropped_and_reported():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 10.0, 15.0)
        _video(s, p, a, g, events=[
            {"start_sec": 10.5, "end_sec": 11.0, "label": "inside", "confidence": 0.5},
            {"start_sec": 1.0, "end_sec": 2.0, "label": "outside_before", "confidence": 0.5},
            {"start_sec": 20.0, "end_sec": 21.0, "label": "outside_after", "confidence": 0.5},
            {"start_sec": 12.0, "end_sec": 12.0, "label": "zero_length", "confidence": 0.5},
        ])
        pid = p.id

    c = _collect(pid)[0]
    payload = c.video_native.payload
    labels = [e["label"] for e in payload["events"]]
    assert labels == ["inside"]
    # TSB-368-R2: the reason is reported as a structured drop reason, and the
    # counts prove the event existed rather than being absent.
    assert "event_outside_shot_window" in payload["drop_reasons"]
    assert payload["event_counters"]["dropped"] == 3
    assert payload["event_count_total"] == 4
    assert any("outside_shot_window" in lim for lim in c.video_native.limitations)


# ---------------------------------------------------------------------------
# 10. No filesystem / model work happens
# ---------------------------------------------------------------------------

def test_collection_does_no_freshness_io_when_not_requested(monkeypatch):
    """``verify_freshness=False`` must not touch the source filesystem."""
    from app.services import director_candidates as module

    def _boom(*args, **kwargs):
        raise AssertionError("source_identity must not be called")

    monkeypatch.setattr(module, "_source_signature_for", _boom)

    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g)
        pid = p.id

    cands = _collect(pid, verify=False)
    assert cands[0].static.state == dc.STATE_UNVERIFIED


def test_unreadable_source_yields_unverified_rather_than_a_guess():
    """With verification on but a missing source file, we admit we cannot tell."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 5.0)
        _semantic(s, p, a, g)
        pid = p.id

    cands = _collect(pid, verify=True)
    # The asset points at a nonexistent path, so source_identity returns "".
    assert cands[0].static.state == dc.STATE_UNVERIFIED
    assert "source_not_readable_for_verification" in cands[0].static.limitations
