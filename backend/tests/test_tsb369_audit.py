"""TSB-369 audit round — the gaps the §8 matrix names but the first pass missed.

Each test here was written because a *specific* requirement in the task book was
not actually enforced, not to inflate the count. Where a hole was found in code
already shipped, the test file records what the pre-fix behaviour was.

Two of these are genuine defects in the first delivery:

* ``AUDIO_CLAIM_WITHOUT_ANALYSIS`` — a shot whose video reading has
  ``audio_analyzed == False`` could still carry a claim about what the audio
  sounds like ("the beat drops at 1.2s"). That is exactly the "describe what was
  never observed" failure the round is meant to prevent, one channel over.
* ``LOCKED_SHOT_RETAINED`` — a shot locked in the base storyboard version could
  be re-timed by a draft without the draft saying so. Drafts must never imply
  they may move something the user locked.
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
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
    Storyboard,
    StoryboardVersion,
    UserAnnotation,
)
from app.services import director_candidates as dc
from app.services import director_plan_input as dpi
from app.services import director_plan_rules as rules
from app.services import director_plan_validation as val
from app.services.director_plan_schema import Claim, CoverageSummary, EvidenceRef, PlanDraft, PlanningBrief
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import validate_temporal_events


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- builders
def _project(s, name="audit") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project) -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name="c.mp4", file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
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


def _static(s, project, asset, seg, *, status="succeeded", is_current=True):
    r = SegmentSemanticResult(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status=status, model_provider="mock", model_id="m", prompt_version="p",
        schema_version="1.0", source_signature=_uid(), frame_manifest_hash="h",
        analysis_json={"scene_tags": ["nature"], "subjects": ["a lake"],
                       "visible_actions": [], "lighting": "daylight",
                       "composition": "wide", "mood": ["calm"]},
        evidence_json={"state": "observed", "frame_ids": ["f1"]},
        request_usage={}, is_current=is_current, error_code="", error_message="",
        created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _motion(s, project, asset, seg, *, status="succeeded", is_current=True):
    r = SegmentMotionReview(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        method="multiframe", status=status, model_provider="mock", model_id="m",
        prompt_version="p", start_sec=seg.start_sec, end_sec=seg.end_sec,
        frame_manifest_hash="h", input_signature="s", motion_type="pan",
        stability_risk="low", action_summary="camera pans", analysis_json={},
        evidence_json={}, request_usage={}, is_current=is_current, human_verdict="",
        human_note="", human_at=None, error_code="", error_message="",
        created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _video(s, project, asset, seg, *, status="succeeded", is_current=True,
           clip_start=None, clip_end=None, audio_analyzed=False):
    cs = seg.start_sec if clip_start is None else clip_start
    ce = seg.end_sec if clip_end is None else clip_end
    clip = ClipArtifact(
        path=Path("/dev/null"), segment_id=seg.id, asset_id=asset.id,
        source_clip_start_sec=cs, source_clip_end_sec=ce, duration_sec=ce - cs,
        width=640, height=360, rotation=0, vfr=False, has_audio=audio_analyzed,
        size_bytes=1, sha256="c" * 64, container="mp4", color_unverified=False,
    )
    resp = {"clip_id": clip.clip_id, "segment_id": seg.id, "summary": "s",
            "observed_temporal_quality": "limited", "audio_analyzed": audio_analyzed,
            "events": [{"event_type": "subject_action", "start_rel_sec": 0.5,
                        "end_rel_sec": 1.5, "description": "a person walks past",
                        "confidence": 0.8}]}
    v = validate_temporal_events(resp, clip=clip, segment_start_sec=cs, segment_end_sec=ce)
    row = SegmentVideoObservation(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status=status, method="video_native", model_provider="mock", model_id="m",
        prompt_version="p", schema_version="1.0", protocol="native",
        source_signature="v", clip_sha256="c" * 64, version_key=_uid(),
        clip_start_sec=cs, clip_end_sec=ce, clip_duration_sec=ce - cs,
        clip_size_bytes=1, segment_start_sec=cs, segment_end_sec=ce,
        analysis_json={"summary": "s"}, events_json=[e.to_dict() for e in v.events],
        request_usage={}, audio_analyzed=audio_analyzed, color_unverified=False,
        observed_temporal_quality="limited", is_current=is_current,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(row)
    s.flush()
    return row


def _input(project_id, **kw):
    return dpi.build_planning_input(project_id, **kw)


def _plan(pi, variant) -> PlanDraft:
    return PlanDraft(
        project_id=pi.project_id, planning_mode="model",
        generation_status="provisional", source_input_digest=pi.source_input_digest,
        coverage=CoverageSummary(), variants=[variant],
    )


def _variant(pi, index=0, **overrides):
    import test_tsb369_plan as base

    return base._base_variant(pi, entry_index=index, **overrides)


# ===========================================================================
# 369-B 叙事：无音频仍声称听到节拍  (RED before the fix)
# ===========================================================================

def test_a_claim_about_sound_is_rejected_when_audio_was_never_analysed():
    """Pre-fix: the validator returned [] and this plan would have been shown.

    A shot's video reading carries ``audio_analyzed=False``. Claiming the music
    has a beat there is describing something never observed — the same failure as
    claiming a lake that was never seen, in the audio channel.
    """
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 10.0, 16.0)
        _video(s, p, a, g, audio_analyzed=False)
        pid = p.id

    pi = _input(pid)
    entry = pi.entries[0]
    assert entry.video_native.audio_analyzed is False

    ref = EvidenceRef(channel="video_native",
                      result_id=entry.video_native.result_id or "",
                      segment_id=entry.segment_id)
    variant = _variant(pi, claims=[
        Claim(text="音乐的节拍在 1.2 秒处重击，适合卡点", kind="observed_signal",
              evidence_refs=[ref]),
    ])
    codes = [p.code for p in val.validate_plan(_plan(pi, variant), pi)]
    assert "AUDIO_CLAIM_WITHOUT_ANALYSIS" in codes


def test_an_audio_channel_cannot_even_be_expressed_in_the_schema():
    """There is no audio evidence channel, so a claim cannot name one.

    Guarding this at the schema is stronger than at the validator: a planner
    physically cannot cite audio, because ``EvidenceRef.channel`` is a closed
    Literal of the three real channels.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        EvidenceRef(channel="audio", result_id="x", segment_id="y")


def test_a_sound_claim_is_rejected_even_when_wording_matches_a_visual_channel():
    """The refusal must be semantic, not merely a naming technicality."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _video(s, p, a, g, audio_analyzed=False)
        pid = p.id

    pi = _input(pid)
    e = pi.entries[0]
    variant = _variant(pi, claims=[
        Claim(text="可以听到海浪声与音乐节拍", kind="observed_signal",
              evidence_refs=[EvidenceRef(channel="video_native",
                                         result_id=e.video_native.result_id or "",
                                         segment_id=e.segment_id)]),
    ])
    codes = [p.code for p in val.validate_plan(_plan(pi, variant), pi)]
    assert "AUDIO_CLAIM_WITHOUT_ANALYSIS" in codes


def test_a_silent_shot_is_marked_audio_unanalysed_for_review():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _video(s, p, a, g, audio_analyzed=False)
        pid = p.id

    plan = rules.plan_offline(_input(pid, max_candidates=10),
                              PlanningBrief(target_duration_sec=5, variant_count=1))
    shot = plan.variants[0].shots[0]
    assert any("音频" in r for r in shot.needs_review)


def test_the_offline_planner_never_emits_an_audio_claim():
    """Rule output must not assert sound even when audio exists."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _video(s, p, a, g, audio_analyzed=True)
        pid = p.id

    plan = rules.plan_offline(_input(pid, max_candidates=10),
                              PlanningBrief(target_duration_sec=5, variant_count=1))
    for shot in plan.variants[0].shots:
        assert shot.audio_hint == ""
        for c in shot.claims:
            assert "节拍" not in c.text and "音乐" not in c.text


# ===========================================================================
# 369-A 时效：stale / failed 的独立负例
# ===========================================================================

@pytest.mark.parametrize("status,is_current", [
    ("failed", True),        # current row, analysis failed
    ("succeeded", False),    # no current row at all (superseded)
])
def test_a_failed_or_superseded_static_reading_is_not_usable_evidence(status, is_current):
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        row = _static(s, p, a, g, status=status, is_current=is_current)
        pid, rid = p.id, row.id

    pi = _input(pid)
    entry = pi.entries[0]
    assert entry.static.state in (dc.STATE_FAILED, dc.STATE_STALE)
    assert entry.static.result_id != rid or entry.static.state != dc.STATE_CURRENT

    # Citing it must be refused.
    variant = _variant(pi, claims=[
        Claim(text="看到了湖面", kind="observed_signal",
              evidence_refs=[EvidenceRef(channel="static", result_id=rid,
                                         segment_id=entry.segment_id)]),
    ])
    codes = [p.code for p in val.validate_plan(_plan(pi, variant), pi)]
    assert "UNSUPPORTED_VISUAL_CLAIM" in codes


def test_a_failed_video_reading_is_not_analyzed():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _video(s, p, a, g, status="failed")
        pid = p.id

    e = _input(pid).entries[0]
    assert e.video_native.state == dc.STATE_FAILED
    assert e.video_native.events == []
    assert e.has_usable_reading is False


def test_a_window_mismatch_makes_the_video_reading_stale():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 10.0, 16.0)
        _video(s, p, a, g, clip_start=0.0, clip_end=6.0)   # analysed a different window
        pid = p.id

    e = _input(pid).entries[0]
    assert e.video_native.state == dc.STATE_STALE
    assert any("window" in lim for lim in e.video_native.limitations)


def test_motion_failure_is_visible_and_not_reported_as_analysed():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _motion(s, p, a, g, status="failed")
        pid = p.id

    e = _input(pid).entries[0]
    assert e.multiframe.state == dc.STATE_FAILED
    assert e.multiframe.action_summary == ""


# ===========================================================================
# 369-A 性能：4 / 40 / 500 / 1000 四档，语句数不随规模增长
# ===========================================================================

def _seed(count: int) -> str:
    pid = _uid()
    with session_scope() as s:
        p = Project(id=pid, name=f"n{count}", source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(
            id=_uid(), project_id=pid, source_path=f"/x/{count}.mp4", display_name="c.mp4",
            file_size_bytes=1, mtime_ns=1, quick_fingerprint="f", duration_sec=float(count * 10),
            fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360, rotation=0,
            codec="h264", pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        for i in range(count):
            s.add(Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=float(i * 10), detected_end_sec=float(i * 10 + 8),
                start_sec=float(i * 10), end_sec=float(i * 10 + 8), is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0}, content_tags={},
                story_role="", user_rating="pending", status="candidate", frame_refs=[],
                proxy_ref="", parent_segment_id="", algorithm_version="1", created_at=None,
            ))
    return pid


def _statements(project_id: str, max_candidates: int) -> int:
    from sqlalchemy import event

    n = 0
    with session_scope() as s:
        engine = s.get_bind()

        def _listen(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            nonlocal n
            n += 1

        event.listen(engine, "before_cursor_execute", _listen)
        try:
            dpi.build_planning_input(project_id, max_candidates=max_candidates)
        finally:
            event.remove(engine, "before_cursor_execute", _listen)
    return n


@pytest.mark.parametrize("count", [4, 40, 500, 1000])
def test_statement_count_is_bounded_at_every_scale(count):
    pid = _seed(count)
    n = _statements(pid, 40)
    assert n <= 20, f"{count} shots needed {n} statements (N+1 suspected)"


def test_statement_count_does_not_trend_up_with_scale():
    small = _statements(_seed(4), 40)
    big = _statements(_seed(1000), 40)
    assert big - small <= 3, f"4 -> 1000 shots moved from {small} to {big} statements"


# ===========================================================================
# 369-B 选择：锁定镜头
# ===========================================================================

def _board_with_locked_shot(pid: str, segment_id: str) -> str:
    """A base version that locks one shot, using the production schema shape."""
    with session_scope() as s:
        board = Storyboard(id=_uid(), project_id=pid, title="", current_version_id=None,
                           created_at=None, updated_at=None)
        s.add(board)
        s.flush()
        v = StoryboardVersion(
            id=_uid(), board_id=board.id, version_no=1, parent_version_id=None,
            schema_version="1.0", brief={},
            sections=[{"id": "sec-1", "name": "主体", "target_duration_sec": 60}],
            shots=[{
                "asset_id": "", "segment_id": segment_id,
                "source_in_sec": 0.0, "source_out_sec": 6.0,
                "target_duration_sec": 6.0, "section_id": "sec-1",
                "story_role": "", "reason": "", "locked": True,
                "source_signature": "sig",
            }],
            constraints={}, validation_summary={}, created_at=None, actor="user",
            reason="initial", job_id=None, restore_of_version_id=None,
            idempotency_key=None,
        )
        s.add(v)
        s.flush()
        board.current_version_id = v.id
        return v.id


def test_a_locked_shot_is_flagged_so_the_draft_cannot_silently_retime_it():
    """A draft may still *show* a locked shot, but must say it is locked.

    Nothing in this round writes to the storyboard, but a preview that silently
    re-times a locked shot would mislead the user about what applying it means.
    """
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _static(s, p, a, g)
        pid, sid = p.id, g.id
    _board_with_locked_shot(pid, sid)

    out = dpi.build_planning_input(pid, max_candidates=10)
    assert out.base_storyboard_version_id is not None
    entry = out.entries[0]
    assert entry.locked_in_base is True
    assert any("锁定" in r or "locked" in r for r in entry.limitations)


def test_an_unlocked_shot_is_not_flagged():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _static(s, p, a, g)
        pid = p.id

    e = dpi.build_planning_input(pid, max_candidates=10).entries[0]
    assert e.locked_in_base is False


def test_reading_the_input_never_writes_to_a_locked_storyboard():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _static(s, p, a, g)
        pid, sid = p.id, g.id
    vid = _board_with_locked_shot(pid, sid)

    before = _snapshot(pid)
    dpi.build_planning_input(pid, max_candidates=10)
    assert _snapshot(pid) == before

    with session_scope() as s:
        v = s.get(StoryboardVersion, vid)
        assert v.shots[0]["locked"] is True
        assert v.shots[0]["source_in_sec"] == 0.0


def _snapshot(pid: str) -> dict:
    with session_scope() as s:
        boards = [b.id for b in s.scalars(select(Storyboard).where(Storyboard.project_id == pid))]
        return {
            "storyboards": len(boards),
            "versions": int(s.scalar(select(func.count()).select_from(StoryboardVersion).where(
                StoryboardVersion.board_id.in_(boards))) or 0) if boards else 0,
            "jobs": int(s.scalar(select(func.count()).select_from(Job).where(
                Job.project_id == pid)) or 0),
            "ops": int(s.scalar(select(func.count()).select_from(PlanOperation).where(
                PlanOperation.project_id == pid)) or 0),
            "edit": int(s.scalar(select(func.count()).select_from(EditPlanShot).where(
                EditPlanShot.project_id == pid)) or 0),
            "ann": int(s.scalar(select(func.count()).select_from(UserAnnotation).join(
                Segment, Segment.id == UserAnnotation.segment_id).where(
                Segment.project_id == pid)) or 0),
        }


# ===========================================================================
# 369-C 生命周期：并发 / 切换项目
# ===========================================================================

def test_two_previews_for_the_same_project_return_identical_bytes(client):
    pid = client.post("/api/projects", json={"name": "life"}).json()["id"]
    with session_scope() as s:
        p = s.get(Project, pid)
        a = _asset(s, p)
        for i in range(4):
            g = _segment(s, p, a, i * 10.0, i * 10.0 + 5.0)
            if i < 2:
                _static(s, p, a, g)

    r1 = client.post(f"/api/projects/{pid}/director/plans/preview",
                     json={"target_duration_sec": 15})
    r2 = client.post(f"/api/projects/{pid}/director/plans/preview",
                     json={"target_duration_sec": 15})
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content


def test_switching_projects_mid_session_keeps_results_separate(client):
    pa = client.post("/api/projects", json={"name": "sw-a"}).json()["id"]
    pb = client.post("/api/projects", json={"name": "sw-b"}).json()["id"]
    for pid, tag in ((pa, "alpha"), (pb, "beta")):
        with session_scope() as s:
            p = s.get(Project, pid)
            a = _asset(s, p)
            g = _segment(s, p, a, 0.0, 5.0)
            _static(s, p, a, g)

    for pid in (pa, pb, pa, pb):
        r = client.post(f"/api/projects/{pid}/director/plans/preview",
                        json={"target_duration_sec": 10})
        assert r.status_code == 200
        body = r.json()
        assert body["project_id"] == pid
        for v in body["variants"]:
            for shot in v["shots"]:
                assert shot["segment_id"] in _seg_ids(pid)


def _seg_ids(pid: str) -> set[str]:
    with session_scope() as s:
        return {g.id for g in s.scalars(select(Segment).where(Segment.project_id == pid))}
