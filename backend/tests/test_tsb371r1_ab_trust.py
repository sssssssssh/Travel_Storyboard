"""TSB-371-R1 P1-03 — the blind comparison must be fair and provably real.

Two independent defects are pinned here.

**1. Content fingerprint.** The old `_fingerprint()` hashed the whole draft minus a
few fields, so `planning_mode` and other metadata were inside the hash. Two plans
that cut *exactly the same shots* therefore looked different, and a fake A/B was
accepted. The fingerprint must describe the edit — order, segment, window, role —
and nothing else.

**2. Provenance.** The old receipt check was "these four fields are non-empty",
which a client can satisfy by typing them. A client-supplied receipt proves nothing.
The proof must be produced server-side: a call record written by the trusted
planner path, keyed by an id the client cannot invent.

The rule for a client receipt is therefore: **it is not evidence**. Only a server
record is.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.database import session_scope
from app.models import ModelCallRecord
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


def _uid() -> str:
    return str(uuid.uuid4())


def _shot(order: int, segment_id: str, *, start: float, end: float,
          role: str = "development", reason: str = "r") -> ShotDraft:
    ref = EvidenceRef(channel="static", result_id="res-1", segment_id=segment_id)
    return ShotDraft(
        shot_id=f"s{order}", segment_id=segment_id, asset_id="a1",
        section_id="sec1", source_in_sec=start, source_out_sec=end,
        planned_duration_sec=end - start, timeline_start_sec=float(order * 4),
        narrative_role=role, selection_reason=reason,
        claims=[Claim(text="claim", kind="observed_signal", evidence_refs=[ref])],
        evidence_refs=[ref],
    )


def _plan(shots: list[ShotDraft], *, mode: str = "offline_rules",
          digest: str = "d" * 64, target: float = 60.0,
          summary: dict | None = None) -> PlanDraft:
    variant = VariantDraft(
        variant_id="v1", title="t", narrative_angle="chronological", logline="l",
        status="provisional",
        sections=[SectionDraft(section_id="sec1", kind="body", title="主体")],
        shots=shots,
        total_duration_sec=sum(s.planned_duration_sec for s in shots),
        target_duration_sec=target,
        duration_gap_sec=target - sum(s.planned_duration_sec for s in shots),
    )
    return PlanDraft(
        project_id="p1", planning_mode=mode, generation_status="provisional",
        source_input_digest=digest, coverage=CoverageSummary(), variants=[variant],
        request_summary=summary or {},
    )


# ---------------------------------------------------------------------------
# 1. The fingerprint describes the edit, not the metadata
# ---------------------------------------------------------------------------

def test_two_identical_edits_have_the_same_fingerprint():
    a = _plan([_shot(0, "seg-a", start=0, end=4)])
    b = _plan([_shot(0, "seg-a", start=0, end=4)])
    assert ab.content_fingerprint(a) == ab.content_fingerprint(b)


def test_metadata_does_not_change_the_fingerprint():
    """planning_mode / model id / warnings / limitations are not part of the edit."""
    a = _plan([_shot(0, "seg-a", start=0, end=4)])
    b = _plan(
        [_shot(0, "seg-a", start=0, end=4)],
        mode="model",
        summary={"model_id": "GLM", "provider": "openai_compatible",
                 "receipt": {"call_id": "x"}},
    )
    assert ab.content_fingerprint(a) == ab.content_fingerprint(b)


def test_a_different_order_is_a_different_edit():
    a = _plan([_shot(0, "seg-a", start=0, end=4), _shot(1, "seg-b", start=4, end=8)])
    b = _plan([_shot(0, "seg-b", start=4, end=8), _shot(1, "seg-a", start=0, end=4)])
    assert ab.content_fingerprint(a) != ab.content_fingerprint(b)


def test_a_different_window_is_a_different_edit():
    a = _plan([_shot(0, "seg-a", start=0, end=4)])
    b = _plan([_shot(0, "seg-a", start=1, end=5)])
    assert ab.content_fingerprint(a) != ab.content_fingerprint(b)


def test_a_different_role_is_a_different_edit():
    a = _plan([_shot(0, "seg-a", start=0, end=4, role="opening")])
    b = _plan([_shot(0, "seg-a", start=0, end=4, role="closing")])
    assert ab.content_fingerprint(a) != ab.content_fingerprint(b)


def test_a_different_reason_is_the_same_edit():
    """The reason is prose, not the cut: two plans may explain it differently."""
    a = _plan([_shot(0, "seg-a", start=0, end=4, reason="rules picked it")])
    b = _plan([_shot(0, "seg-a", start=0, end=4, reason="the model picked it")])
    assert ab.content_fingerprint(a) == ab.content_fingerprint(b)


def test_identical_content_is_refused_as_an_ab_pair():
    a = _plan([_shot(0, "seg-a", start=0, end=4)])
    b = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model")
    problems = ab.check_pairing(a, b, None)
    assert any(p["code"] == "AB_NOT_DISTINCT" for p in problems), problems


def test_different_content_is_not_refused_for_being_identical():
    a = _plan([_shot(0, "seg-a", start=0, end=4)])
    b = _plan([_shot(0, "seg-a", start=0, end=5)], mode="model")
    problems = ab.check_pairing(a, b, None)
    assert not any(p["code"] == "AB_NOT_DISTINCT" for p in problems)


# ---------------------------------------------------------------------------
# 2. A client receipt is not evidence
# ---------------------------------------------------------------------------

def test_a_client_supplied_receipt_is_rejected():
    """Typing plausible field values proves nothing; only a server record does."""
    forged = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model", summary={
        "receipt": {
            "model_id": "GLM-5.3-Flash", "provider": "openai_compatible",
            "call_id": "made-up", "elapsed_ms": 100, "attempts": 1,
        }
    })
    assert ab.has_model_receipt(forged) is False


def test_no_receipt_at_all_is_rejected():
    bare = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model")
    assert ab.has_model_receipt(bare) is False


def test_a_receipt_pointing_at_no_server_record_is_rejected():
    phantom = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model", summary={
        "receipt": {"call_id": _uid(), "server": True}})
    with session_scope() as s:
        assert ab.has_model_receipt(phantom, session=s) is False


def test_a_server_recorded_call_is_accepted():
    """The planner writes this row; a client cannot."""
    call_id = _uid()
    with session_scope() as s:
        s.add(ModelCallRecord(
            id=call_id, channel="plan_text", project_id="p1",
            result_digest="", purpose="director_plan", succeeded=True,
            request_bytes=100, response_bytes=200, elapsed_ms=300,
        ))
        s.flush()
        plan = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model",
                     summary={"receipt": {"call_id": call_id}})
        assert ab.has_model_receipt(plan, session=s) is True


def test_a_recorded_failed_call_is_not_evidence():
    """A call that errored must not license a B side."""
    call_id = _uid()
    with session_scope() as s:
        s.add(ModelCallRecord(
            id=call_id, channel="plan_text", project_id="p1",
            result_digest="", purpose="director_plan", succeeded=False,
            request_bytes=100, response_bytes=0, elapsed_ms=300,
        ))
        s.flush()
        plan = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model",
                     summary={"receipt": {"call_id": call_id}})
        assert ab.has_model_receipt(plan, session=s) is False


def test_a_call_from_another_project_is_not_evidence():
    call_id = _uid()
    with session_scope() as s:
        s.add(ModelCallRecord(
            id=call_id, channel="plan_text", project_id="other-project",
            result_digest="", purpose="director_plan", succeeded=True,
            request_bytes=1, response_bytes=1, elapsed_ms=1,
        ))
        s.flush()
        plan = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model",
                     summary={"receipt": {"call_id": call_id}})
        assert ab.has_model_receipt(plan, session=s, project_id="p1") is False


def test_pairing_without_a_session_refuses_a_model_side():
    """No database means no evidence — pairing must not fall back to trusting."""
    plan = _plan([_shot(0, "seg-a", start=0, end=4)], mode="model",
                 summary={"receipt": {"call_id": _uid()}})
    offline = _plan([_shot(0, "seg-a", start=0, end=5)])
    problems = ab.check_pairing(offline, plan, None)
    assert any(p["code"] == "AB_NO_REAL_MODEL_PLAN" for p in problems), problems
