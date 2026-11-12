"""TSB-371-R1 P1-05 — a submitted A/B must stay submitted.

TSB-371's ``blind_view`` returned ``revealed: False`` unconditionally, so after
submitting, a refresh dropped the reviewer back into the blind view and the reveal
was lost. Worse, the mapping was still in the database — the reviewer simply could
not see the answer they had already earned.

The rule: the *server* decides what to show, from the stored state.

* not submitted  -> blind view, no provenance anywhere;
* submitted       -> revealed view, read-only, same result on every read;
* resubmitting    -> refused;
* neither query parameters nor client state can change that.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session_scope
from app.models import EvaluationSession
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


def _uid() -> str:
    return str(uuid.uuid4())


def _shot_channel(segment_id: str, result_id: str) -> str:
    """Channel of the reading the shot cites."""
    from app.models import SegmentSemanticResult

    with session_scope() as s:
        row = s.get(SegmentSemanticResult, result_id)
        return "static" if row is not None else "static"


def _shot(order: int, segment_id: str, *, start: float, end: float,
          role: str = "development", result_id: str = "res-1",
          asset_id: str = "a1") -> ShotDraft:
    ref = EvidenceRef(channel="static", result_id=result_id, segment_id=segment_id)
    return ShotDraft(
        shot_id=f"s{order}", segment_id=segment_id, asset_id=asset_id,
        section_id="sec1", source_in_sec=start, source_out_sec=end,
        planned_duration_sec=end - start, timeline_start_sec=float(order * 4),
        narrative_role=role, selection_reason="r",
        claims=[Claim(text="c", kind="observed_signal", evidence_refs=[ref])],
        evidence_refs=[ref],
    )


def _real_digest(project_id: str) -> str:
    with session_scope() as s:
        return dpi.build_planning_input(project_id, session=s).source_input_digest


def _plan(shots, *, mode="offline_rules", project_id="p1", target=60.0,
          digest: str = "d" * 64) -> PlanDraft:
    total = sum(s.planned_duration_sec for s in shots)
    variant = VariantDraft(
        variant_id="v1", title="t", narrative_angle="chronological", logline="l",
        status="provisional",
        sections=[SectionDraft(section_id="sec1", kind="body", title="主体")],
        shots=shots, total_duration_sec=total, target_duration_sec=target,
        duration_gap_sec=target - total,
    )
    return PlanDraft(
        project_id=project_id, planning_mode=mode,
        generation_status="provisional", source_input_digest=digest,
        coverage=CoverageSummary(), variants=[variant],
    )


def _receipted(plan: PlanDraft) -> PlanDraft:
    """A model plan whose receipt names a real server record."""
    call_id = _uid()
    with session_scope() as s:
        s.add(ab_call_record(call_id, plan.project_id))
        s.flush()
    summary = dict(plan.request_summary or {})
    summary["receipt"] = {"call_id": call_id, "model_id": "GLM",
                          "provider": "openai_compatible", "elapsed_ms": 1}
    return plan.model_copy(update={"request_summary": summary})


def ab_call_record(call_id: str, project_id: str):
    from app.models import ModelCallRecord

    return ModelCallRecord(
        id=call_id, channel="plan_text", project_id=project_id,
        purpose="director_plan", result_digest="", succeeded=True,
        model_id="GLM", request_bytes=1, response_bytes=1, elapsed_ms=1,
    )


def _pair() -> str:
    """Create an A/B session directly and return its id."""
    offline = _plan([_shot(0, "seg-a", start=0, end=4)])
    model = _receipted(_plan([_shot(0, "seg-a", start=0, end=5)], mode="model"))
    with session_scope() as s:
        pair = ab.create_ab_session(s, "p1", offline, model, None)
        s.commit()
        return pair.session.id


# ---------------------------------------------------------------------------
# Before submission: still blind
# ---------------------------------------------------------------------------

def test_an_unsubmitted_session_stays_blind():
    sid = _pair()
    with session_scope() as s:
        view = ab.blind_view(s.get(EvaluationSession, sid))
    assert view["revealed"] is False
    assert "mapping" not in view


# ---------------------------------------------------------------------------
# After submission: the reveal survives a reload
# ---------------------------------------------------------------------------

def test_a_submitted_session_returns_the_revealed_view():
    """The bug: a reload dropped the reviewer back into the blind view."""
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid),
                             {"preference": "A"})
        s.commit()

    with session_scope() as s:
        view = ab.current_view(s.get(EvaluationSession, sid))
    assert view["revealed"] is True
    assert set(view["mapping"]) == {"A", "B"}


def test_the_reveal_is_stable_across_many_reads():
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "B"})
        s.commit()

    seen = []
    for _ in range(5):
        with session_scope() as s:
            seen.append(ab.current_view(s.get(EvaluationSession, sid))["submission"])
    assert all(x == seen[0] for x in seen)


def test_the_blind_judgement_is_preserved_verbatim():
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), {
            "preference": "A",
            "a_scores": {"coherence": 4, "pacing": 3},
            "note": "A 的节奏更好",
        })
        s.commit()
    with session_scope() as s:
        sub = ab.current_view(s.get(EvaluationSession, sid))["submission"]
    assert sub["preference"] == "A"
    assert sub["scores"]["A"]["coherence"] == 4
    assert sub["note"] == "A 的节奏更好"


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------

def test_a_submitted_session_cannot_be_resubmitted():
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "A"})
        s.commit()
    with pytest.raises(ab.AbError) as exc:
        with session_scope() as s:
            ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "B"})
    assert exc.value.code == "AB_ALREADY_SUBMITTED"


def test_a_rejected_resubmission_does_not_change_the_result():
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "A"})
        s.commit()
    before = None
    with session_scope() as s:
        before = dict(ab.current_view(s.get(EvaluationSession, sid))["submission"])
    try:
        with session_scope() as s:
            ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "B"})
    except ab.AbError:
        pass
    with session_scope() as s:
        after = dict(ab.current_view(s.get(EvaluationSession, sid))["submission"])
    assert after == before


# ---------------------------------------------------------------------------
# The client cannot talk its way out of the blind
# ---------------------------------------------------------------------------

def test_a_client_asking_to_reveal_early_gets_the_blind_view():
    """The query string is a hint at most; the server decides."""
    sid = _pair()
    with session_scope() as s:
        view = ab.current_view(s.get(EvaluationSession, sid),
                               wants_reveal=True)
    assert view["revealed"] is False
    assert "mapping" not in view


def test_a_submitted_session_always_reveals_even_if_the_client_says_blind():
    sid = _pair()
    with session_scope() as s:
        ab.submit_preference(s.get(EvaluationSession, sid), {"preference": "A"})
        s.commit()
    with session_scope() as s:
        view = ab.current_view(s.get(EvaluationSession, sid),
                               wants_reveal=False)
    assert view["revealed"] is True


# ---------------------------------------------------------------------------
# Through the HTTP API
# ---------------------------------------------------------------------------

def _real_project(client) -> tuple[str, str, str, str]:
    """A project with one analysed shot, so the validator has real input."""
    import os
    from datetime import datetime, timezone
    from pathlib import Path as _P

    from app.models import Asset, Segment, SegmentSemanticResult

    pid = client.post("/api/projects", json={"name": "ab-real"}).json()["id"]
    root = _P(os.environ.get("TEMP") or ".") / "tsb371r1-ab"
    root.mkdir(parents=True, exist_ok=True)
    src = root / f"{pid[:8]}.mp4"
    src.write_bytes(b"\x00" * 64)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=60.0, fps_num=25, fps_den=1,
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
            detected_start_sec=0.0, detected_end_sec=6.0,
            start_sec=0.0, end_sec=6.0, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        res_id = _uid()
        s.add(SegmentSemanticResult(
            id=res_id, project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"], "subjects": ["boats"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
        seg_id, result_id, asset_id = seg.id, res_id, a.id
    return pid, seg_id, result_id, asset_id


def test_the_api_returns_the_revealed_view_after_a_reload(client):
    pid, seg_id, res_id, asset_id = _real_project(client)
    digest = _real_digest(pid)
    offline = _plan([_shot(0, seg_id, start=0, end=4, result_id=res_id,
                           asset_id=asset_id)], project_id=pid, digest=digest)
    model = _receipted(_plan([_shot(0, seg_id, start=0, end=5, result_id=res_id,
                                    asset_id=asset_id)],
                             mode="model", project_id=pid, digest=digest))
    created = client.post(AB_POST.format(p=pid), json={
        "offline_plan": offline.model_dump(mode="json"),
        "model_plan": model.model_dump(mode="json"),
    })
    assert created.status_code in (200, 201), created.text + " || " + str(created.json())
    sid = created.json()["session_id"]
    assert created.json()["revealed"] is False

    sub = client.post(f"{AB_POST.format(p=pid)}/{sid}/submit",
                      json={"preference": "A"})
    assert sub.status_code == 200, sub.text

    # A reload is just another GET.
    again = client.get(f"{AB_POST.format(p=pid)}/{sid}")
    assert again.status_code == 200
    assert again.json()["revealed"] is True
    assert set(again.json()["mapping"]) == {"A", "B"}

    # And it stays that way.
    third = client.get(f"{AB_POST.format(p=pid)}/{sid}")
    assert third.json()["submission"] == again.json()["submission"]


def test_the_api_refuses_a_second_submission(client):
    pid, seg_id, res_id, asset_id = _real_project(client)
    digest = _real_digest(pid)
    offline = _plan([_shot(0, seg_id, start=0, end=4, result_id=res_id,
                           asset_id=asset_id)], project_id=pid, digest=digest)
    model = _receipted(_plan([_shot(0, seg_id, start=0, end=5, result_id=res_id,
                                    asset_id=asset_id)],
                             mode="model", project_id=pid, digest=digest))
    sid = client.post(AB_POST.format(p=pid), json={
        "offline_plan": offline.model_dump(mode="json"),
        "model_plan": model.model_dump(mode="json"),
    }).json()["session_id"]
    client.post(f"{AB_POST.format(p=pid)}/{sid}/submit", json={"preference": "A"})
    second = client.post(f"{AB_POST.format(p=pid)}/{sid}/submit",
                         json={"preference": "B"})
    assert second.status_code == 409
