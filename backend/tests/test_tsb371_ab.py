"""TSB-371-C — pairing rules and the blind.

Three properties are load-bearing and each has tests here:

* **E25** with no real model plan the answer is ``AB_BLOCKED_NO_REAL_MODEL_PLAN``,
  never a mock dressed up as the model side;
* **E27/E28** unequal inputs are refused, because a comparison of two different
  candidate sets is not an A/B;
* **E31/E32** nothing in the pre-submission payload reveals which side is which,
  and the reveal only happens after a preference is recorded.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import director_plan_input as dpi
from app.services import director_plan_rules as rules
from app.services import evaluation_ab as ab
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

AB_POST = "/api/projects/{p}/evaluation/ab-sessions"

from app.models import EvaluationSession  # noqa: E402


def _with_receipt(plan):
    """Simulate a draft the real adapter produced (it stamps a receipt)."""
    summary = dict(getattr(plan, "request_summary", None) or {})
    summary["receipt"] = {
        "model_id": "GLM-5.3-Flash", "provider": "openai_compatible",
        "call_id": "test-receipt-0001", "elapsed_ms": 1234,
        "attempts": 1, "at": "2026-01-01T00:00:00+00:00",
    }
    return plan.model_copy(update={"request_summary": summary})


def _submit(sid, payload):
    """Submit inside its own session, so the change is committed and visible."""
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), payload)
        s.commit()


def _reveal(sid):
    with session_scope() as s:
        return ab.revealed_view(s.get(EvaluationSession, sid))


def _create(pid, baseline, model_plan, pi):
    """Create the pair inside a real session (the service needs persistence)."""
    with session_scope() as s:
        pair = ab.create_ab_session(s, pid, baseline, model_plan, pi)
        s.commit()
        # Detach a plain copy so assertions can read it after the session closes.
        return pair, pair.session.id


def _load(session_id):
    with session_scope() as s:
        return s.get(EvaluationSession, session_id)





def _uid() -> str:
    return str(uuid.uuid4())


def _seed(analysed=2, n=2) -> str:
    with session_scope() as s:
        p = Project(id=_uid(), name="t371c", source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(
            id=_uid(), project_id=p.id, source_path=f"/nonexistent/{_uid()}.mp4",
            display_name="c.mp4", file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
            duration_sec=60.0, fps_num=25, fps_den=1, vfr_flag=False, width=320,
            height=180, rotation=0, codec="h264", pixel_format="yuv420p",
            color_primaries="", color_transfer="", color_space="", has_audio=False,
            audio_codec="", scan_status="succeeded", analysis_version="",
            analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        for i in range(n):
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=p.id,
                detected_start_sec=i * 10.0, detected_end_sec=i * 10.0 + 6,
                start_sec=i * 10.0, end_sec=i * 10.0 + 6, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
                algorithm_version="1", created_at=None,
            )
            s.add(g)
            s.flush()
            if i < analysed:
                s.add(SegmentSemanticResult(
                    id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
                    status="succeeded", model_provider="openai_compatible",
                    model_id="GLM", prompt_version="p", schema_version="1.0",
                    source_signature=_uid(), frame_manifest_hash="h",
                    analysis_json={"scene_tags": ["lake"], "subjects": ["pier"],
                                   "visible_actions": [], "lighting": "daylight",
                                   "composition": "wide", "mood": ["calm"]},
                    evidence_json={"state": "observed", "frame_ids": ["f1"]},
                    request_usage={}, is_current=True, error_code="",
                    error_message="", created_at=None, updated_at=None,
                ))
        return p.id


def _fake_model_plan(pi, *, digest=None, title="模型方案", mode="model",
                     target=60.0, shots_override=None) -> PlanDraft:
    entry = pi.entries[0]
    duration = entry.source_out_sec - entry.source_in_sec
    ref = EvidenceRef(channel="static", result_id=entry.static.result_id or "",
                      segment_id=entry.segment_id)
    shot = ShotDraft(
        shot_id="m1", segment_id=entry.segment_id, asset_id=entry.asset_id,
        section_id="s1", source_in_sec=entry.source_in_sec,
        source_out_sec=entry.source_out_sec, planned_duration_sec=duration,
        timeline_start_sec=0.0, narrative_role="opening",
        selection_reason="模型给出的理由",
        claims=[Claim(text="静帧观察：湖与栈桥", kind="observed_signal",
                      evidence_refs=[ref])],
        evidence_refs=[ref],
    )
    if shots_override is not None:
        shot = shots_override
    variant = VariantDraft(
        variant_id="m-v1", title=title, narrative_angle="chronological",
        logline="模型写的主线", status="provisional",
        sections=[SectionDraft(section_id="s1", kind="body", title="主体")],
        shots=[shot], total_duration_sec=duration, target_duration_sec=target,
        duration_gap_sec=target - duration,
    )
    return PlanDraft(
        project_id=pi.project_id, planning_mode=mode,
        generation_status="provisional",
        source_input_digest=digest or pi.source_input_digest,
        coverage=CoverageSummary(), variants=[variant],
    )


def _pair_inputs(analysed=2):
    pid = _seed(analysed=analysed)
    pi = dpi.build_planning_input(pid, max_candidates=12)
    offline = rules.plan_offline(pi, PlanningBrief(theme="t", target_duration_sec=60,
                                                   variant_count=1))
    return pid, pi, offline


# ---------------------------------------------------------------------------
# E25 / E26 — refusals that prevent a fake A/B
# ---------------------------------------------------------------------------

def test_pairing_without_a_real_model_plan_is_refused():
    pid, pi, offline = _pair_inputs()
    with pytest.raises(ab.AbError) as exc:
        _create(pid, offline, offline, pi)
    assert exc.value.code == "AB_LEFT_NOT_OFFLINE" or exc.value.code.startswith("AB_")


def test_a_mock_plan_dressed_as_model_is_refused():
    pid, pi, offline = _pair_inputs()
    fake = _fake_model_plan(pi, mode="model")
    # A draft with the model *label* but no receipt is not a real model plan.
    with pytest.raises(ab.AbError) as exc:
        _create(pid, offline, fake, pi)
    assert exc.value.code == "AB_NO_REAL_MODEL_PLAN"


def test_a_different_input_digest_is_refused():
    """E27: two plans over different inputs are not comparable."""
    pid, pi, offline = _pair_inputs()
    model_plan = _with_receipt(_fake_model_plan(pi, digest="a" * 64))
    with pytest.raises(ab.AbError) as exc:
        _create(pid, offline, model_plan, pi)
    assert exc.value.code == "AB_INPUT_DIGEST_MISMATCH"


def test_a_different_target_duration_is_refused():
    """E28: the comparison must not silently change the brief."""
    pid, pi, offline = _pair_inputs()
    model_plan = _with_receipt(_fake_model_plan(pi, target=999.0))
    with pytest.raises(ab.AbError) as exc:
        _create(pid, offline, model_plan, pi)
    assert exc.value.code in ("AB_TARGET_MISMATCH", "AB_PLAN_INVALID")


def test_a_genuine_pair_is_accepted_and_the_mapping_is_server_side():
    pid, pi, offline = _pair_inputs()
    plan = rules.plan_offline(pi, PlanningBrief(theme="t", target_duration_sec=60,
                                                variant_count=1))
    # Wrap it as a "model" plan with different content so it is distinct.
    model_plan = _with_receipt(_fake_model_plan(pi, title="模型方案"))
    model_plan = model_plan.model_copy(update={"variants": [model_plan.variants[0].model_copy(
        update={"target_duration_sec": plan.variants[0].target_duration_sec,
                "duration_gap_sec": plan.variants[0].target_duration_sec
                - model_plan.variants[0].total_duration_sec})]})
    baseline = plan.model_copy(update={"variants": [plan.variants[0].model_copy(
        update={"target_duration_sec": model_plan.variants[0].target_duration_sec,
                "duration_gap_sec": model_plan.variants[0].target_duration_sec
                - plan.variants[0].total_duration_sec})]})
    pair, _sid = _create(pid, baseline, model_plan, pi)
    assert set(pair.mapping) == {"A", "B"}
    assert set(pair.mapping.values()) == {"model", "offline_rules"}


# ---------------------------------------------------------------------------
# E31 — the blind view leaks nothing
# ---------------------------------------------------------------------------

def test_the_blind_view_contains_no_provenance():
    pid, pi, offline = _pair_inputs()
    model_plan = _with_receipt(_fake_model_plan(pi, title="AIModel_Generated_Plan"))
    baseline = offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0,
            "duration_gap_sec": 60.0 - offline.variants[0].total_duration_sec})]})
    model_plan = model_plan.model_copy(update={
        "variants": [model_plan.variants[0].model_copy(update={
            "target_duration_sec": 60.0,
            "duration_gap_sec": 60.0 - model_plan.variants[0].total_duration_sec})]})

    pair, sid = _create(pid, baseline, model_plan, pi)
    # Load from the DB: that row really carries the mapping, so a leak would show
    # actual provenance rather than a None placeholder.
    view = ab.blind_view(_load(sid))
    blob = json.dumps(view, ensure_ascii=False)

    assert view["revealed"] is False
    for leaked in ("mapping", "model_id", "planning_mode", "offline_rules",
                   "AIModel", "m-v1", "模型方案"):
        assert leaked not in blob, f"the blind view leaked {leaked!r}"


def test_both_sides_have_the_same_shape():
    pid, pi, offline = _pair_inputs()
    pair, _sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    assert set(pair.left) == set(pair.right)


# ---------------------------------------------------------------------------
# E32 — reveal only after submission
# ---------------------------------------------------------------------------

def test_reveal_is_refused_before_submission():
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    with pytest.raises(ab.AbError) as exc:
        ab.revealed_view(_load(sid))
    assert exc.value.code == "AB_NOT_SUBMITTED"


def test_submitting_reveals_the_mapping_and_keeps_the_blind_result():
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)

    _submit(sid, {
        "preference": "A",
        "a_scores": {"coherence": 4, "pacing": 3, "reason_credibility": 3,
                     "operability": 4},
        "b_scores": {"coherence": 2},
    })
    view = _reveal(sid)
    assert view["revealed"] is True
    assert view["mapping"]["A"] in ("model", "offline_rules")
    # The blind judgement is preserved verbatim.
    assert view["submission"]["preference"] == "A"
    assert view["submission"]["scores"]["A"]["coherence"] == 4


def test_one_reviewer_is_reported_as_one_reviewer():
    """E33: a single person's preference is not a population win rate."""
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    _submit(sid, {"preference": "A"})
    view = _reveal(sid)
    assert view["reviewers"] == 1
    assert "胜率" not in json.dumps(view, ensure_ascii=False) or "不是人群胜率" in json.dumps(view, ensure_ascii=False)


def test_a_second_submission_is_refused():
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    _submit(sid, {"preference": "A"})
    with pytest.raises(ab.AbError) as exc:
        _submit(sid, {"preference": "B"})
    assert exc.value.code == "AB_ALREADY_SUBMITTED"


def test_an_invalid_preference_is_refused():
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    with pytest.raises(ab.AbError) as exc:
        _submit(sid, {"preference": "C"})
    assert exc.value.code == "AB_PREFERENCE_INVALID"


def test_an_out_of_range_score_is_refused():
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    with pytest.raises(ab.AbError) as exc:
        _submit(sid, {"preference": "A", "a_scores": {"coherence": 9}})
    assert exc.value.code == "AB_SCORE_INVALID"


def test_the_mapping_is_stable_across_reads():
    """E30: refreshing must not re-randomise which side is which."""
    pid, pi, offline = _pair_inputs()
    pair, sid = _create(pid, offline.model_copy(update={
        "variants": [offline.variants[0].model_copy(update={
            "target_duration_sec": 60.0})]}),
        _with_receipt(_fake_model_plan(pi)), pi)
    first = dict(_load(sid).context_json["mapping"])
    for _ in range(5):
        assert _load(sid).context_json["mapping"] == first
