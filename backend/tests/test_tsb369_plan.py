"""TSB-369-B — plan generation and the validator's adversarial cases.

Two things are being proved here:

1. the offline planner produces a plan that is *structurally* sound and honest
   about what it does not know;
2. the validator rejects every way a planner (offline, mock, or future model)
   could produce something false — inventing shots, upgrading a hedged reading
   into certainty, claiming footage it never saw, or altering the timeline.

The negative cases use :class:`MockPlannerProvider`, which is how a hostile or
merely wrong model response is simulated without any network.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import director_candidates as dc
from app.services import director_plan_input as dpi
from app.services import director_plan_rules as rules
from app.services import director_plan_validation as val
from app.services.director_plan_providers import (
    ExternalPlannerProvider,
    MockPlannerProvider,
    ModelCallNotAuthorized,
    OfflineRulesPlanner,
    provider_for,
)
from app.services.director_plan_schema import (
    Claim,
    CoverageSummary,
    EvidenceRef,
    PlanDraft,
    PlanningBrief,
    SectionDraft,
    ShotDraft,
    VariantDraft,
)
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import validate_temporal_events


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _project(s, name="tsb369b") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project, name="clip.mp4") -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name=name, file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
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


def _segment(s, project, asset, start, end, *, rating="pending", role="", status="candidate",
             note=""):
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
        from app.models import UserAnnotation

        s.add(UserAnnotation(id=_uid(), segment_id=g.id, rating=rating, note=note,
                             in_sec=None, out_sec=None, created_at=None, updated_at=None))
        s.flush()
    return g


def _static(s, project, asset, seg, *, tags=None, state="observed"):
    r = SegmentSemanticResult(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", model_provider="mock", model_id="m", prompt_version="p",
        schema_version="1.0", source_signature=_uid(), frame_manifest_hash="h",
        analysis_json={"scene_tags": tags or ["nature"], "subjects": ["a lake"],
                       "visible_actions": ["ripples"], "lighting": "daylight",
                       "composition": "wide", "mood": ["calm"]},
        evidence_json={"state": state, "frame_ids": ["f1", "f2"]},
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
    resp = {
        "clip_id": clip.clip_id, "segment_id": seg.id, "summary": "a boat crosses",
        "observed_temporal_quality": "limited", "audio_analyzed": False,
        "events": [{"event_type": "subject_action", "start_rel_sec": 0.5,
                    "end_rel_sec": 1.5, "description": "a person walks past",
                    "confidence": 0.8}],
    }
    validated = validate_temporal_events(
        resp, clip=clip, segment_start_sec=seg.start_sec, segment_end_sec=seg.end_sec)
    from app.models import SegmentVideoObservation

    row = SegmentVideoObservation(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", method="video_native", model_provider="mock", model_id="m",
        prompt_version="p", schema_version="1.0", protocol="native",
        source_signature="v", clip_sha256="c" * 64, version_key=_uid(),
        clip_start_sec=seg.start_sec, clip_end_sec=seg.end_sec,
        clip_duration_sec=seg.end_sec - seg.start_sec, clip_size_bytes=1,
        segment_start_sec=seg.start_sec, segment_end_sec=seg.end_sec,
        analysis_json={"summary": "a boat crosses"},
        events_json=[e.to_dict() for e in validated.events],
        request_usage={}, audio_analyzed=False, color_unverified=False,
        observed_temporal_quality="limited", is_current=True,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(row)
    s.flush()
    return row


def _input(project_id: str, *, max_candidates=40) -> dpi.PlanningInput:
    return dpi.build_planning_input(project_id, max_candidates=max_candidates)


# ---------------------------------------------------------------------------
# Offline planner: structural correctness and honesty
# ---------------------------------------------------------------------------

def test_offline_plan_is_structurally_valid():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        for i in range(5):
            g = _segment(s, p, a, i * 10.0, i * 10.0 + 6.0)
            if i < 2:
                _static(s, p, a, g)
        pid = p.id

    pi = _input(pid)
    plan = rules.plan_offline(pi, PlanningBrief(target_duration_sec=30, variant_count=2))
    assert val.validate_plan(plan, pi) == []
    assert plan.planning_mode == "offline_rules"
    assert plan.variants


def test_timeline_is_contiguous_and_sums_to_total():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        for i in range(4):
            _segment(s, p, a, i * 10.0, i * 10.0 + 5.0)
        pid = p.id

    plan = rules.plan_offline(_input(pid), PlanningBrief(target_duration_sec=15))
    v = plan.variants[0]
    assert v.shots[0].timeline_start_sec == 0.0
    running = 0.0
    for shot in v.shots:
        assert shot.timeline_start_sec == pytest.approx(running)
        running += shot.planned_duration_sec
    assert v.total_duration_sec == pytest.approx(running)
    assert v.duration_gap_sec == pytest.approx(v.target_duration_sec - running)


def test_offline_plan_is_deterministic():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        for i in range(5):
            _segment(s, p, a, i * 10.0, i * 10.0 + 5.0)
        pid = p.id

    a1 = rules.plan_offline(_input(pid), PlanningBrief(variant_count=3)).model_dump()
    a2 = rules.plan_offline(_input(pid), PlanningBrief(variant_count=3)).model_dump()
    assert a1 == a2


def test_offline_plan_never_selects_an_excluded_or_review_shot():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        good = _segment(s, p, a, 0.0, 6.0)
        _segment(s, p, a, 10.0, 16.0, rating="exclude")
        _segment(s, p, a, 20.0, 26.0, status="needs_review")
        pid = p.id

    pi = _input(pid)
    plan = rules.plan_offline(pi, PlanningBrief(target_duration_sec=120))
    for v in plan.variants:
        for shot in v.shots:
            assert shot.segment_id == good.id


def test_a_favourite_is_placed_first_under_highlights_first():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        for i in range(4):
            _segment(s, p, a, i * 10.0, i * 10.0 + 5.0)
        fav = _segment(s, p, a, 50.0, 56.0, rating="favorite")
        pid = p.id

    plan = rules.plan_offline(_input(pid), PlanningBrief(
        target_duration_sec=20, style="highlights_first", variant_count=1))
    assert plan.variants[0].shots[0].segment_id == fav.id


def test_insufficient_evidence_yields_no_variants_rather_than_fabrication():
    with session_scope() as s:
        p = _project(s)
        pid = p.id
    plan = rules.plan_offline(_input(pid), PlanningBrief())
    assert plan.generation_status == "insufficient_evidence"
    assert plan.variants == []


def test_a_shot_without_any_reading_gets_no_factual_claim():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 6.0)
        pid = p.id

    plan = rules.plan_offline(_input(pid), PlanningBrief(target_duration_sec=10, variant_count=1))
    shots = plan.variants[0].shots
    assert shots
    for shot in shots:
        assert shot.claims == [], "no reading means no observed claim"
        assert shot.evidence_refs == []
        assert any("没有任何模型读数" in r for r in shot.needs_review)


def test_the_theme_never_becomes_a_footage_claim():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 6.0)
        pid = p.id

    brief = PlanningBrief(theme="雪山与极光", target_duration_sec=10, variant_count=1)
    plan = rules.plan_offline(_input(pid), brief)
    blob = str(plan.model_dump())
    assert "雪山" not in blob and "极光" not in blob


def test_variants_differ_in_more_than_their_titles():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        for i in range(6):
            _segment(s, p, a, i * 10.0, i * 10.0 + 5.0)
        pid = p.id

    plan = rules.plan_offline(_input(pid), PlanningBrief(
        target_duration_sec=15, variant_count=3))
    if len(plan.variants) >= 2:
        seqs = [tuple(s.segment_id for s in v.shots) for v in plan.variants]
        assert len(set(seqs)) == len(seqs), "variant sequences must be genuinely distinct"


# ---------------------------------------------------------------------------
# Validator: adversarial mock plans
# ---------------------------------------------------------------------------

def _base_variant(pi: dpi.PlanningInput, *, entry_index=0, **overrides) -> VariantDraft:
    entry = pi.entries[entry_index]
    duration = entry.source_out_sec - entry.source_in_sec
    shot = ShotDraft(
        shot_id="s1", segment_id=entry.segment_id, asset_id=entry.asset_id,
        section_id="sec-opening", source_in_sec=entry.source_in_sec,
        source_out_sec=entry.source_out_sec, planned_duration_sec=duration,
        timeline_start_sec=0.0, narrative_role="opening",
        selection_reason="test",
    )
    shot = shot.model_copy(update=overrides) if overrides else shot
    return VariantDraft(
        variant_id="vx", title="t", narrative_angle="chronological", status="provisional",
        sections=[SectionDraft(section_id="sec-opening", kind="opening", title="开场")],
        shots=[shot], total_duration_sec=shot.planned_duration_sec,
        target_duration_sec=shot.planned_duration_sec, duration_gap_sec=0.0,
    )


def _plan(pi: dpi.PlanningInput, variant: VariantDraft) -> PlanDraft:
    return PlanDraft(
        project_id=pi.project_id, planning_mode="model",
        generation_status="provisional", source_input_digest=pi.source_input_digest,
        coverage=CoverageSummary(), variants=[variant],
    )


@pytest.fixture()
def one_shot_input():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 10.0, 16.0)
        _static(s, p, a, g)
        pid = p.id
    return _input(pid)


def test_validator_accepts_a_faithful_plan(one_shot_input):
    pi = one_shot_input
    entry = pi.entries[0]
    ref = EvidenceRef(channel="static", result_id=entry.static.result_id or "",
                      segment_id=entry.segment_id, evidence_state=entry.static.evidence_state)
    shot = _base_variant(pi).shots[0].model_copy(update={
        "claims": [Claim(text="静帧观察：有湖", kind="observed_signal", evidence_refs=[ref])],
        "evidence_refs": [ref],
    })
    variant = _base_variant(pi).model_copy(update={"shots": [shot]})
    assert val.validate_plan(_plan(pi, variant), pi) == []


def test_validator_rejects_an_invented_segment(one_shot_input):
    pi = one_shot_input
    ghost = str(uuid.uuid4())
    variant = _base_variant(pi, segment_id=ghost)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "UNKNOWN_SEGMENT" in [p.code for p in problems]


def test_validator_rejects_a_shot_from_another_project(one_shot_input):
    pi = one_shot_input
    with session_scope() as s:
        p2 = _project(s, "other")
        a2 = _asset(s, p2)
        g2 = _segment(s, p2, a2, 0.0, 5.0)
        foreign = g2.id
    variant = _base_variant(pi, segment_id=foreign)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "UNKNOWN_SEGMENT" in [p.code for p in problems]


def test_validator_rejects_a_claim_without_evidence(one_shot_input):
    pi = one_shot_input
    variant = _base_variant(pi, claims=[
        Claim(text="镜头里出现了极光", kind="observed_signal"),
    ])
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "UNSUPPORTED_VISUAL_CLAIM" in [p.code for p in problems]


def test_validator_rejects_citing_evidence_the_shot_does_not_have(one_shot_input):
    pi = one_shot_input
    variant = _base_variant(pi, claims=[
        Claim(text="看到了雪山", kind="observed_signal",
              evidence_refs=[EvidenceRef(channel="static", result_id=str(uuid.uuid4()),
                                         segment_id=pi.entries[0].segment_id)]),
    ])
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "UNSUPPORTED_VISUAL_CLAIM" in [p.code for p in problems]


@pytest.mark.parametrize("evidence_state", ["uncertain", "not_visible"])
def test_validator_rejects_upgrading_a_hedged_reading(evidence_state):
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 0.0, 6.0)
        _static(s, p, a, g, state=evidence_state)
        pid = p.id
    pi = _input(pid)
    entry = pi.entries[0]
    ref = EvidenceRef(channel="static", result_id=entry.static.result_id or "",
                      segment_id=entry.segment_id, evidence_state=evidence_state)
    variant = _base_variant(pi, claims=[
        Claim(text="明确看到了湖面", kind="observed_signal", evidence_refs=[ref]),
    ])
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "SEMANTIC_UPGRADE" in [p.code for p in problems]


def test_validator_rejects_a_window_outside_the_source(one_shot_input):
    pi = one_shot_input
    entry = pi.entries[0]
    variant = _base_variant(pi, source_in_sec=entry.source_in_sec - 5.0,
                            source_out_sec=entry.source_out_sec,
                            planned_duration_sec=(entry.source_out_sec - entry.source_in_sec) + 5.0)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "INVALID_SHOT_WINDOW" in [p.code for p in problems]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -10.0])
def test_validator_rejects_non_finite_or_reversed_windows(one_shot_input, bad):
    pi = one_shot_input
    variant = _base_variant(pi, source_in_sec=bad)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "INVALID_SHOT_WINDOW" in [p.code for p in problems]


def test_validator_rejects_a_broken_timeline(one_shot_input):
    pi = one_shot_input
    variant = _base_variant(pi, timeline_start_sec=5.0)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "INVALID_TIMELINE" in [p.code for p in problems]


def test_validator_rejects_a_duration_that_does_not_match_the_window(one_shot_input):
    pi = one_shot_input
    variant = _base_variant(pi, planned_duration_sec=99.0)
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "INVALID_SHOT_DURATION" in [p.code for p in problems]


def test_validator_rejects_an_orphan_section(one_shot_input):
    pi = one_shot_input
    variant = _base_variant(pi, section_id="sec-nope")
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "ORPHAN_SECTION" in [p.code for p in problems]


def test_validator_rejects_the_same_segment_twice(one_shot_input):
    pi = one_shot_input
    v = _base_variant(pi)
    second = v.shots[0].model_copy(update={
        "shot_id": "s2", "timeline_start_sec": v.shots[0].planned_duration_sec,
    })
    variant = v.model_copy(update={
        "shots": [v.shots[0], second],
        "total_duration_sec": v.shots[0].planned_duration_sec * 2,
        "duration_gap_sec": -v.shots[0].planned_duration_sec,
    })
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "DUPLICATE_SEGMENT" in [p.code for p in problems]


def test_validator_rejects_variants_that_only_differ_by_title(one_shot_input):
    pi = one_shot_input
    v = _base_variant(pi)
    v2 = v.model_copy(update={"variant_id": "vy", "title": "不同的标题"})
    plan = PlanDraft(
        project_id=pi.project_id, planning_mode="model",
        generation_status="provisional", source_input_digest=pi.source_input_digest,
        coverage=CoverageSummary(), variants=[v, v2],
    )
    problems = val.validate_plan(plan, pi)
    assert "VARIANTS_NOT_DISTINCT" in [p.code for p in problems]


def test_validator_rejects_a_plan_built_on_different_input(one_shot_input):
    pi = one_shot_input
    plan = _plan(pi, _base_variant(pi)).model_copy(update={"source_input_digest": "stale"})
    problems = val.validate_plan(plan, pi)
    assert "DIRECTOR_INPUT_CHANGED" in [p.code for p in problems]


def test_validator_rejects_a_plan_for_another_project(one_shot_input):
    pi = one_shot_input
    plan = _plan(pi, _base_variant(pi)).model_copy(update={"project_id": str(uuid.uuid4())})
    problems = val.validate_plan(plan, pi)
    assert "PROJECT_MISMATCH" in [p.code for p in problems]


def test_a_video_event_claim_outside_the_shot_is_rejected():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, 10.0, 16.0)
        _video(s, p, a, g)
        pid = p.id
    pi = _input(pid)
    entry = pi.entries[0]
    ref = EvidenceRef(channel="video_native", result_id=entry.video_native.result_id or "",
                      segment_id=entry.segment_id,
                      event_start_sec=100.0, event_end_sec=101.0)
    variant = _base_variant(pi, claims=[
        Claim(text="远处发生了事件", kind="observed_signal", evidence_refs=[ref]),
    ])
    problems = val.validate_plan(_plan(pi, variant), pi)
    assert "INVALID_EVENT_TIME" in [p.code for p in problems]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def test_mock_provider_is_wired_for_tests(one_shot_input):
    provider = MockPlannerProvider(lambda pi, brief: [_base_variant(pi)])
    variants = provider.propose(one_shot_input, PlanningBrief())
    assert len(variants) == 1


def test_external_provider_makes_no_call_and_refuses(one_shot_input):
    provider = ExternalPlannerProvider()
    with pytest.raises(ModelCallNotAuthorized) as exc:
        provider.propose(one_shot_input, PlanningBrief())
    assert exc.value.code == "MODEL_CALL_NOT_AUTHORIZED"


def test_mock_mode_is_not_selectable():
    with pytest.raises(ValueError):
        provider_for("mock")


def test_offline_provider_is_the_production_default(one_shot_input):
    assert provider_for("offline_rules").name == "offline_rules"
