"""TSB-370-D — the real model planner adapter, tested without any network.

Two things must hold before a model may plan at all:

1. **What leaves is a whitelist.** No path, no user note, no image, no raw payload.
2. **What comes back changes nothing authoritative.** ``project_id``, the digest,
   coverage and lock state are server-filled; the model's opinion of them is
   discarded. And every existing guard still bites.

The adapter is exercised with payloads shaped like a real reply, so a failure here
is a contract failure, not a flaky network test.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentSemanticResult,
    SegmentVideoObservation,
    UserAnnotation,
)
from app.services import director_candidates as dc
from app.services import director_plan_input as dpi
from app.services import director_plan_validation as val
from app.services.director_plan_model import (
    ModelPlanError,
    build_evidence_text,
    extract_json,
    parse_model_plan,
)
from app.services.director_plan_schema import PlanningBrief
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import validate_temporal_events


def _uid() -> str:
    return str(uuid.uuid4())


def _project(s) -> Project:
    p = Project(id=_uid(), name="t370d", source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, p) -> Asset:
    a = Asset(
        id=_uid(), project_id=p.id, source_path=f"/nonexistent/{_uid()}.mp4",
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
    return a


def _segment(s, p, a, start, end, *, note="", rating="pending") -> Segment:
    g = Segment(
        id=_uid(), asset_id=a.id, project_id=p.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
        user_rating=rating, status="candidate", frame_refs=[], proxy_ref="",
        parent_segment_id="", algorithm_version="1", created_at=None,
    )
    s.add(g)
    s.flush()
    if note:
        s.add(UserAnnotation(id=_uid(), segment_id=g.id, rating=rating, note=note,
                             in_sec=None, out_sec=None, created_at=None,
                             updated_at=None))
        s.flush()
    return g


def _static(s, p, a, g, tags=None, state="observed"):
    r = SegmentSemanticResult(
        id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
        status="succeeded", model_provider="openai_compatible", model_id="GLM",
        prompt_version="p", schema_version="1.0", source_signature=_uid(),
        frame_manifest_hash="h",
        analysis_json={"scene_tags": tags or ["nature"], "subjects": ["a lake"],
                       "visible_actions": ["ripples"], "lighting": "daylight",
                       "composition": "wide", "mood": ["calm"]},
        evidence_json={"state": state, "frame_ids": ["f1"]},
        request_usage={}, is_current=True, error_code="", error_message="",
        created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _video(s, p, a, g):
    clip = ClipArtifact(
        path=Path("/dev/null"), segment_id=g.id, asset_id=a.id,
        source_clip_start_sec=g.start_sec, source_clip_end_sec=g.end_sec,
        duration_sec=g.end_sec - g.start_sec, width=640, height=360, rotation=0,
        vfr=False, has_audio=False, size_bytes=1, sha256="c" * 64,
        container="mp4", color_unverified=False,
    )
    resp = {"clip_id": clip.clip_id, "segment_id": g.id, "summary": "a boat crosses",
            "observed_temporal_quality": "limited", "audio_analyzed": False,
            "events": [{"event_type": "subject_action", "start_rel_sec": 0.5,
                        "end_rel_sec": 1.5, "description": "a person walks past",
                        "confidence": 0.8}]}
    v = validate_temporal_events(resp, clip=clip, segment_start_sec=g.start_sec,
                                 segment_end_sec=g.end_sec)
    row = SegmentVideoObservation(
        id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
        status="succeeded", method="video_native", model_provider="m", model_id="m",
        prompt_version="p", schema_version="1.0", protocol="native",
        source_signature="v", clip_sha256="c" * 64, version_key=_uid(),
        clip_start_sec=g.start_sec, clip_end_sec=g.end_sec,
        clip_duration_sec=g.end_sec - g.start_sec, clip_size_bytes=1,
        segment_start_sec=g.start_sec, segment_end_sec=g.end_sec,
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
def two_shot_input():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g1 = _segment(s, p, a, 0.0, 6.0, note="我在这里遇到了老朋友小王，电话13800000000")
        g2 = _segment(s, p, a, 10.0, 18.0)
        r1 = _static(s, p, a, g1, tags=["lake", "boats"])
        _static(s, p, a, g2, tags=["temple"])
        _video(s, p, a, g2)
        pid = p.id
        ids = (g1.id, g2.id, r1.id)
    return dpi.build_planning_input(pid, max_candidates=10), ids


# ---------------------------------------------------------------- what leaves
def test_paths_are_never_in_the_prompt(two_shot_input):
    pi, _ = two_shot_input
    text, _summary = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert "/nonexistent/" not in text
    assert "source_path" not in text


def test_user_notes_and_personal_data_never_leave(two_shot_input):
    """The note contains a name and a phone number; neither may be projected."""
    pi, _ = two_shot_input
    text, summary = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert "老朋友" not in text
    assert "13800000000" not in text
    assert "小王" not in text
    assert summary["sends_user_notes"] is False


def test_no_images_are_sent(two_shot_input):
    pi, _ = two_shot_input
    _text, summary = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert summary["sends_images"] is False
    assert summary["sends_paths"] is False


def test_the_prompt_carries_evidence_ids_and_absolute_times(two_shot_input):
    pi, _ = two_shot_input
    text, summary = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert "static_evidence_id=" in text
    assert "video_evidence_id=" in text
    # The 10-18s shot's event must read as absolute source time 10.5, not 0.5.
    assert "10.50-11.50s" in text
    assert summary["shots_listed"] == 2


def test_the_theme_is_labelled_as_a_constraint(two_shot_input):
    pi, _ = two_shot_input
    text, _ = build_evidence_text(pi, PlanningBrief(theme="雪山与极光"))
    assert "creative constraint" in text.lower()


def test_a_locked_shot_is_flagged_in_the_prompt(two_shot_input):
    pi, _ = two_shot_input
    # Simulate the lock on the entry (input assembly sets it from the version).
    from dataclasses import replace

    locked_entry = replace(pi.entries[0], locked_in_base=True)
    pi.entries[0] = locked_entry
    text, _ = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert "LOCKED" in text


def test_audio_is_declared_unanalysed_in_the_prompt(two_shot_input):
    pi, _ = two_shot_input
    text, _ = build_evidence_text(pi, PlanningBrief(theme="t"))
    assert "AUDIO: not analysed" in text


# ---------------------------------------------------------------- parsing
def test_extract_json_accepts_a_fenced_reply():
    payload = {"variants": []}
    assert extract_json("```json\n" + json.dumps(payload) + "\n```") == payload


def test_extract_json_rejects_prose():
    with pytest.raises(ModelPlanError) as exc:
        extract_json("I cannot help with that.")
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def _reply(pi, ids, *, shots=None, title="方案A", angle="chronological"):
    g1, g2, r1 = ids
    return {
        "variants": [{
            "title": title,
            "narrative_angle": angle,
            "logline": "从湖边走到庙前",
            "sections": [
                {"section_id": "s-open", "kind": "opening", "title": "开场"},
                {"section_id": "s-body", "kind": "body", "title": "展开"},
            ],
            "shots": shots if shots is not None else [
                {"segment_id": g1, "source_in_sec": 0.0, "source_out_sec": 3.0,
                 "section_id": "s-open", "narrative_role": "opening",
                 "selection_reason": "有静帧观察：湖与船",
                 "claims": [{"text": "静帧观察：场景标签 lake,boats",
                             "kind": "observed_signal",
                             "evidence_refs": [{"channel": "static", "result_id": r1}]}],
                 "evidence_refs": [{"channel": "static", "result_id": r1}]},
                {"segment_id": g2, "source_in_sec": 10.0, "source_out_sec": 14.0,
                 "section_id": "s-body", "narrative_role": "closing",
                 "selection_reason": "有原生视频事件",
                 "claims": [], "evidence_refs": []},
            ],
        }],
    }


def test_a_faithful_model_reply_parses_and_validates(two_shot_input):
    pi, ids = two_shot_input
    plan = parse_model_plan(_reply(pi, ids), pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="openai_compatible")
    assert plan.planning_mode == "model"
    assert len(plan.variants) == 1
    assert val.validate_plan(plan, pi) == []


def test_server_fields_cannot_be_overridden_by_the_model(two_shot_input):
    pi, ids = two_shot_input
    payload = _reply(pi, ids)
    # A malicious or confused model tries to take over the authoritative fields.
    payload.update({"project_id": "evil", "input_digest": "deadbeef",
                    "planning_mode": "offline_rules", "coverage": {"library_segments": 1}})
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    assert plan.project_id == pi.project_id
    assert plan.source_input_digest == pi.source_input_digest
    assert plan.planning_mode == "model"
    assert plan.coverage.library_segments == (pi.coverage or {}).get(
        "library_segments", 0)


def test_an_invented_shot_from_the_model_is_rejected(two_shot_input):
    pi, ids = two_shot_input
    ghost = str(uuid.uuid4())
    payload = _reply(pi, ids, shots=[
        {"segment_id": ghost, "source_in_sec": 0.0, "source_out_sec": 2.0,
         "section_id": "s-open", "narrative_role": "opening",
         "selection_reason": "x", "claims": [], "evidence_refs": []},
    ])
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "UNKNOWN_SEGMENT" in codes


def test_a_sound_claim_from_the_model_is_rejected(two_shot_input):
    pi, ids = two_shot_input
    g1, _g2, r1 = ids
    payload = _reply(pi, ids, shots=[
        {"segment_id": g1, "source_in_sec": 0.0, "source_out_sec": 3.0,
         "section_id": "s-open", "narrative_role": "opening",
         "selection_reason": "x",
         "claims": [{"text": "音乐节拍在 1.2 秒处重击", "kind": "observed_signal",
                     "evidence_refs": [{"channel": "static", "result_id": r1}]}],
         "evidence_refs": []},
    ])
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "AUDIO_CLAIM_WITHOUT_ANALYSIS" in codes


def test_an_uncertain_upgrade_from_the_model_is_rejected():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        r = _static(s, p, a, g, state="uncertain")
        pid, gid, rid = p.id, g.id, r.id
    pi = dpi.build_planning_input(pid, max_candidates=10)
    payload = {"variants": [{
        "title": "t", "narrative_angle": "chronological", "logline": "",
        "sections": [{"section_id": "s1", "kind": "body", "title": "主体"}],
        "shots": [{"segment_id": gid, "source_in_sec": 0.0, "source_out_sec": 3.0,
                   "section_id": "s1", "narrative_role": "development",
                   "selection_reason": "x",
                   "claims": [{"text": "明确看到了湖面", "kind": "observed_signal",
                               "evidence_refs": [{"channel": "static",
                                                  "result_id": rid}]}],
                   "evidence_refs": [{"channel": "static", "result_id": rid}]}],
    }]}
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "SEMANTIC_UPGRADE" in codes


def test_two_identical_model_variants_are_rejected(two_shot_input):
    pi, ids = two_shot_input
    one = _reply(pi, ids)
    payload = {"variants": [one["variants"][0],
                            dict(one["variants"][0], title="另一个名字")]}
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "VARIANTS_NOT_DISTINCT" in codes


def test_out_of_window_times_are_clamped_to_the_real_shot(two_shot_input):
    pi, ids = two_shot_input
    g1, _g2, r1 = ids
    payload = _reply(pi, ids, shots=[
        {"segment_id": g1, "source_in_sec": -50.0, "source_out_sec": 999.0,
         "section_id": "s-open", "narrative_role": "opening",
         "selection_reason": "x", "claims": [], "evidence_refs": []},
    ])
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    shot = plan.variants[0].shots[0]
    assert shot.source_in_sec >= 0.0
    assert shot.source_out_sec <= 6.0
    assert val.validate_plan(plan, pi) == []


def test_a_reply_with_no_variants_is_a_reported_failure(two_shot_input):
    pi, _ = two_shot_input
    with pytest.raises(ModelPlanError) as exc:
        parse_model_plan({"variants": []}, pi, PlanningBrief(), model_id="m",
                         provider_name="p")
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_evidence_refs_for_ids_the_shot_lacks_are_dropped(two_shot_input):
    """A ref the shot does not own is removed, so the claim becomes unsupported."""
    pi, ids = two_shot_input
    g1, g2, r1 = ids
    payload = _reply(pi, ids, shots=[
        {"segment_id": g2, "source_in_sec": 10.0, "source_out_sec": 12.0,
         "section_id": "s-open", "narrative_role": "opening",
         "selection_reason": "x",
         "claims": [{"text": "看到了湖与船", "kind": "observed_signal",
                     "evidence_refs": [{"channel": "static", "result_id": r1}]}],
         "evidence_refs": []},
    ])
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    shot = plan.variants[0].shots[0]
    assert shot.claims[0].evidence_refs == []
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "UNSUPPORTED_VISUAL_CLAIM" in codes


def test_an_unknown_channel_is_coerced_not_crashed(two_shot_input):
    pi, ids = two_shot_input
    g1, _g2, r1 = ids
    payload = _reply(pi, ids, shots=[
        {"segment_id": g1, "source_in_sec": 0.0, "source_out_sec": 3.0,
         "section_id": "s-open", "narrative_role": "opening",
         "selection_reason": "x", "claims": [],
         "evidence_refs": [{"channel": "audio", "result_id": r1}]},
    ])
    plan = parse_model_plan(payload, pi, PlanningBrief(target_duration_sec=10),
                            model_id="GLM", provider_name="p")
    assert all(r.channel in ("static", "multiframe", "video_native")
               for r in plan.variants[0].shots[0].evidence_refs)
