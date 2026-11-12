"""TSB-371-R1 P1-01 — human judgement must reach the planner, per claim.

TSB-371 added a `review_state` flag on the candidate list, which was not enough:
`build_planning_input()` never read the ratings, so a sentence the reviewer marked
wrong still fed the plan. This file pins the real requirement.

The principles, from the task book §2:

* a wrong model description does **not** make the footage worthless. The shot keeps
  its place; only the wrong *claim* is withheld from the planner;
* right claims must survive when a sibling claim is wrong (partial correctness);
* "not sure" is never treated as an error;
* an unreviewed shot keeps the existing behaviour exactly;
* a judgement about a superseded reading must not grade the new one;
* nothing is rewritten: the model reading, the annotation and the segment stay as
  they were — the planner simply sees a filtered view.

The honest disclosure requirement: if claims were withheld, the planning input must
say so, in a way that changes the input digest. A plan built from filtered evidence
must not be indistinguishable from one built from unfiltered evidence.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
)
from app.services import director_plan_input as dpi
from app.services import evaluation_feedback as fb

SESSIONS = "/api/projects/{p}/evaluation/sessions"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-p1" 
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(client, *, analysis=None):
    """One shot with a real static reading: 3 subjects, 2 actions, 2 tags."""
    pid = client.post("/api/projects", json={"name": "r1p1"}).json()["id"]
    src = _tmp_root() / f"{pid[:8]}.mp4"
    src.write_bytes(b"\x00" * 64)
    analysis = analysis or {
        "scene_tags": ["lake", "sky"],
        "subjects": ["small boats", "wooden pier", "white pagoda"],
        "visible_actions": ["boats floating", "birds on the rail"],
        "lighting": "daylight",
        "composition": "wide",
        "mood": ["calm"],
    }
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=60.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=1280, height=720, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None,
            scanned_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()
        seg = Segment(
            id=_uid(), asset_id=a.id, project_id=pid,
            detected_start_sec=4.0, detected_end_sec=11.0,
            start_sec=4.0, end_sec=11.0, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        res = SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h", analysis_json=analysis,
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        )
        s.add(res)
        s.flush()
        return pid, seg.id, res.id


def _review(client, pid, verdicts: dict[str, str]) -> str:
    """Create a session and record the given claim verdicts on the first item."""
    created = client.post(SESSIONS.format(p=pid), json={"limit": 5}).json()
    sid = created["session_id"]
    item = created["items"][0]
    body = {"claim_verdicts": [{"key": k, "verdict": v} for k, v in verdicts.items()]}
    r = client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}", json=body)
    assert r.status_code == 200, r.text
    return sid


def _plan_input(pid):
    with session_scope() as s:
        return dpi.build_planning_input(pid, session=s)


def _entry(pi, seg_id):
    for e in pi.entries:
        if e.segment_id == seg_id:
            return e
    raise AssertionError("segment missing from the planning input")


# ---------------------------------------------------------------------------
# The claim is withheld, the shot survives
# ---------------------------------------------------------------------------

def test_a_wrong_claim_is_withheld_from_the_planner(client):
    pid, seg_id, res_id = _seed(client)
    before = _entry(_plan_input(pid), seg_id)
    assert "white pagoda" in before.static.visual.get("subjects", [])

    _review(client, pid, {"subjects:2": "wrong"})

    after = _entry(_plan_input(pid), seg_id)
    assert "white pagoda" not in after.static.visual.get("subjects", [])
    # The shot is still there: a wrong description is not a worthless shot.
    assert after.segment_id == seg_id
    assert after.eligibility == "eligible"


def test_a_right_claim_is_kept(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"subjects:2": "right"})
    e = _entry(_plan_input(pid), seg_id)
    assert "white pagoda" in e.static.visual.get("subjects", [])


def test_partial_correctness_keeps_the_right_claims(client):
    """Wrong on one subject, right on another: only the wrong one goes."""
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"subjects:2": "wrong", "subjects:0": "right"})

    e = _entry(_plan_input(pid), seg_id)
    subjects = e.static.visual.get("subjects", [])
    assert "white pagoda" not in subjects
    assert "small boats" in subjects
    # Untouched claims are not collateral damage either.
    assert "wooden pier" in subjects


def test_unsure_is_not_treated_as_wrong(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"subjects:2": "unsure"})
    e = _entry(_plan_input(pid), seg_id)
    assert "white pagoda" in e.static.visual.get("subjects", [])


def test_an_unreviewed_shot_is_unchanged(client):
    pid, seg_id, res_id = _seed(client)
    e = _entry(_plan_input(pid), seg_id)
    assert set(e.static.visual.get("subjects", [])) == {
        "small boats", "wooden pier", "white pagoda"}
    assert not any("review" in lim for lim in e.limitations)


# ---------------------------------------------------------------------------
# Disclosure: filtered evidence must be visible and change the digest
# ---------------------------------------------------------------------------

def test_withheld_claims_are_disclosed(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"subjects:2": "wrong"})
    e = _entry(_plan_input(pid), seg_id)
    assert any("human_review" in lim for lim in e.limitations), e.limitations
    assert any("human_review" in lim for lim in e.static.limitations), e.static.limitations


def test_filtering_changes_the_input_digest(client):
    """A plan from filtered evidence must not look identical to an unfiltered one."""
    pid, seg_id, res_id = _seed(client)
    before = _plan_input(pid).source_input_digest
    _review(client, pid, {"subjects:2": "wrong"})
    after = _plan_input(pid).source_input_digest
    assert before != after


def test_a_reviewed_but_all_right_shot_also_changes_the_digest(client):
    """The digest covers the review state, not just the removal."""
    pid, seg_id, res_id = _seed(client)
    before = _plan_input(pid).source_input_digest
    _review(client, pid, {"subjects:0": "right"})
    after = _plan_input(pid).source_input_digest
    assert before != after


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------

def test_a_judgement_of_an_old_reading_does_not_filter_the_new_one(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"subjects:2": "wrong"})

    # The model is re-run: the old row stops being current.
    with session_scope() as s:
        old = s.get(SegmentSemanticResult, res_id)
        old.is_current = False
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=old.asset_id, segment_id=seg_id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p2", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["temple"], "subjects": ["white pagoda"]},
            evidence_json={"state": "observed", "frame_ids": ["f2"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None))

    e = _entry(_plan_input(pid), seg_id)
    # The new reading's own subject is untouched by an opinion about the old one.
    assert "white pagoda" in e.static.visual.get("subjects", [])


# ---------------------------------------------------------------------------
# Nothing is rewritten
# ---------------------------------------------------------------------------

def test_the_model_reading_is_never_modified(client):
    pid, seg_id, res_id = _seed(client)
    with session_scope() as s:
        before = s.get(SegmentSemanticResult, res_id).analysis_json
    _review(client, pid, {"subjects:2": "wrong"})
    _plan_input(pid)
    with session_scope() as s:
        assert s.get(SegmentSemanticResult, res_id).analysis_json == before


def test_the_segment_and_annotation_are_never_modified(client):
    pid, seg_id, res_id = _seed(client)
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        before = (seg.user_rating, seg.status, seg.start_sec, seg.end_sec)
    _review(client, pid, {"subjects:2": "wrong"})
    _plan_input(pid)
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        assert (seg.user_rating, seg.status, seg.start_sec, seg.end_sec) == before


def test_a_wrong_action_claim_is_withheld_too(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"visible_actions:1": "wrong"})
    e = _entry(_plan_input(pid), seg_id)
    assert "birds on the rail" not in e.static.visual.get("visible_actions", [])
    assert "boats floating" in e.static.visual.get("visible_actions", [])


def test_a_wrong_scene_tag_is_withheld(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, {"scene_tags:1": "wrong"})
    e = _entry(_plan_input(pid), seg_id)
    assert "sky" not in e.static.visual.get("scene_tags", [])
    assert "lake" in e.static.visual.get("scene_tags", [])


# ---------------------------------------------------------------------------
# Multiframe claims participate as well
# ---------------------------------------------------------------------------

def test_a_wrong_motion_summary_is_withheld(client):
    with session_scope() as s:
        pass
    pid, seg_id, res_id = _seed(client)
    # Add a multiframe reading for the same shot.
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        s.add(SegmentMotionReview(
            id=_uid(), project_id=pid, asset_id=seg.asset_id, segment_id=seg_id,
            method="multiframe", status="succeeded",
            model_provider="openai_compatible", model_id="GLM", prompt_version="1.0",
            start_sec=4.0, end_sec=11.0, frame_manifest_hash="h",
            motion_type="pan", stability_risk="low",
            action_summary="camera pans left past the pagoda",
            analysis_json={"motion_type": "pan", "stability_risk": "low",
                           "action_summary": "camera pans left past the pagoda"},
            evidence_json={"method": "multiframe",
                           "frames": [{"time_sec": 5.0, "frame_id": ""}]},
            request_usage={}, is_current=True, error_code="", error_message="",
        ))

    created = client.post(SESSIONS.format(p=pid), json={"limit": 10}).json()
    sid = created["session_id"]
    motion_item = None
    for item in created["items"]:
        if item["channel"] == "multiframe":
            motion_item = item
            break
    assert motion_item is not None, "a multiframe item should be offered"

    client.patch(
        f"{SESSIONS.format(p=pid)}/{sid}/ratings/{motion_item['item_id']}",
        json={"claim_verdicts": [
            {"key": "action_summary:0", "verdict": "wrong"}]})

    e = _entry(_plan_input(pid), seg_id)
    assert e.multiframe.action_summary == "", (
        "a wrong action_summary must be blanked, not merely shortened")
    assert any("human_review" in lim for lim in e.multiframe.limitations)
