"""TSB-370-D — the model-mode API contract.

Every row here is a refusal path, because the dangerous directions are all
"something left the machine that should not have". The tests count outbound
attempts instead of trusting a status code.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult

PREVIEW = "/api/projects/{p}/director/plans/preview"


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(client, name="t370api"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    with session_scope() as s:
        p = s.get(Project, pid)
        a = Asset(
            id=_uid(), project_id=pid, source_path=f"/nonexistent/{_uid()}.mp4",
            display_name="c.mp4", file_size_bytes=1, mtime_ns=1,
            quick_fingerprint="f", duration_sec=600.0, fps_num=30, fps_den=1,
            vfr_flag=False, width=640, height=360, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        for i in range(3):
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=i * 10.0, detected_end_sec=i * 10.0 + 6,
                start_sec=i * 10.0, end_sec=i * 10.0 + 6, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="",
                parent_segment_id="", algorithm_version="1", created_at=None,
            )
            s.add(g)
            s.flush()
            if i == 0:
                s.add(SegmentSemanticResult(
                    id=_uid(), project_id=pid, asset_id=a.id, segment_id=g.id,
                    status="succeeded", model_provider="openai_compatible",
                    model_id="GLM", prompt_version="p", schema_version="1.0",
                    source_signature=_uid(), frame_manifest_hash="h",
                    analysis_json={"scene_tags": ["lake"]},
                    evidence_json={"state": "observed", "frame_ids": ["f1"]},
                    request_usage={}, is_current=True, error_code="",
                    error_message="", created_at=None, updated_at=None))
    return pid


@pytest.fixture()
def no_outbound(monkeypatch):
    """Fail the test if anything tries to open a model connection."""
    calls: list[str] = []

    def _guard(*a, **kw):
        calls.append("sent")
        raise AssertionError("a model request was attempted")

    from app.services.vision.providers import openai_compatible as oc

    monkeypatch.setattr(oc, "post_model_request", _guard)
    monkeypatch.setattr(oc, "httpx", type("X", (), {"post": staticmethod(_guard),
                                                    "Client": object,
                                                    "TimeoutException": Exception,
                                                    "HTTPError": Exception}))
    return calls


def test_model_mode_without_a_grant_is_403_and_sends_nothing(client, no_outbound):
    pid = _seed(client)
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "target_duration_sec": 15})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "MODEL_CALL_NOT_AUTHORIZED"
    assert no_outbound == []


def test_model_mode_off_globally_is_403_even_with_a_grant(client, monkeypatch,
                                                          no_outbound):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "off", raising=False)
    pid = _seed(client)
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "target_duration_sec": 15})
    assert r.status_code == 403
    assert "MODEL_UPLOAD_DENIED" in r.json()["detail"]["code"] or \
           r.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"
    assert no_outbound == []


def test_a_stale_digest_is_409_before_any_model_call(client, no_outbound):
    pid = _seed(client)
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "expected_input_digest": "stale-digest", "target_duration_sec": 15})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DIRECTOR_INPUT_CHANGED"
    assert no_outbound == []


def test_offline_mode_still_works_and_sends_nothing(client, no_outbound):
    pid = _seed(client)
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "offline_rules", "target_duration_sec": 15})
    assert r.status_code == 200, r.text
    assert r.json()["planning_mode"] == "offline_rules"
    assert no_outbound == []


def test_mock_mode_remains_unreachable(client, no_outbound):
    pid = _seed(client)
    r = client.post(PREVIEW.format(p=pid), json={"planning_mode": "mock"})
    assert r.status_code == 422
    assert no_outbound == []


def test_model_mode_with_a_grant_does_not_write_a_storyboard(client, monkeypatch):
    """Even a successful model plan must stay a read-only draft."""
    from sqlalchemy import func, select

    from app.models import Storyboard, StoryboardVersion

    pid = _seed(client)

    # Stub the provider so no network is involved; the point is the side effects.
    class _Resp:
        text = ('{"variants":[{"title":"t","narrative_angle":"chronological",'
                '"logline":"","sections":[{"section_id":"s1","kind":"body",'
                '"title":"主体"}],"shots":[]}]}')

    class _Provider:
        def complete(self, request, *, authorized=None):
            assert authorized is not None, "provider must receive an auth context"
            assert request.images == [], "model planning must not send images"
            return _Resp()

    import app.api.director as director_api

    monkeypatch.setattr("app.services.vision.get_provider", lambda: _Provider())

    def _counts():
        with session_scope() as s:
            boards = [b.id for b in s.scalars(
                select(Storyboard).where(Storyboard.project_id == pid))]
            return (len(boards),
                    int(s.scalar(select(func.count()).select_from(
                        StoryboardVersion).where(
                        StoryboardVersion.board_id.in_(boards))) or 0) if boards else 0)

    before = _counts()
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "target_duration_sec": 15})
    assert r.status_code in (200, 422), r.text
    assert _counts() == before


def test_an_unparseable_model_reply_is_reported_not_replaced(client, monkeypatch):
    """The offline plan must never stand in for a failed model call."""
    pid = _seed(client)

    class _Resp:
        text = "Sorry, I cannot do that."

    class _Provider:
        def complete(self, request, *, authorized=None):
            return _Resp()

    monkeypatch.setattr("app.services.vision.get_provider", lambda: _Provider())
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "target_duration_sec": 15})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "MODEL_INVALID_OUTPUT"
    # Critically: no draft came back at all.
    assert "variants" not in r.json()


def test_a_fabricating_model_is_rejected_with_problems(client, monkeypatch):
    pid = _seed(client)

    class _Resp:
        text = ('{"variants":[{"title":"t","narrative_angle":"chronological",'
                '"logline":"","sections":[{"section_id":"s1","kind":"body",'
                '"title":"b"}],"shots":[{"segment_id":"does-not-exist",'
                '"source_in_sec":0,"source_out_sec":2,"section_id":"s1",'
                '"narrative_role":"opening","selection_reason":"x",'
                '"claims":[],"evidence_refs":[]}]}]}')

    class _Provider:
        def complete(self, request, *, authorized=None):
            return _Resp()

    monkeypatch.setattr("app.services.vision.get_provider", lambda: _Provider())
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "target_duration_sec": 15})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "PLAN_REJECTED"
    assert any(p["code"] == "UNKNOWN_SEGMENT" for p in detail["problems"])


def test_a_provider_transport_error_is_502(client, monkeypatch):
    pid = _seed(client)

    from app.services.vision.errors import ModelError

    class _Provider:
        def complete(self, request, *, authorized=None):
            raise ModelError("MODEL_TIMEOUT", "model request timed out")

    monkeypatch.setattr("app.services.vision.get_provider", lambda: _Provider())
    r = client.post(PREVIEW.format(p=pid), json={
        "planning_mode": "model", "upload_acknowledged": True,
        "target_duration_sec": 15})
    assert r.status_code == 502
    assert r.json()["detail"]["code"] == "MODEL_TIMEOUT"


def test_the_request_summary_is_available_before_sending(client):
    """The UI must be able to show what would be sent, without sending."""
    from app.services.director_plan_model import build_evidence_text
    from app.services.director_plan_input import build_planning_input
    from app.services.director_plan_schema import PlanningBrief

    pid = _seed(client)
    pi = build_planning_input(pid, max_candidates=10)
    text, summary = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert summary["sends_images"] is False
    assert summary["sends_user_notes"] is False
    assert summary["sends_paths"] is False
    assert summary["chars"] == len(text)
    assert summary["shots_listed"] == 3
