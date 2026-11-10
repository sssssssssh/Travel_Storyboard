"""TSB-370-0 — the plan input digest must reach the server from the browser.

Pre-fix state (recorded here, not invented): ``DirectorPlans.tsx`` posted

    expected_input_digest: ctx.data?.coverage ? undefined : undefined

which is ``undefined`` on both branches, so the 409 guard was never exercised by
the UI even though the backend enforced it. The fix needs a *read-only* source for
the digest that matches what the planner will compute, hence
``GET /director/plans/input-status``.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import Asset, Job, Project, Segment, Storyboard, StoryboardVersion

STATUS = "/api/projects/{p}/director/plans/input-status"
PREVIEW = "/api/projects/{p}/director/plans/preview"
CONTEXT = "/api/projects/{p}/director/context"


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(client, name="t370"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    with session_scope() as s:
        p = s.get(Project, pid)
        a = Asset(
            id=_uid(), project_id=pid, source_path=f"/nonexistent/{_uid()}.mp4",
            display_name="c.mp4", file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
            duration_sec=600.0, fps_num=30, fps_den=1, vfr_flag=False, width=640,
            height=360, rotation=0, codec="h264", pixel_format="yuv420p",
            color_primaries="", color_transfer="", color_space="", has_audio=False,
            audio_codec="", scan_status="succeeded", analysis_version="",
            analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        for i in range(3):
            s.add(Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=i * 10.0, detected_end_sec=i * 10.0 + 6,
                start_sec=i * 10.0, end_sec=i * 10.0 + 6, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="",
                parent_segment_id="", algorithm_version="1", created_at=None,
            ))
    return pid


def _counts(pid):
    with session_scope() as s:
        boards = [b.id for b in s.scalars(select(Storyboard).where(Storyboard.project_id == pid))]
        return {
            "storyboards": len(boards),
            "versions": int(s.scalar(select(func.count()).select_from(StoryboardVersion).where(
                StoryboardVersion.board_id.in_(boards))) or 0) if boards else 0,
            "jobs": int(s.scalar(select(func.count()).select_from(Job).where(
                Job.project_id == pid)) or 0),
        }


def test_input_status_returns_a_digest(client):
    pid = _seed(client)
    r = client.get(STATUS.format(p=pid))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == pid
    assert len(body["input_digest"]) == 64
    assert body["segment_count"] == 3


def test_input_status_digest_equals_the_one_the_preview_uses(client):
    """The whole point: the browser's digest must not be a different quantity."""
    pid = _seed(client)
    status = client.get(STATUS.format(p=pid)).json()
    plan = client.post(PREVIEW.format(p=pid), json={"target_duration_sec": 15}).json()
    assert plan["source_input_digest"] == status["input_digest"]


def test_a_preview_using_the_status_digest_is_not_rejected(client):
    pid = _seed(client)
    digest = client.get(STATUS.format(p=pid)).json()["input_digest"]
    r = client.post(PREVIEW.format(p=pid), json={
        "target_duration_sec": 15, "expected_input_digest": digest})
    assert r.status_code == 200, r.text


def test_editing_a_human_note_makes_the_old_digest_stale(client):
    """The exact scenario the failing UI could never trigger."""
    pid = _seed(client)
    digest = client.get(STATUS.format(p=pid)).json()["input_digest"]

    with session_scope() as s:
        from app.models import UserAnnotation
        seg = s.scalars(select(Segment).where(Segment.project_id == pid)).first()
        s.add(UserAnnotation(id=_uid(), segment_id=seg.id, rating="like",
                             note="第二次去这里", in_sec=None, out_sec=None,
                             created_at=None, updated_at=None))

    r = client.post(PREVIEW.format(p=pid), json={
        "target_duration_sec": 15, "expected_input_digest": digest})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DIRECTOR_INPUT_CHANGED"
    # And the fresh digest now works.
    fresh = client.get(STATUS.format(p=pid)).json()["input_digest"]
    assert fresh != digest
    ok = client.post(PREVIEW.format(p=pid), json={
        "target_duration_sec": 15, "expected_input_digest": fresh})
    assert ok.status_code == 200


def test_changing_a_shot_window_makes_the_old_digest_stale(client):
    pid = _seed(client)
    digest = client.get(STATUS.format(p=pid)).json()["input_digest"]
    with session_scope() as s:
        seg = s.scalars(select(Segment).where(Segment.project_id == pid)).first()
        seg.end_sec = seg.end_sec - 1.0
    r = client.post(PREVIEW.format(p=pid), json={
        "target_duration_sec": 15, "expected_input_digest": digest})
    assert r.status_code == 409


def test_input_status_reports_evidence_and_lock_state(client):
    pid = _seed(client)
    body = client.get(STATUS.format(p=pid)).json()
    assert body["evidence_channels"]["static"] == 0
    assert body["evidence_channels"]["video_native"] == 0
    assert body["locked_segments"] == 0
    assert body["segments_with_any_valid_reading"] == 0


def test_input_status_is_read_only(client):
    pid = _seed(client)
    before = _counts(pid)
    client.get(STATUS.format(p=pid))
    client.get(STATUS.format(p=pid))
    assert _counts(pid) == before
    assert before["storyboards"] == 0


def test_input_status_respects_max_candidates_consistently(client):
    pid = _seed(client)
    s1 = client.get(STATUS.format(p=pid) + "?max_candidates=2").json()
    assert s1["segment_count"] == 2
    # A preview with the same max_candidates must agree on the digest.
    plan = client.post(PREVIEW.format(p=pid), json={
        "target_duration_sec": 10, "max_candidates": 2,
        "expected_input_digest": s1["input_digest"]})
    assert plan.status_code == 200, plan.text


def test_unknown_project_is_404(client):
    assert client.get(STATUS.format(p=str(uuid.uuid4()))).status_code == 404
