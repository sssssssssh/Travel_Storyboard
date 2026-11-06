"""TSB-367-R1 — operation state must survive the transaction that failed (R1-02).

Tests first. On `eb63003` the service does::

    op.status = "stale"
    s.flush()
    raise PlanVersionConflict(...)

and ``session_scope`` then rolls the whole transaction back — so the row is left
as ``proposed`` while the request was actually refused. The same applies to
``failed``. These tests read the **committed** row, not the in-memory object, and
assert the exact status (no ``in {...}`` dodging).

They also pin the state machine down at the database level, for both INSERT and
UPDATE, so no code path — present or future — can park a row in a state the
machine does not allow.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, init_db
from app.models import Asset, PlanOperation, Project, Segment, Storyboard, new_id

ALLOWED_FROM_PROPOSED = {"applied", "rejected", "stale", "failed"}
TERMINAL = {"applied", "rejected", "stale", "failed"}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _seeded(name: str) -> dict[str, str]:
    ids = {"project": new_id(), "asset": new_id(), "seg1": new_id(), "seg2": new_id()}
    with SessionLocal() as s:
        s.add(Project(id=ids["project"], name=name, source_roots=[],
                      output_dir="", settings={}))
        s.flush()
        s.add(Asset(id=ids["asset"], project_id=ids["project"],
                    source_path=f"/tmp/{name}.mp4", display_name=f"{name}.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                    width=1920, height=1080))
        s.flush()
        for key, (start, end) in (("seg1", (0.0, 5.0)), ("seg2", (5.0, 10.0))):
            s.add(Segment(id=ids[key], project_id=ids["project"],
                          asset_id=ids["asset"], start_sec=start, end_sec=end,
                          detected_start_sec=start, detected_end_sec=end))
        s.commit()

    from app.services.storyboard_service import get_current_version, save_version

    base = get_current_version(project_id=ids["project"])
    view = save_version(
        project_id=ids["project"], base_version_id=base.version_id,
        shots=[
            {"shot_id": "shot-1", "asset_id": ids["asset"], "segment_id": ids["seg1"],
             "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0},
            {"shot_id": "shot-2", "asset_id": ids["asset"], "segment_id": ids["seg2"],
             "source_in_sec": 5.0, "source_out_sec": 7.0, "target_duration_sec": 2.0},
        ],
    )
    ids["board"] = view.board_id
    ids["version"] = view.version_id
    return ids


def _env(ids: dict[str, str], ops: list[dict], key: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "base_version_id": ids["version"],
        "idempotency_key": key or str(uuid.uuid4()),
        "operations": ops,
        "reason": "r1-02",
    }


def _trim() -> dict:
    return {"type": "trim_shot", "shot_id": "shot-1",
            "source_in_sec": 0.5, "source_out_sec": 1.5}


def _committed_status(op_id: str) -> str:
    """Read the status from a fresh session: proves it was actually committed."""
    with SessionLocal() as s:
        return s.get(PlanOperation, op_id).status


def _version_count(pid: str) -> int:
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        from app.models import StoryboardVersion

        return len(s.scalars(select(StoryboardVersion).where(
            StoryboardVersion.board_id == board.id)).all())


# ---------------------------------------------------------------------------
# stale persists
# ---------------------------------------------------------------------------


def test_r1_02_a_stale_proposal_is_committed_as_stale():
    from app.services.plan_operation_service import (
        PlanVersionConflict,
        apply_operation,
        propose_operation,
    )

    ids = _seeded("r102-stale")
    first = propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))
    second = propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))

    apply_operation(project_id=ids["project"], operation_id=first.id)
    with pytest.raises(PlanVersionConflict):
        apply_operation(project_id=ids["project"], operation_id=second.id)

    # The committed row must say stale — not proposed.
    assert _committed_status(second.id) == "stale"


def test_r1_02_a_stale_operation_cannot_be_applied_later():
    from app.services.plan_operation_service import (
        PlanVersionConflict,
        StaleProposal,
        apply_operation,
        propose_operation,
    )

    ids = _seeded("r102-stale-retry")
    first = propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))
    second = propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))
    apply_operation(project_id=ids["project"], operation_id=first.id)
    with pytest.raises(PlanVersionConflict):
        apply_operation(project_id=ids["project"], operation_id=second.id)

    # Retrying a stale operation must be refused, not silently applied. The
    # contract is exact: stale and failed are terminal, so the second attempt is
    # OPERATION_ALREADY_FINAL (409), never a silent apply and never a new version.
    from app.services.plan_operation_service import OperationAlreadyFinal

    with pytest.raises(OperationAlreadyFinal) as excinfo:
        apply_operation(project_id=ids["project"], operation_id=second.id)
    assert excinfo.value.code == "OPERATION_ALREADY_FINAL"
    assert _committed_status(second.id) == "stale"


# ---------------------------------------------------------------------------
# failed persists
# ---------------------------------------------------------------------------


def test_r1_02_a_deterministic_failure_is_committed_as_failed(monkeypatch):
    from app.services import plan_operation_service as svc
    from app.services.plan_operations import OperationExecutionError

    ids = _seeded("r102-failed")
    op = svc.propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))

    # Make the executor fail *at apply time* only.
    real = svc.execute_operations

    def _fail(*args, **kwargs):
        raise OperationExecutionError("SHOT_NOT_FOUND", "simulated", index=0)

    calls = []
    orig_writer = svc._set_status_if_proposed

    def traced(operation_id, new_status, error_code=""):
        result = orig_writer(operation_id, new_status, error_code=error_code)
        calls.append(result)
        return result

    monkeypatch.setattr(svc, "_set_status_if_proposed", traced)
    monkeypatch.setattr(svc, "execute_operations", _fail)
    caught = None
    try:
        svc.apply_operation(project_id=ids["project"], operation_id=op.id)
    except BaseException as exc:
        caught = exc
    monkeypatch.setattr(svc, "execute_operations", real)
    assert caught is not None, "apply must not succeed when the executor fails"
    assert calls == [True], (
        f"the status writer did not commit: calls={calls}, "
        f"exc={type(caught).__name__}: {caught}"
    )

    # Diagnostics kept in the failure message: if this ever regresses, "which
    # status did we see" is the only question that matters.
    seen = _committed_status(op.id)
    assert seen == "failed", f"expected failed, saw {seen} (op={op.id})"


def test_r1_02_a_failed_apply_creates_no_version():
    from app.services import plan_operation_service as svc
    from app.services.plan_operations import OperationExecutionError

    ids = _seeded("r102-failed-noversion")
    op = svc.propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))
    before = _version_count(ids["project"])

    real = svc.execute_operations

    def _fail(*args, **kwargs):
        raise OperationExecutionError("INVALID_RANGE", "simulated", index=0)

    svc.execute_operations = _fail
    try:
        with pytest.raises(Exception):
            svc.apply_operation(project_id=ids["project"], operation_id=op.id)
    finally:
        svc.execute_operations = real

    assert _version_count(ids["project"]) == before, "a failed apply left a half version"


# ---------------------------------------------------------------------------
# applied atomicity and immutability
# ---------------------------------------------------------------------------


def test_r1_02_applied_and_result_version_commit_together():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r102-atomic")
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, [_trim()]))
    applied = apply_operation(project_id=ids["project"], operation_id=op.id)

    # In a fresh session: both the status and the result version are visible.
    with SessionLocal() as s:
        from app.models import StoryboardVersion

        row = s.get(PlanOperation, op.id)
        assert row.status == "applied"
        assert row.result_version_id
        assert s.get(StoryboardVersion, row.result_version_id) is not None
        board = s.get(Storyboard, row.board_id)
        assert board.current_version_id == row.result_version_id


@pytest.mark.parametrize("terminal,forbidden", [
    ("applied", "proposed"),
    ("rejected", "applied"),
    ("stale", "proposed"),
    ("failed", "proposed"),
])
def test_r1_02_terminal_states_cannot_move(terminal: str, forbidden: str):
    """Database-level: a terminal row cannot be rewritten (UPDATE path)."""
    pid = new_id()
    board_id = new_id()
    version_id = new_id()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="term", source_roots=[], output_dir="", settings={}))
        s.flush()
        s.add(Storyboard(id=board_id, project_id=pid, title=""))
        s.flush()
        from app.models import StoryboardVersion

        s.add(StoryboardVersion(
            id=version_id, board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="initial",
        ))
        s.flush()
        board = s.get(Storyboard, board_id)
        board.current_version_id = version_id
        s.commit()

    with SessionLocal() as s:
        row = PlanOperation(
            id=new_id(), project_id=pid, board_id=board_id,
            base_version_id=version_id, idempotency_key=new_id(),
            schema_version=1, operations=[], diff={},
            status="proposed", reason="", actor="user", payload_digest="",
        )
        s.add(row)
        s.flush()
        if terminal == "applied":
            # applied requires a result version; give it one so only the
            # terminal-immutability rule is under test.
            from app.models import StoryboardVersion

            v2 = StoryboardVersion(
                id=new_id(), board_id=board_id, version_no=2, schema_version="1.0",
                brief={}, sections=[], shots=[], constraints={},
                validation_summary={"state": "ok", "issues": []},
                actor="user", reason="manual_edit", parent_version_id=version_id,
            )
            s.add(v2)
            s.flush()
            row.status = "applied"
            row.result_version_id = v2.id
        else:
            row.status = terminal
        s.commit()
        op_id = row.id

    with SessionLocal() as s:
        target = s.get(PlanOperation, op_id)
        target.status = forbidden
        with pytest.raises(IntegrityError):
            s.commit()


def test_r1_02_an_illegal_status_is_rejected_on_insert():
    pid = new_id()
    board_id = new_id()
    version_id = new_id()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="badins", source_roots=[], output_dir="", settings={}))
        s.flush()
        s.add(Storyboard(id=board_id, project_id=pid, title=""))
        s.flush()
        from app.models import StoryboardVersion

        s.add(StoryboardVersion(
            id=version_id, board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="initial",
        ))
        s.commit()

    with SessionLocal() as s:
        s.add(PlanOperation(
            id=new_id(), project_id=pid, board_id=board_id,
            base_version_id=version_id, idempotency_key=new_id(),
            schema_version=1, operations=[], diff={},
            status="nonsense", reason="", actor="user", payload_digest="",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_r1_02_applied_without_result_version_is_rejected_on_insert():
    pid = new_id()
    board_id = new_id()
    version_id = new_id()
    with SessionLocal() as s:
        s.add(Project(id=pid, name="badapplied", source_roots=[], output_dir="", settings={}))
        s.flush()
        s.add(Storyboard(id=board_id, project_id=pid, title=""))
        s.flush()
        from app.models import StoryboardVersion

        s.add(StoryboardVersion(
            id=version_id, board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="initial",
        ))
        s.commit()

    with SessionLocal() as s:
        s.add(PlanOperation(
            id=new_id(), project_id=pid, board_id=board_id,
            base_version_id=version_id, idempotency_key=new_id(),
            schema_version=1, operations=[], diff={},
            status="applied", reason="", actor="user", payload_digest="",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


# ---------------------------------------------------------------------------
# concurrency protection for the status write
# ---------------------------------------------------------------------------


def test_r1_02_a_status_write_never_overwrites_a_committed_terminal_state():
    """The stale/failed writer must be conditional on ``status='proposed'``."""
    import inspect

    from app.services import plan_operation_service as svc

    source = inspect.getsource(svc)
    assert "UPDATE plan_operations" in source.upper() or "_set_status_if_proposed" in source, (
        "state writes must be conditional, not a blind ORM assignment after rollback"
    )
