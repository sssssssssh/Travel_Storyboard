"""TSB-371-E — close the loop: what the human judged must change what happens next.

Before this, the review collected opinions and produced a report that nothing read.
A reviewer could mark every sentence wrong and the planner would still use the same
readings unchanged. That makes the whole exercise record-keeping.

The rule here is deliberately narrow and reversible:

* a shot the reviewer marked **wrong** gets downgraded for planning, with the
  reason recorded, so it is not silently reused as if it were trusted evidence;
* the downgrade is a *planning* signal only — the stored model reading, the
  storyboard, the annotations and the original ratings are never rewritten;
* the review's own tables remain the source of truth, so undoing a judgement
  restores the shot;
* a shot nobody reviewed is untouched: silence is not disapproval.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import (
    Asset,
    EvaluationItem,
    EvaluationRating,
    EvaluationSession,
    Project,
    Segment,
    SegmentSemanticResult,
)
from app.services import director_candidates as dc
from app.services import evaluation_feedback as fb

SESSIONS = "/api/projects/{p}/evaluation/sessions"


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(client, name="t371e"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    # A present file: a shot whose source is missing cannot be reviewed at all,
    # and the feedback loop is about reviewed shots.
    root = Path(os.environ.get("TEMP") or ".") / "tsb371-feedback-src"
    root.mkdir(parents=True, exist_ok=True)
    src = root / f"{pid[:8]}.mp4"
    if not src.exists():
        src.write_bytes(b"\x00" * 64)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src),
            display_name="c.mp4", file_size_bytes=src.stat().st_size,
            mtime_ns=src.stat().st_mtime_ns, quick_fingerprint="f",
            duration_sec=60.0, fps_num=25, fps_den=1, vfr_flag=False, width=320,
            height=180, rotation=0, codec="h264", pixel_format="yuv420p",
            color_primaries="", color_transfer="", color_space="", has_audio=False,
            audio_codec="", scan_status="succeeded", analysis_version="",
            analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        seg = Segment(
            id=_uid(), asset_id=a.id, project_id=pid,
            detected_start_sec=0.0, detected_end_sec=4.0,
            start_sec=0.0, end_sec=4.0, is_trimmed=False,
            boundary_reason="", quality_metrics={"blur_avg": 100.0},
            content_tags={}, story_role="", user_rating="pending",
            status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
            algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        res = SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"], "subjects": ["pier"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        )
        s.add(res)
        s.flush()
        return pid, seg.id, res.id


def _review(client, pid, judgements: list[dict]) -> str:
    """Create a session and apply the given per-claim verdicts."""
    created = client.post(SESSIONS.format(p=pid), json={"limit": 5}).json()
    sid = created["session_id"]
    item = created["items"][0]
    r = client.patch(
        f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
        json=judgements[0])
    assert r.status_code == 200, r.text
    return sid


# ---------------------------------------------------------------------------
# E34 — a wrong claim downgrades the shot, without rewriting evidence
# ---------------------------------------------------------------------------

def test_a_shot_judged_wrong_is_downgraded_for_planning(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [
                               {"key": "subjects:0", "verdict": "wrong"},
                           ]}])

    with session_scope() as s:
        verdict = fb.review_verdict(s, pid, seg_id)
    assert verdict["state"] == "wrong"
    assert verdict["wrong"] == 1
    # The planner sees a reason, not a silent change.
    assert "review_judged_wrong" in verdict["planning_reasons"]


def test_a_shot_judged_right_is_not_downgraded(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, [{"subject": 2,
                           "claim_verdicts": [
                               {"key": "subjects:0", "verdict": "right"},
                               {"key": "scene_tags:0", "verdict": "right"},
                           ]}])
    with session_scope() as s:
        verdict = fb.review_verdict(s, pid, seg_id)
    assert verdict["state"] == "trusted"
    assert verdict["planning_reasons"] == []


def test_an_unreviewed_shot_is_left_alone(client):
    """Silence is not disapproval: no review must not become a downgrade."""
    pid, seg_id, res_id = _seed(client)
    with session_scope() as s:
        verdict = fb.review_verdict(s, pid, seg_id)
    assert verdict["state"] == "unreviewed"
    assert verdict["planning_reasons"] == []


def test_unsure_alone_does_not_downgrade(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, [{"subject": None,
                           "claim_verdicts": [
                               {"key": "subjects:0", "verdict": "unsure"},
                           ]}])
    with session_scope() as s:
        verdict = fb.review_verdict(s, pid, seg_id)
    assert verdict["state"] == "unreviewed"
    assert verdict["planning_reasons"] == []


# ---------------------------------------------------------------------------
# E35 — the evidence itself is never rewritten
# ---------------------------------------------------------------------------

def test_reviewing_never_rewrites_the_model_reading(client):
    pid, seg_id, res_id = _seed(client)
    with session_scope() as s:
        before = s.get(SegmentSemanticResult, res_id).analysis_json
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]}])
    with session_scope() as s:
        row = s.get(SegmentSemanticResult, res_id)
        assert row.analysis_json == before
        # Still the current reading: a review is an opinion about it, not a re-run.
        assert row.is_current is True


def test_reviewing_never_rewrites_the_segment_or_annotation(client):
    pid, seg_id, res_id = _seed(client)
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        before = (seg.user_rating, seg.status, seg.start_sec, seg.end_sec)
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]}])
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        assert (seg.user_rating, seg.status, seg.start_sec, seg.end_sec) == before


def test_reviewing_never_touches_the_storyboard(client):
    pid, seg_id, res_id = _seed(client)
    client.post(f"/api/projects/{pid}/storyboards", json={"name": "b"})
    with session_scope() as s:
        from app.models import Storyboard
        before = int(s.scalar(
            select(__import__("sqlalchemy").func.count()).select_from(Storyboard)
            .where(Storyboard.project_id == pid)) or 0)
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]}])
    with session_scope() as s:
        from app.models import Storyboard
        after = int(s.scalar(
            select(__import__("sqlalchemy").func.count()).select_from(Storyboard)
            .where(Storyboard.project_id == pid)) or 0)
    assert after == before


# ---------------------------------------------------------------------------
# E36 — the downgrade is reversible
# ---------------------------------------------------------------------------

def test_changing_a_verdict_to_right_restores_the_shot(client):
    pid, seg_id, res_id = _seed(client)
    created = client.post(SESSIONS.format(p=pid), json={"limit": 5}).json()
    sid = created["session_id"]
    item = created["items"][0]

    client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                 json={"subject": 0,
                       "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]})
    with session_scope() as s:
        assert fb.review_verdict(s, pid, seg_id)["state"] == "wrong"

    client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                 json={"subject": 2,
                       "claim_verdicts": [{"key": "subjects:0", "verdict": "right"}]})
    with session_scope() as s:
        assert fb.review_verdict(s, pid, seg_id)["state"] == "trusted"


def test_a_superseded_rating_stops_counting(client):
    """A judgement about an old reading must not grade a new one."""
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]}])
    with session_scope() as s:
        old = s.get(SegmentSemanticResult, res_id)
        old.is_current = False
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=old.asset_id, segment_id=seg_id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p2", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h", analysis_json={"scene_tags": ["temple"]},
            evidence_json={"state": "observed", "frame_ids": ["f2"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None))
    with session_scope() as s:
        verdict = fb.review_verdict(s, pid, seg_id)
    assert verdict["state"] == "unreviewed"
    assert verdict["planning_reasons"] == []


# ---------------------------------------------------------------------------
# The planner actually consumes it
# ---------------------------------------------------------------------------

def test_the_candidate_list_surfaces_the_review_state(client):
    pid, seg_id, res_id = _seed(client)
    _review(client, pid, [{"subject": 0,
                           "claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]}])
    # Default eligibility is "eligible"; the fixture shot has no reading channel
    # enrichment, so ask for all and check the row is present.
    rows = client.get(f"/api/projects/{pid}/director/candidates?eligibility=all").json()
    items = rows.get("items") or rows.get("candidates") or []
    assert items, f"the candidate list should contain the shot; got {rows}"
    row = items[0]
    assert row["review_state"] == "wrong"
    assert "review_judged_wrong" in (row.get("review_reasons") or [])
