"""TSB-371 — retiring a source must be a label, not a delete.

The user said "不要了" about a 1.9 GB file that is no longer on disk. The tempting
implementation — delete the asset and its segments — would destroy:

* 2 model readings that were paid for,
* the record of what was once proposed in this library,
* any human opinion attached to those shots (zero here, but not by rule).

So retiring only changes a label, and the tests below check the survivors one by
one. They also pin the two ways this could quietly corrupt other numbers:

* a retired shot must not become "human_excluded" — nobody judged it;
* a shot with a human opinion must never be reclassified by a file vanishing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentSemanticResult,
    UserAnnotation,
)
from app.services import evaluation_retire as retire

import os


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(client, *, rating: str = "pending"):
    pid = client.post("/api/projects", json={"name": "retire"}).json()["id"]
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid,
            source_path=f"/gone/{_uid()}.mp4", display_name="DJI_0007_D.MP4",
            file_size_bytes=1_988_000_000, mtime_ns=1, quick_fingerprint="f",
            duration_sec=60.0, fps_num=25, fps_den=1, vfr_flag=False,
            width=3840, height=2160, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None, scanned_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()
        seg_ids = []
        for i in range(3):
            seg = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=i * 10.0, detected_end_sec=i * 10.0 + 8,
                start_sec=i * 10.0, end_sec=i * 10.0 + 8, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating=rating,
                status="candidate", frame_refs=[], proxy_ref="",
                parent_segment_id="", algorithm_version="1", created_at=None,
            )
            s.add(seg)
            s.flush()
            seg_ids.append(seg.id)
        for seg_id in seg_ids[:2]:
            s.add(SegmentSemanticResult(
                id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg_id,
                status="succeeded", model_provider="openai_compatible", model_id="GLM",
                prompt_version="p", schema_version="1.0", source_signature=_uid(),
                frame_manifest_hash="h",
                analysis_json={"scene_tags": ["lake"], "subjects": ["pier"]},
                evidence_json={"state": "observed", "frame_ids": ["f1"]},
                request_usage={}, is_current=True, error_code="", error_message="",
                created_at=None, updated_at=None,
            ))
        return pid, a.id, seg_ids


def _counts(pid: str) -> dict[str, int]:
    with session_scope() as s:
        return {
            "assets": int(s.scalar(select(func.count()).select_from(Asset)
                                   .where(Asset.project_id == pid)) or 0),
            "segments": int(s.scalar(select(func.count()).select_from(Segment)
                                     .where(Segment.project_id == pid)) or 0),
            "readings": int(s.scalar(
                select(func.count()).select_from(SegmentSemanticResult)
                .where(SegmentSemanticResult.project_id == pid)) or 0),
        }


# ---------------------------------------------------------------------------
# Nothing is deleted
# ---------------------------------------------------------------------------

def test_retiring_deletes_nothing(client):
    pid, aid, seg_ids = _seed(client)
    before = _counts(pid)
    assert before == {"assets": 1, "segments": 3, "readings": 2}

    r = client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire",
                    json={"note": "用户确认不要了"})
    assert r.status_code == 200, r.text

    assert _counts(pid) == before, "retiring must not remove any row"
    body = r.json()
    assert body["preserved"] == {"segments": 3, "model_readings": 2,
                                 "annotations": 0}
    assert body["reversible"] is True


def test_the_asset_is_marked_retired(client):
    pid, aid, seg_ids = _seed(client)
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire",
                json={"note": "不要了"})
    with session_scope() as s:
        asset = s.get(Asset, aid)
        assert asset.source_state == "retired"
        assert asset.source_retired_at is not None
        assert asset.source_retired_note == "不要了"


def test_the_shots_become_excluded_with_a_distinct_reason(client):
    """Not 'human_excluded': nobody judged these shots, the file simply went."""
    pid, aid, seg_ids = _seed(client)
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire", json={})

    rows = client.get(
        f"/api/projects/{pid}/director/candidates?eligibility=all&limit=50").json()
    assert rows["total"] == 3
    for item in rows["items"]:
        assert item["eligibility"] == "excluded"
        assert "source_retired_by_user" in item["exclusion_reasons"]
        assert "human_excluded" not in item["exclusion_reasons"]


def test_a_retired_shot_is_not_offered_by_default(client):
    pid, aid, seg_ids = _seed(client)
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire", json={})
    rows = client.get(f"/api/projects/{pid}/director/candidates").json()
    assert rows["total"] == 0


def test_an_active_shot_is_unaffected(client):
    pid, aid, seg_ids = _seed(client)
    rows = client.get(
        f"/api/projects/{pid}/director/candidates?eligibility=all&limit=50").json()
    for item in rows["items"]:
        assert "source_retired_by_user" not in item["exclusion_reasons"]


# ---------------------------------------------------------------------------
# A human opinion outranks a missing file
# ---------------------------------------------------------------------------

def test_a_human_exclusion_is_not_overwritten_by_retirement(client):
    pid, aid, seg_ids = _seed(client, rating="exclude")
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire", json={})
    rows = client.get(
        f"/api/projects/{pid}/director/candidates?eligibility=all&limit=50").json()
    for item in rows["items"]:
        # The human's reason must survive; the file vanishing is additive.
        assert "human_excluded" in item["exclusion_reasons"]


def test_a_human_preference_is_never_rewritten(client):
    pid, aid, seg_ids = _seed(client, rating="favorite")
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire", json={})
    with session_scope() as s:
        for seg_id in seg_ids:
            assert s.get(Segment, seg_id).user_rating == "favorite"


# ---------------------------------------------------------------------------
# Reversible
# ---------------------------------------------------------------------------

def test_a_retirement_can_be_undone(client):
    pid, aid, seg_ids = _seed(client)
    client.post(f"/api/projects/{pid}/evaluation/assets/{aid}/retire", json={})
    assert client.get(
        f"/api/projects/{pid}/director/candidates").json()["total"] == 0

    r = client.delete(f"/api/projects/{pid}/evaluation/assets/{aid}/retire")
    assert r.status_code == 200
    with session_scope() as s:
        assert s.get(Asset, aid).source_state == "active"
    assert client.get(
        f"/api/projects/{pid}/director/candidates").json()["total"] == 3


def test_retiring_an_unknown_asset_is_404(client):
    pid, aid, seg_ids = _seed(client)
    r = client.post(f"/api/projects/{pid}/evaluation/assets/{_uid()}/retire", json={})
    assert r.status_code == 404


def test_another_projects_asset_cannot_be_retired(client):
    pid_a, aid_a, _ = _seed(client)
    pid_b, aid_b, _ = _seed(client)
    r = client.post(f"/api/projects/{pid_b}/evaluation/assets/{aid_a}/retire", json={})
    assert r.status_code == 404
    with session_scope() as s:
        assert s.get(Asset, aid_a).source_state == "active"
