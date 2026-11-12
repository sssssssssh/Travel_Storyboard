"""TSB-371-R1 P2-03 — the report's gate status must come from evidence.

TSB-371 wrote the gate statuses as literal strings:

    "G4_MODEL_PLAN": {"status": "BLOCKED", "reason": "本轮未获得新的额度"}

That was true when it was written and would stay "true" after a successful A/B,
because nothing ever recomputed it. A report that cannot change its mind is not a
report; it is a decoration.

The rule here:

* ``NOT_RUN`` when nothing was attempted — never ``PASS``;
* ``PASS`` only from a real, recorded, successful call of the right purpose;
* ``BLOCKED`` when an attempt was made and refused;
* the existence of an A/B *page* is not evidence that an A/B *happened*.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.database import session_scope
from app.models import EvaluationSession, ModelCallRecord
from app.services import evaluation_report as rep


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _own_project(monkeypatch):
    """Give every test a private project id so records cannot leak between them."""
    name = f"gate-{uuid.uuid4()}"
    monkeypatch.setattr(rep, "_CURRENT_PROJECT", name, raising=False)
    return name


def _call(*, purpose="director_plan", channel="plan_text", project_id=None,
          succeeded=True, error_code=""):
    with session_scope() as s:
        row = ModelCallRecord(
            id=_uid(), channel=channel,
            project_id=project_id or rep._CURRENT_PROJECT, purpose=purpose,
            result_digest="", succeeded=succeeded, model_id="GLM",
            request_bytes=10, response_bytes=10, elapsed_ms=10,
            error_code=error_code,
        )
        s.add(row)
        s.flush()
        return row.id


def _gates(project_id=None):
    project_id = project_id or rep._CURRENT_PROJECT
    with session_scope() as s:
        return rep.gate_status(session=s, project_id=project_id)


# ---------------------------------------------------------------------------
# No evidence means NOT_RUN, never PASS
# ---------------------------------------------------------------------------

def test_no_call_means_not_run_not_pass():
    gates = _gates()
    assert gates["G4_MODEL_PLAN"]["status"] == "NOT_RUN"
    assert gates["G3_NATIVE_REAL"]["status"] == "NOT_RUN"


def test_a_not_run_gate_says_why():
    gates = _gates()
    for name in ("G4_MODEL_PLAN", "G3_NATIVE_REAL"):
        assert gates[name]["reason"], name


# ---------------------------------------------------------------------------
# A real recorded call is what turns a gate on
# ---------------------------------------------------------------------------

def test_a_successful_plan_call_passes_g4():
    _call(purpose="director_plan")
    assert _gates()["G4_MODEL_PLAN"]["status"] == "PASS"


def test_a_successful_video_call_passes_g3():
    _call(purpose="video_probe", channel="native_video")
    assert _gates()["G3_NATIVE_REAL"]["status"] == "PASS"


def test_a_failed_call_does_not_pass_a_gate():
    _call(purpose="director_plan", succeeded=False, error_code="MODEL_HTTP_503")
    assert _gates()["G4_MODEL_PLAN"]["status"] == "BLOCKED"


def test_a_call_of_the_wrong_purpose_does_not_pass():
    """A static image call must not make the planning gate look done."""
    _call(purpose="static_analysis", channel="static")
    assert _gates()["G4_MODEL_PLAN"]["status"] == "NOT_RUN"
    assert _gates()["G3_NATIVE_REAL"]["status"] == "NOT_RUN"


def test_a_call_from_another_project_does_not_pass():
    _call(purpose="director_plan", project_id="someone-else")
    assert _gates()["G4_MODEL_PLAN"]["status"] == "NOT_RUN"


def test_the_gate_reports_the_evidence_it_used():
    call_id = _call(purpose="director_plan")
    gate = _gates()["G4_MODEL_PLAN"]
    assert gate["evidence"], "a PASS must point at the record that justifies it"
    assert call_id in str(gate["evidence"])


# ---------------------------------------------------------------------------
# An A/B session is not proof that an A/B happened
# ---------------------------------------------------------------------------

def test_an_ab_session_alone_does_not_pass_g4():
    with session_scope() as s:
        s.add(EvaluationSession(
            id=_uid(), project_id=rep._CURRENT_PROJECT, kind="ab", status="IN_PROGRESS",
            sample_seed="", sampling_rule_version="1.0", reviewer="",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc), context_json={}))
        s.flush()
    # The page exists; no model call was made.
    assert _gates()["G4_MODEL_PLAN"]["status"] == "NOT_RUN"


def test_a_submitted_ab_alone_does_not_pass_g4():
    with session_scope() as s:
        s.add(EvaluationSession(
            id=_uid(), project_id=rep._CURRENT_PROJECT, kind="ab", status="SUBMITTED",
            sample_seed="", sampling_rule_version="1.0", reviewer="",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            context_json={"submission": {"preference": "A", "scores": {}, "note": ""}}))
        s.flush()
    assert _gates()["G4_MODEL_PLAN"]["status"] == "NOT_RUN"


# ---------------------------------------------------------------------------
# The report carries it through
# ---------------------------------------------------------------------------

def test_the_report_uses_the_computed_gates(client):
    pid = client.post("/api/projects", json={"name": "p2-3"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/evaluation/sessions",
                      json={"limit": 5}).json()["session_id"]
    body = client.get(
        f"/api/projects/{pid}/evaluation/reports/{sid}").json()
    assert body["gates"]["G4_MODEL_PLAN"]["status"] in (
        "NOT_RUN", "PASS", "BLOCKED")
    # Never blindly PASS: this session had no model call.
    assert body["gates"]["G4_MODEL_PLAN"]["status"] == "NOT_RUN"
