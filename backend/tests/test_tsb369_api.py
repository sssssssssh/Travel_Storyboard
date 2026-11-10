"""TSB-369-C — the read-only preview API.

The endpoint's value is what it *does not* do, so most of this file is about
proving absence: no storyboard version, no job, no operation, no annotation
change, no external call. HTTP 200 alone would prove none of that.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import (
    Asset,
    EditPlanShot,
    Job,
    PlanOperation,
    Project,
    Segment,
    SegmentSemanticResult,
    SegmentVideoObservation,
    Storyboard,
    StoryboardVersion,
    UserAnnotation,
)
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import validate_temporal_events

PREVIEW = "/api/projects/{p}/director/plans/preview"
CONTEXT = "/api/projects/{p}/director/context"


def _uid() -> str:
    return str(uuid.uuid4())


def _project(s, name="tsb369c") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project) -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name="clip.mp4", file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
        duration_sec=600.0, fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360,
        rotation=0, codec="h264", pixel_format="yuv420p", color_primaries="",
        color_transfer="", color_space="", has_audio=False, audio_codec="",
        scan_status="succeeded", analysis_version="", analyzed_fingerprint="",
        analyzed_source_signature="", analyzed_detect_config="",
        analyzed_at=None, scanned_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _segment(s, project, asset, start, end, *, rating="pending", role="",
             status="candidate", note=""):
    g = Segment(
        id=_uid(), asset_id=asset.id, project_id=project.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role=role,
        user_rating=rating, status=status, frame_refs=[], proxy_ref="",
        parent_segment_id="", algorithm_version="1", created_at=None,
    )
    s.add(g)
    s.flush()
    if note:
        s.add(UserAnnotation(id=_uid(), segment_id=g.id, rating=rating, note=note,
                             in_sec=None, out_sec=None, created_at=None, updated_at=None))
        s.flush()
    return g


def _static(s, project, asset, seg, tags=None):
    r = SegmentSemanticResult(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", model_provider="mock", model_id="m", prompt_version="p",
        schema_version="1.0", source_signature=_uid(), frame_manifest_hash="h",
        analysis_json={"scene_tags": tags or ["nature"], "subjects": ["a lake"],
                       "visible_actions": [], "lighting": "daylight",
                       "composition": "wide", "mood": ["calm"]},
        evidence_json={"state": "observed", "frame_ids": ["f1", "f2"]},
        request_usage={}, is_current=True, error_code="", error_message="",
        created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _video(s, project, asset, seg):
    clip = ClipArtifact(
        path=Path("/dev/null"), segment_id=seg.id, asset_id=asset.id,
        source_clip_start_sec=seg.start_sec, source_clip_end_sec=seg.end_sec,
        duration_sec=seg.end_sec - seg.start_sec, width=640, height=360, rotation=0,
        vfr=False, has_audio=False, size_bytes=1, sha256="c" * 64, container="mp4",
        color_unverified=False,
    )
    resp = {"clip_id": clip.clip_id, "segment_id": seg.id, "summary": "a boat crosses",
            "observed_temporal_quality": "limited", "audio_analyzed": False,
            "events": [{"event_type": "subject_action", "start_rel_sec": 0.5,
                        "end_rel_sec": 1.5, "description": "a person walks past",
                        "confidence": 0.8}]}
    v = validate_temporal_events(resp, clip=clip, segment_start_sec=seg.start_sec,
                                 segment_end_sec=seg.end_sec)
    row = SegmentVideoObservation(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", method="video_native", model_provider="mock", model_id="m",
        prompt_version="p", schema_version="1.0", protocol="native",
        source_signature="v", clip_sha256="c" * 64, version_key=_uid(),
        clip_start_sec=seg.start_sec, clip_end_sec=seg.end_sec,
        clip_duration_sec=seg.end_sec - seg.start_sec, clip_size_bytes=1,
        segment_start_sec=seg.start_sec, segment_end_sec=seg.end_sec,
        analysis_json={"summary": "a boat crosses"},
        events_json=[e.to_dict() for e in v.events],
        request_usage={}, audio_analyzed=False, color_unverified=False,
        observed_temporal_quality="limited", is_current=True,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(row)
    s.flush()
    return row


@pytest.fixture()
def seeded(client):
    pid = client.post("/api/projects", json={"name": "tsb369c", "source_roots": []}).json()["id"]
    with session_scope() as s:
        a = _asset(s, s.get(Project, pid))
        for i in range(5):
            g = _segment(s, a and s.get(Project, pid), a, i * 10.0, i * 10.0 + 6.0)
            if i < 2:
                _static(s, s.get(Project, pid), a, g)
        _segment(s, s.get(Project, pid), a, 60.0, 66.0, rating="exclude")
        _segment(s, s.get(Project, pid), a, 70.0, 76.0, status="needs_review")
    return pid


def _counts(pid: str) -> dict:
    with session_scope() as s:
        board_ids = [b.id for b in s.scalars(
            select(Storyboard).where(Storyboard.project_id == pid))]
        return {
            "storyboards": len(board_ids),
            "versions": int(s.scalar(select(func.count()).select_from(StoryboardVersion).where(
                StoryboardVersion.board_id.in_(board_ids))) or 0) if board_ids else 0,
            "jobs": int(s.scalar(select(func.count()).select_from(Job).where(
                Job.project_id == pid)) or 0),
            "operations": int(s.scalar(select(func.count()).select_from(PlanOperation).where(
                PlanOperation.project_id == pid)) or 0),
            "edit_plan": int(s.scalar(select(func.count()).select_from(EditPlanShot).where(
                EditPlanShot.project_id == pid)) or 0),
            "segments": int(s.scalar(select(func.count()).select_from(Segment).where(
                Segment.project_id == pid)) or 0),
        }


# ---------------------------------------------------------------- happy path
def test_preview_returns_a_draft(client, seeded):
    r = client.post(PREVIEW.format(p=seeded), json={
        "target_duration_sec": 20, "variant_count": 2, "style": "chronological",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["planning_mode"] == "offline_rules"
    assert body["project_id"] == seeded
    assert body["variants"], body
    assert body["schema_version"] == "1.0"


def test_preview_matches_the_digest_from_context(client, seeded):
    ctx = client.get(CONTEXT.format(p=seeded)).json()
    r = client.post(PREVIEW.format(p=seeded), json={
        "expected_input_digest": ctx["input_digest"], "target_duration_sec": 20,
    })
    assert r.status_code == 200, r.text
    assert r.json()["source_input_digest"] == ctx["input_digest"]


def test_a_matching_digest_never_produces_a_false_409(client, seeded):
    """The digest the preview computes must equal the one /context returns."""
    ctx = client.get(CONTEXT.format(p=seeded)).json()
    for max_c in (40, 40):
        r = client.post(PREVIEW.format(p=seeded), json={
            "expected_input_digest": ctx["input_digest"], "max_candidates": max_c,
            "target_duration_sec": 20,
        })
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------- write-free
def test_preview_writes_nothing(client, seeded):
    before = _counts(seeded)
    r = client.post(PREVIEW.format(p=seeded), json={"target_duration_sec": 20})
    assert r.status_code == 200, r.text
    assert _counts(seeded) == before


def test_preview_does_not_create_a_storyboard_for_a_bare_project(client):
    """The trap: get_current_version() would auto-create an empty v1."""
    pid = client.post("/api/projects", json={"name": "bare"}).json()["id"]
    with session_scope() as s:
        a = _asset(s, s.get(Project, pid))
        _segment(s, s.get(Project, pid), a, 0.0, 5.0)
    before = _counts(pid)
    assert before["storyboards"] == 0

    r = client.post(PREVIEW.format(p=pid), json={"target_duration_sec": 10})
    assert r.status_code == 200, r.text
    assert _counts(pid)["storyboards"] == 0
    assert _counts(pid)["versions"] == 0


def test_preview_does_not_touch_annotations_or_ratings(client, seeded):
    with session_scope() as s:
        before = [(g.id, g.user_rating, g.status) for g in s.scalars(
            select(Segment).where(Segment.project_id == seeded)).all()]
    client.post(PREVIEW.format(p=seeded), json={"target_duration_sec": 20})
    with session_scope() as s:
        after = [(g.id, g.user_rating, g.status) for g in s.scalars(
            select(Segment).where(Segment.project_id == seeded)).all()]
    assert before == after


# ---------------------------------------------------------------- errors
def test_unknown_project_is_404(client):
    r = client.post(PREVIEW.format(p=str(uuid.uuid4())), json={})
    assert r.status_code == 404


def test_changed_input_is_409(client, seeded):
    r = client.post(PREVIEW.format(p=seeded), json={
        "expected_input_digest": "not-the-current-digest", "target_duration_sec": 20,
    })
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "DIRECTOR_INPUT_CHANGED"
    assert detail["current_input_digest"]


def test_a_real_change_between_reads_produces_409(client, seeded):
    ctx = client.get(CONTEXT.format(p=seeded)).json()
    with session_scope() as s:
        g = s.scalars(select(Segment).where(Segment.project_id == seeded)).first()
        g.user_rating = "favorite"
    r = client.post(PREVIEW.format(p=seeded), json={
        "expected_input_digest": ctx["input_digest"], "target_duration_sec": 20,
    })
    assert r.status_code == 409


def test_model_mode_is_403_with_no_network(client, seeded):
    r = client.post(PREVIEW.format(p=seeded), json={
        "planning_mode": "model", "target_duration_sec": 20,
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "MODEL_CALL_NOT_AUTHORIZED"


@pytest.mark.parametrize("payload", [
    {"variant_count": 0},
    {"variant_count": 9},
    {"target_duration_sec": 0},
    {"max_candidates": 0},
    {"max_candidates": 9999},
    {"style": "nonsense"},
    {"planning_mode": "mock"},
])
def test_invalid_payloads_are_422(client, seeded, payload):
    r = client.post(PREVIEW.format(p=seeded), json={**payload})
    assert r.status_code == 422, f"{payload} -> {r.status_code}"


def test_mock_mode_is_not_accepted_from_http(client, seeded):
    r = client.post(PREVIEW.format(p=seeded), json={"planning_mode": "mock"})
    assert r.status_code == 422


def test_an_empty_library_returns_insufficient_evidence_not_an_error(client):
    pid = client.post("/api/projects", json={"name": "empty"}).json()["id"]
    r = client.post(PREVIEW.format(p=pid), json={"target_duration_sec": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation_status"] == "insufficient_evidence"
    assert body["variants"] == []


# ---------------------------------------------------------------- isolation
def test_another_projects_shots_never_appear(client):
    pid_a = client.post("/api/projects", json={"name": "a"}).json()["id"]
    pid_b = client.post("/api/projects", json={"name": "b"}).json()["id"]
    with session_scope() as s:
        pa, pb = s.get(Project, pid_a), s.get(Project, pid_b)
        aa, ab = _asset(s, pa), _asset(s, pb)
        ga = _segment(s, pa, aa, 0.0, 5.0)
        gb = _segment(s, pb, ab, 0.0, 5.0)
        _static(s, pb, ab, gb, tags=["secret"])

    r = client.post(PREVIEW.format(p=pid_a), json={"target_duration_sec": 10})
    assert r.status_code == 200
    blob = r.text
    assert gb.id not in blob
    assert "secret" not in blob


# ---------------------------------------------------------------- safety
def test_no_paths_or_usage_leak_from_preview(client, seeded):
    with session_scope() as s:
        a = s.scalars(select(Asset).where(Asset.project_id == seeded)).first()
        g = s.scalars(select(Segment).where(Segment.project_id == seeded)).first()
        _video(s, s.get(Project, seeded), a, g)
    r = client.post(PREVIEW.format(p=seeded), json={"target_duration_sec": 30})
    assert r.status_code == 200, r.text
    blob = r.text
    assert "/nonexistent/" not in blob
    assert "source_path" not in blob
    assert "request_usage" not in blob


def test_the_theme_is_echoed_nowhere_as_a_footage_fact(client, seeded):
    r = client.post(PREVIEW.format(p=seeded), json={
        "theme": "雪山与极光", "target_duration_sec": 20,
    })
    assert r.status_code == 200, r.text
    assert "极光" not in r.text
    assert "雪山" not in r.text


def test_two_calls_are_byte_identical(client, seeded):
    a = client.post(PREVIEW.format(p=seeded), json={"target_duration_sec": 20}).json()
    b = client.post(PREVIEW.format(p=seeded), json={"target_duration_sec": 20}).json()
    assert a == b


def test_concurrent_previews_do_not_mix_projects(client):
    ids = [client.post("/api/projects", json={"name": f"c{i}"}).json()["id"] for i in range(3)]
    for pid in ids:
        with session_scope() as s:
            a = _asset(s, s.get(Project, pid))
            _segment(s, s.get(Project, pid), a, 0.0, 5.0)
    results = [client.post(PREVIEW.format(p=pid), json={"target_duration_sec": 10})
               for pid in ids]
    for pid, r in zip(ids, results):
        assert r.status_code == 200
        assert r.json()["project_id"] == pid
        for shot in r.json()["variants"][0]["shots"] if r.json()["variants"] else []:
            assert shot["segment_id"] in {
                g.id for g in _segments_of(pid)}
    # And no project gained a storyboard from this.
    for pid in ids:
        assert _counts(pid)["storyboards"] == 0


def _segments_of(pid: str) -> list:
    with session_scope() as s:
        return s.scalars(select(Segment).where(Segment.project_id == pid)).all()
