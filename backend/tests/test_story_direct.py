"""Audit P1-06 — direct AI analysis must be a real director's pass.

The model is stubbed, so these tests pin down the *contract* around it:
representative selection, coverage accounting, identifier validation (invented
ids must never reach the database), JSON recovery, and persistence.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Frame, ReviewRequest, Segment
from app.paths import frames_dir
from app.services import story_planner
from app.services.story_planner import (
    _extract_json,
    _persist_requests,
    _select_representative_frames,
    _validate_analysis,
    direct_analyze,
)


def _jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (90, 110, 130)).save(path, format="JPEG")


@pytest.fixture()
def seeded(client):
    """A project with several assets/segments/frames, deliberately varied."""
    res = client.post("/api/projects", json={"name": "story-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    layout = [
        ("alpha.mp4", 3, "favorite"),
        ("bravo.mp4", 2, "like"),
        ("charlie.mp4", 2, "pending"),
        ("delta.mp4", 1, "exclude"),
    ]
    assets: dict[str, str] = {}
    segments: list[tuple[str, str, str]] = []  # (seg_id, asset_id, rating)

    for name, n_segs, rating in layout:
        asset_id = str(uuid.uuid4())
        with session_scope() as s:
            s.add(
                Asset(
                    id=asset_id,
                    project_id=project_id,
                    source_path=f"C:/footage/{name}",
                    display_name=name,
                    duration_sec=60.0,
                    scan_status="succeeded",
                )
            )
        assets[name] = asset_id

        for k in range(n_segs):
            seg_id = str(uuid.uuid4())
            start = float(k * 10)
            with session_scope() as s:
                s.add(
                    Segment(
                        id=seg_id,
                        project_id=project_id,
                        asset_id=asset_id,
                        detected_start_sec=start,
                        detected_end_sec=start + 10,
                        start_sec=start,
                        end_sec=start + 10,
                        boundary_reason="hard_cut",
                        user_rating=rating,
                    )
                )
            segments.append((seg_id, asset_id, rating))

            for i in range(3):
                frame_id = str(uuid.uuid4())
                jpg = frames_dir(project_id, asset_id) / f"{frame_id}.jpg"
                _jpeg(jpg)
                with session_scope() as s:
                    s.add(
                        Frame(
                            id=frame_id,
                            segment_id=seg_id,
                            asset_id=asset_id,
                            time_sec=start + i * 3.0,
                            image_path=jpg.resolve().as_posix(),
                        )
                    )

    return project_id, assets, segments


# --- JSON recovery -----------------------------------------------------------

def test_extract_json_plain_object():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_from_code_fence():
    raw = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps!'
    assert _extract_json(raw) == {"a": 1}


def test_extract_json_embedded_in_prose():
    raw = 'Sure. {"a": 1, "b": [2]} and that is all.'
    assert _extract_json(raw) == {"a": 1, "b": [2]}


def test_extract_json_returns_none_for_garbage():
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None
    assert _extract_json("[1,2,3]") is None


# --- representative selection + coverage ------------------------------------

def test_selection_prefers_user_rated_shots(seeded):
    project_id, assets, segments = seeded
    picked, coverage = _select_representative_frames(project_id, max_images=6, max_segments_sampled=20)

    assert picked, "expected at least one frame"
    # Excluded footage must never be sampled.
    excluded_asset = assets["delta.mp4"]
    assert all(p["asset_id"] != excluded_asset for p in picked)
    # The favourite asset should appear (it is ranked first).
    assert assets["alpha.mp4"] in {p["asset_id"] for p in picked}


def test_selection_spreads_across_assets(seeded):
    project_id, assets, _ = seeded
    picked, _ = _select_representative_frames(project_id, max_images=3, max_segments_sampled=20)
    # Round-robin means 3 images should come from 3 different videos, not 3 from one.
    assert len({p["asset_id"] for p in picked}) == 3


def test_every_picked_frame_carries_its_binding(seeded):
    project_id, _, _ = seeded
    picked, _ = _select_representative_frames(project_id, max_images=5, max_segments_sampled=20)
    for p in picked:
        assert p["asset_id"] and p["segment_id"] and p["frame_id"]
        assert p["end_sec"] > p["start_sec"]
        assert p["start_sec"] <= p["frame_time_sec"] <= p["end_sec"] or p["frame_time_sec"] >= 0
        assert p["path"].is_file()


def test_coverage_reports_what_was_not_seen(seeded):
    project_id, _, segments = seeded
    picked, coverage = _select_representative_frames(project_id, max_images=2, max_segments_sampled=20)

    # 8 segments total, 1 asset is excluded -> 7 candidates.
    assert coverage["candidate_segments"] == 7
    assert coverage["images_sent"] == 2
    assert coverage["segments_covered_by_images"] == 2
    assert coverage["segments_not_covered"] == 5
    assert coverage["complete"] is False
    assert coverage["library_assets"] == 4


def test_eligible_coverage_does_not_claim_excluded_library_is_seen(seeded):
    project_id, _, _ = seeded
    _, coverage = _select_representative_frames(project_id, max_images=50, max_segments_sampled=50)
    assert coverage["segments_covered_by_images"] == coverage["candidate_segments"]
    assert coverage["assets_covered_by_images"] < coverage["library_assets"]
    assert coverage["complete"] is False


# --- validation of model output ---------------------------------------------

def _allowed(seg_id: str, asset_id: str, start=0.0, end=10.0) -> dict:
    return {seg_id: {"asset_id": asset_id, "start_sec": start, "end_sec": end}}


def test_validate_accepts_a_well_formed_request():
    allowed = _allowed("seg-1", "asset-1")
    accepted, problems = _validate_analysis(
        {"review_requests": [{"segment_id": "seg-1", "asset_id": "asset-1",
                              "start_sec": 2, "end_sec": 5, "priority": "high",
                              "reason": "check shake"}]},
        allowed,
    )
    assert problems == []
    assert len(accepted) == 1
    assert accepted[0]["start_sec"] == 2 and accepted[0]["end_sec"] == 5
    assert accepted[0]["priority"] == "high"


def test_validate_rejects_invented_segment_ids():
    accepted, problems = _validate_analysis(
        {"review_requests": [{"segment_id": "made-up", "asset_id": "asset-1"}]},
        _allowed("seg-1", "asset-1"),
    )
    assert accepted == []
    assert any("未知" in p for p in problems)


def test_validate_rejects_mismatched_asset_id():
    accepted, problems = _validate_analysis(
        {"review_requests": [{"segment_id": "seg-1", "asset_id": "wrong-asset"}]},
        _allowed("seg-1", "asset-1"),
    )
    assert accepted == []
    assert any("不匹配" in p for p in problems)


def test_validate_clamps_out_of_range_times():
    accepted, _ = _validate_analysis(
        {"review_requests": [{"segment_id": "seg-1", "asset_id": "asset-1",
                              "start_sec": -50, "end_sec": 999}]},
        _allowed("seg-1", "asset-1", 0.0, 10.0),
    )
    assert len(accepted) == 1
    assert accepted[0]["start_sec"] == 0.0
    assert accepted[0]["end_sec"] == 10.0


def test_validate_rejects_empty_and_nan_ranges():
    allowed = _allowed("seg-1", "asset-1", 0.0, 10.0)
    accepted, problems = _validate_analysis(
        {"review_requests": [
            {"segment_id": "seg-1", "asset_id": "asset-1", "start_sec": 5, "end_sec": 5},
            {"segment_id": "seg-1", "asset_id": "asset-1", "start_sec": "abc", "end_sec": 3},
        ]},
        allowed,
    )
    assert accepted == []
    assert len(problems) >= 2


def test_validate_deduplicates_identical_requests():
    accepted, _ = _validate_analysis(
        {"review_requests": [
            {"segment_id": "seg-1", "asset_id": "asset-1", "start_sec": 1, "end_sec": 4},
            {"segment_id": "seg-1", "asset_id": "asset-1", "start_sec": 1, "end_sec": 4},
        ]},
        _allowed("seg-1", "asset-1"),
    )
    assert len(accepted) == 1


def test_validate_handles_non_list_payloads():
    accepted, problems = _validate_analysis({"review_requests": "nope"}, _allowed("s", "a"))
    assert accepted == []
    assert problems
    accepted, problems = _validate_analysis({"stories": "nope"}, _allowed("s", "a"))
    assert accepted == [] and any("stories" in p for p in problems)


def test_validate_normalises_unknown_priority():
    accepted, _ = _validate_analysis(
        {"review_requests": [{"segment_id": "seg-1", "asset_id": "asset-1",
                              "start_sec": 1, "end_sec": 4, "priority": "URGENT!!"}]},
        _allowed("seg-1", "asset-1"),
    )
    assert accepted[0]["priority"] == "medium"


# --- persistence -------------------------------------------------------------

def test_persist_requests_creates_rows(seeded):
    project_id, assets, _ = seeded
    created = _persist_requests(
        project_id,
        [{"asset_id": assets["alpha.mp4"], "segment_id": "seg-x", "start_sec": 1.0,
          "end_sec": 3.0, "priority": "high", "reason": "test"}],
    )
    assert created == 1
    with session_scope() as s:
        row = s.scalars(
            select(ReviewRequest).where(ReviewRequest.project_id == project_id)
        ).first()
    assert row is not None and row.source == "direct" and row.status == "pending"


def test_persist_requests_skips_duplicates(seeded):
    project_id, assets, _ = seeded
    req = {"asset_id": assets["alpha.mp4"], "segment_id": "seg-y", "start_sec": 1.0,
           "end_sec": 3.0, "priority": "high", "reason": "test"}
    assert _persist_requests(project_id, [req]) == 1
    assert _persist_requests(project_id, [req]) == 0


# --- end-to-end with a stubbed model ----------------------------------------

def test_direct_analyze_requires_configuration(seeded, monkeypatch):
    project_id, _, _ = seeded
    monkeypatch.setattr(story_planner.model_client, "is_configured", lambda: False)
    result = direct_analyze(project_id)
    assert result["configured"] is False
    assert result["review_requests"] == []


def test_direct_analyze_happy_path(seeded, monkeypatch):
    project_id, assets, _ = seeded
    seen: dict = {}

    monkeypatch.setattr(story_planner.model_client, "is_configured", lambda: True)

    def fake_chat(messages, **kwargs):
        seen["messages"] = messages
        # Echo back a request for a real segment, using the ids from the prompt.
        text = messages[-1]["content"][0]["text"]
        seg_line = next(l for l in text.splitlines() if "segment_id=" in l)
        seg = seg_line.split("segment_id=")[1].split()[0]
        asset = seg_line.split("asset_id=")[1].split()[0]
        return json.dumps(
            {
                "report": "Discussed only the shots provided.",
                "stories": [
                    {"title": "Coast to mountains", "logline": "move inland",
                     "segment_ids": [seg, "invented-id"]}
                ],
                "review_requests": [
                    {"asset_id": asset, "segment_id": seg, "start_sec": 0.0,
                     "end_sec": 5.0, "priority": "high", "reason": "verify motion"},
                    {"asset_id": "bogus", "segment_id": "bogus", "start_sec": 0,
                     "end_sec": 1},
                ],
            }
        )

    monkeypatch.setattr(story_planner.model_client, "chat", fake_chat)

    result = direct_analyze(project_id, max_images=3, max_segments_sampled=10)

    assert result["configured"] is True
    assert result["created"] == 1
    assert len(result["review_requests"]) == 1
    assert result["coverage"]["images_sent"] == 3
    # The invented id is dropped from the story but reported.
    assert result["stories"][0]["unknown_segment_ids"] == ["invented-id"]
    assert result["stories"][0]["segment_ids"] != ["invented-id"]
    # Prompt carried the binding manifest.
    prompt_text = seen["messages"][-1]["content"][0]["text"]
    assert "IMAGE INDEX" in prompt_text
    assert "COVERAGE" in prompt_text


def test_direct_analyze_retries_on_non_json(seeded, monkeypatch):
    project_id, _, _ = seeded
    monkeypatch.setattr(story_planner.model_client, "is_configured", lambda: True)
    calls = {"n": 0}

    def flaky_chat(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Sorry, I cannot do that."
        # On retry the final message is the plain-text correction, so look for
        # the original image manifest anywhere in the conversation.
        text = ""
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text += part.get("text", "") + "\n"
            elif isinstance(content, str):
                text += content + "\n"
        seg_line = next(l for l in text.splitlines() if "segment_id=" in l)
        seg = seg_line.split("segment_id=")[1].split()[0]
        asset = seg_line.split("asset_id=")[1].split()[0]
        return json.dumps({"report": "ok", "stories": [],
                           "review_requests": [{"asset_id": asset, "segment_id": seg,
                                                "start_sec": 0, "end_sec": 3}]})

    monkeypatch.setattr(story_planner.model_client, "chat", flaky_chat)

    result = direct_analyze(project_id, max_images=2, max_segments_sampled=10)
    assert calls["n"] == 2
    assert result["retried"] is True
    assert result["created"] == 1


def test_direct_analyze_survives_persistent_garbage(seeded, monkeypatch):
    project_id, _, _ = seeded
    monkeypatch.setattr(story_planner.model_client, "is_configured", lambda: True)
    monkeypatch.setattr(story_planner.model_client, "chat", lambda *a, **k: "still not json")

    result = direct_analyze(project_id, max_images=2, max_segments_sampled=10)
    assert result["review_requests"] == []
    assert result["created"] == 0
    assert result["problems"]
    # The raw reply is surfaced so the user can see what happened.
    assert "still not json" in result["raw"]


def test_budget_one_prefers_favorite_even_if_created_later(seeded):
    pid, _, segments = seeded
    with session_scope() as s:
        for seg_id, _, rating in segments:
            s.get(Segment, seg_id).user_rating = 'pending' if rating == 'favorite' else 'favorite' if rating == 'pending' else rating
    picked, _ = _select_representative_frames(pid, 1, 24)
    assert picked[0]['user_rating'] == 'favorite'


def test_fairness_happens_before_candidate_cap(client):
    pid = client.post('/api/projects', json={'name': 'large-library'}).json()['id']
    for i in range(60):
        aid = str(uuid.uuid4())
        with session_scope() as s:
            s.add(Asset(id=aid, project_id=pid, source_path=f'/v/{i}.mp4', display_name=f'{i:03}.mp4'))
        for j in range(50 if i == 0 else 1):
            sid, fid = str(uuid.uuid4()), str(uuid.uuid4())
            path = frames_dir(pid, aid) / f'{fid}.jpg'
            _jpeg(path)
            with session_scope() as s:
                s.add(Segment(id=sid, project_id=pid, asset_id=aid, start_sec=j*2, end_sec=j*2+2))
                s.flush()
                s.add(Frame(id=fid, segment_id=sid, asset_id=aid, time_sec=j*2+1, image_path=str(path)))
    picked, coverage = _select_representative_frames(pid, 12, 24)
    assert len({p['asset_id'] for p in picked}) == 12
    assert coverage['images_sent'] == 12
    assert coverage['assets_not_covered'] == 48
    assert coverage['complete'] is False
    again, _ = _select_representative_frames(pid, 12, 24)
    assert [p['frame_id'] for p in picked] == [p['frame_id'] for p in again]
    picked[0]['path'].unlink()
    after, coverage = _select_representative_frames(pid, 12, 24)
    assert all(p['path'].is_file() for p in after)
    assert coverage['assets_covered_by_images'] == len({p['asset_id'] for p in after})


def test_direct_request_persistence_rechecks_current_segment(seeded):
    pid, _, segments = seeded
    sid, aid, _ = segments[0]
    with session_scope() as s: s.get(Segment, sid).status = 'needs_review'
    assert _persist_requests(pid, [{'asset_id': aid, 'segment_id': sid, 'start_sec': 1,
                                   'end_sec': 5, 'priority': 'high', 'reason': 'now stale'}]) == 0
    with session_scope() as s:
        assert not s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).all()
