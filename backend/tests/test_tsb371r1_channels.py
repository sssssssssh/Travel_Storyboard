"""TSB-371-R1 P1-02 — every channel must be reviewable, claim by claim.

TSB-371 only produced per-claim items for the static channel. The multiframe
reading already stores ``motion_type``, ``stability_risk`` and ``action_summary``,
and the native-video reading stores a summary plus timed events — none of it was
offered for review.

The data structures here are copied from what the writers actually persist, not
invented:

* static  -> ``SegmentSemanticResult.analysis_json`` {scene_tags, subjects,
  visible_actions, lighting, composition, mood}
* multiframe -> ``SegmentMotionReview.analysis_json`` {motion_type, stability_risk,
  action_summary} plus ``evidence_json.frames[].time_sec``
* video_native -> ``SegmentVideoObservation`` {analysis_json.summary,
  events_json[].{event_type,start_abs_sec,end_abs_sec,description,confidence}}

Rules enforced below:

* three channels produce three different claim sets — never mixed;
* each claim names its own result row and channel;
* a timed claim carries the source-absolute second so a reviewer can seek to it;
* a field the model did not output yields no claim (no invented assertions).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from app.services import evaluation_samples as samples

SESSIONS = "/api/projects/{p}/evaluation/sessions"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-p2"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _base(client, name="r1p2"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    src = _tmp_root() / f"{pid[:8]}.mp4"
    src.write_bytes(b"\x00" * 64)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=120.0, fps_num=25, fps_den=1,
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
        return pid, seg.id, a.id


def _add_static(pid, seg_id, asset_id, analysis):
    with session_scope() as s:
        row = SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=asset_id, segment_id=seg_id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h", analysis_json=analysis,
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        )
        s.add(row)
        s.flush()
        return row.id


def _add_motion(pid, seg_id, asset_id, *, analysis, frames):
    with session_scope() as s:
        row = SegmentMotionReview(
            id=_uid(), project_id=pid, asset_id=asset_id, segment_id=seg_id,
            method="multiframe", status="succeeded",
            model_provider="openai_compatible", model_id="GLM", prompt_version="1.0",
            start_sec=4.0, end_sec=11.0, frame_manifest_hash="h",
            motion_type=analysis.get("motion_type", ""),
            stability_risk=analysis.get("stability_risk", ""),
            action_summary=analysis.get("action_summary", ""),
            analysis_json=analysis,
            evidence_json={"method": "multiframe", "frames": frames},
            request_usage={}, is_current=True, error_code="", error_message="",
        )
        s.add(row)
        s.flush()
        return row.id


def _add_video(pid, seg_id, asset_id, *, summary, events):
    with session_scope() as s:
        row = SegmentVideoObservation(
            id=_uid(), project_id=pid, asset_id=asset_id, segment_id=seg_id,
            status="succeeded", method="video_native",
            model_provider="openai_compatible", model_id="GLM", prompt_version="1.0",
            schema_version="1.0", protocol="openai_compatible",
            source_signature=_uid(), clip_sha256="a" * 64, version_key="k",
            clip_start_sec=4.0, clip_end_sec=11.0, clip_duration_sec=7.0,
            clip_size_bytes=1000, segment_start_sec=4.0, segment_end_sec=11.0,
            analysis_json={"summary": summary}, events_json=events,
            request_usage={}, audio_analyzed=False, color_unverified=False,
            observed_temporal_quality="good", is_current=True,
            error_code="", error_message="",
        )
        s.add(row)
        s.flush()
        return row.id


def _items(client, pid):
    return client.get(f"/api/projects/{pid}/evaluation/eligible-samples?limit=10").json()


def _claim_keys(item) -> set[str]:
    keys = set()
    for group, value in (item.get("readings") or {}).items():
        if isinstance(value, list):
            for i in range(len(value)):
                keys.add(f"{group}:{i}")
        elif value:
            keys.add(f"{group}:0")
    return keys


# ---------------------------------------------------------------------------
# Three channels, three claim sets
# ---------------------------------------------------------------------------

def test_all_three_channels_produce_review_items(client):
    pid, seg_id, aid = _base(client)
    _add_static(pid, seg_id, aid, {"scene_tags": ["lake"], "subjects": ["boats"]})
    _add_motion(pid, seg_id, aid,
                analysis={"motion_type": "pan", "stability_risk": "low",
                          "action_summary": "camera pans left"},
                frames=[{"time_sec": 5.0, "frame_id": ""}])
    _add_video(pid, seg_id, aid, summary="a boat crosses the lake",
               events=[{"event_type": "motion", "start_abs_sec": 5.0,
                        "end_abs_sec": 7.0, "description": "boat enters",
                        "confidence": 0.8}])

    body = _items(client, pid)
    channels = sorted(i["channel"] for i in body["items"])
    assert channels == ["multiframe", "static", "video_native"]


def test_the_static_claims_are_the_documented_six_groups(client):
    pid, seg_id, aid = _base(client)
    _add_static(pid, seg_id, aid, {
        "scene_tags": ["lake", "sky"], "subjects": ["boats"],
        "visible_actions": ["floating"], "lighting": "daylight",
        "composition": "wide", "mood": ["calm"]})
    item = next(i for i in _items(client, pid)["items"] if i["channel"] == "static")
    assert _claim_keys(item) == {
        "scene_tags:0", "scene_tags:1", "subjects:0",
        "visible_actions:0", "lighting:0", "composition:0", "mood:0",
    }


def test_the_multiframe_claims_cover_the_stored_fields(client):
    pid, seg_id, aid = _base(client)
    _add_motion(pid, seg_id, aid,
                analysis={"motion_type": "pan", "stability_risk": "low",
                          "action_summary": "camera pans left past the pagoda"},
                frames=[{"time_sec": 5.0, "frame_id": ""},
                        {"time_sec": 8.0, "frame_id": ""}])
    item = next(i for i in _items(client, pid)["items"] if i["channel"] == "multiframe")
    keys = _claim_keys(item)
    assert "motion_type:0" in keys
    assert "stability_risk:0" in keys
    assert "action_summary:0" in keys
    # The stored frames become timed claims, in source-absolute seconds.
    assert item["readings"]["motion_type"] == ["pan"]
    assert item["readings"]["stability_risk"] == ["low"]
    assert item["readings"]["action_summary"] == ["camera pans left past the pagoda"]
    assert item["readings"]["evidence_times_sec"] == [5.0, 8.0]


def test_the_video_claims_cover_summary_and_events(client):
    pid, seg_id, aid = _base(client)
    _add_video(pid, seg_id, aid, summary="a boat crosses the lake",
               events=[
                   {"event_type": "motion", "start_abs_sec": 5.0, "end_abs_sec": 7.0,
                    "description": "boat enters frame", "confidence": 0.8},
                   {"event_type": "people", "start_abs_sec": 8.0, "end_abs_sec": 9.5,
                    "description": "two people appear", "confidence": 0.6},
               ])
    item = next(i for i in _items(client, pid)["items"] if i["channel"] == "video_native")
    readings = item["readings"]
    assert readings["summary"] == ["a boat crosses the lake"]
    assert len(readings["events"]) == 2
    # Each event keeps its own source-absolute window.
    assert item["event_windows"][0]["start_abs_sec"] == 5.0
    assert item["event_windows"][1]["end_abs_sec"] == 9.5


def test_channels_never_share_a_claim_key(client):
    """A verdict on a static claim must not be readable as a motion claim."""
    pid, seg_id, aid = _base(client)
    _add_static(pid, seg_id, aid, {"scene_tags": ["lake"], "subjects": ["boats"]})
    _add_motion(pid, seg_id, aid,
                analysis={"motion_type": "pan", "stability_risk": "low",
                          "action_summary": "camera pans"},
                frames=[{"time_sec": 5.0, "frame_id": ""}])
    items = _items(client, pid)["items"]
    static_item = next(i for i in items if i["channel"] == "static")
    motion_item = next(i for i in items if i["channel"] == "multiframe")
    assert static_item["result_id"] != motion_item["result_id"]
    # Each carries its own result id, so a stored verdict is unambiguous.
    assert static_item["channel"] != motion_item["channel"]


# ---------------------------------------------------------------------------
# No invented claims
# ---------------------------------------------------------------------------

def test_a_field_the_model_did_not_output_yields_no_claim(client):
    pid, seg_id, aid = _base(client)
    _add_motion(pid, seg_id, aid,
                analysis={"motion_type": "pan"},      # no summary, no risk
                frames=[])
    item = next(i for i in _items(client, pid)["items"] if i["channel"] == "multiframe")
    keys = _claim_keys(item)
    assert "motion_type:0" in keys
    assert "action_summary:0" not in keys
    assert "stability_risk:0" not in keys


def test_a_video_reading_without_events_yields_no_event_claims(client):
    pid, seg_id, aid = _base(client)
    _add_video(pid, seg_id, aid, summary="a quiet lake", events=[])
    item = next(i for i in _items(client, pid)["items"] if i["channel"] == "video_native")
    assert "events" not in item["readings"] or item["readings"]["events"] == []


# ---------------------------------------------------------------------------
# Saved and restored through the rating session
# ---------------------------------------------------------------------------

def test_a_multiframe_claim_can_be_saved_and_restored(client):
    pid, seg_id, aid = _base(client)
    _add_motion(pid, seg_id, aid,
                analysis={"motion_type": "pan", "stability_risk": "low",
                          "action_summary": "camera pans left"},
                frames=[{"time_sec": 5.0, "frame_id": ""}])
    created = client.post(SESSIONS.format(p=pid), json={"limit": 10}).json()
    sid = created["session_id"]
    item = next(i for i in created["items"] if i["channel"] == "multiframe")
    assert "action_summary:0" in _claim_keys(item)

    r = client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                     json={"action": 0,
                           "claim_verdicts": [
                               {"key": "action_summary:0", "verdict": "wrong"}]})
    assert r.status_code == 200, r.text

    resumed = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    row = next(i for i in resumed["items"] if i["item_id"] == item["item_id"])
    assert row["claim_verdicts"] == [
        {"key": "action_summary:0", "verdict": "wrong"}]


def test_a_video_event_claim_can_be_saved(client):
    pid, seg_id, aid = _base(client)
    _add_video(pid, seg_id, aid, summary="a boat crosses",
               events=[{"event_type": "motion", "start_abs_sec": 5.0,
                        "end_abs_sec": 7.0, "description": "boat enters",
                        "confidence": 0.8}])
    created = client.post(SESSIONS.format(p=pid), json={"limit": 10}).json()
    sid = created["session_id"]
    item = next(i for i in created["items"] if i["channel"] == "video_native")
    r = client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                     json={"claim_verdicts": [
                         {"key": "events:0", "verdict": "wrong"}]})
    assert r.status_code == 200, r.text
