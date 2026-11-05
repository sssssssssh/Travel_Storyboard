"""TSB-367 — PlanOperation model, state machine and database invariants (§3).

Written first. On the pre-TSB-367 tree every case here fails because the table,
its triggers and the lock dimensions do not exist.

The guarantees asserted:

* ``applied`` **must** carry a ``result_version_id`` that lives in the same board;
* a terminal operation cannot be rewritten (no silent "un-apply");
* ``base_version_id`` must belong to the operation's board;
* one ``idempotency_key`` per board, enforced by the database;
* ``status`` is a closed set;
* lock dimensions are validated and ``all`` expands to the union.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine, init_db
from app.models import (
    PlanOperation,
    Project,
    Storyboard,
    StoryboardVersion,
    new_id,
)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _board(project_name: str = "t367") -> tuple[str, str, str]:
    """Create project + board + v1; return (project_id, board_id, version_id)."""
    with SessionLocal() as s:
        p = Project(id=new_id(), name=project_name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        b = Storyboard(id=new_id(), project_id=p.id, title="")
        s.add(b)
        s.flush()
        v = StoryboardVersion(
            id=new_id(), board_id=b.id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="initial",
        )
        s.add(v)
        s.flush()
        b.current_version_id = v.id
        s.commit()
        return p.id, b.id, v.id


def _op(pid: str, board_id: str, base: str, **kw) -> PlanOperation:
    defaults = dict(
        project_id=pid, board_id=board_id, base_version_id=base,
        idempotency_key=new_id(), schema_version=1,
        operations=[], diff={}, status="proposed", reason="", actor="user",
        payload_digest="",
    )
    defaults.update(kw)
    return PlanOperation(**defaults)


# --- table shape -----------------------------------------------------------


def test_plan_operation_table_has_the_contract_columns():
    cols = {c.name for c in PlanOperation.__table__.columns}
    expected = {
        "id", "board_id", "base_version_id", "message_id", "idempotency_key",
        "schema_version", "operations", "diff", "status", "reason",
        "result_version_id", "actor", "created_at", "updated_at",
    }
    assert expected <= cols, f"missing: {expected - cols}"


def test_status_values_are_a_closed_set():
    """A typo in ``status`` must be refused by the database, not stored."""
    pid, board_id, base = _board("t367-status")
    with SessionLocal() as s:
        s.add(_op(pid, board_id, base, status="bogus"))
        with pytest.raises(IntegrityError):
            s.commit()


# --- idempotency -----------------------------------------------------------


def test_idempotency_key_is_unique_per_board():
    pid, board_id, base = _board("t367-idem")
    key = "same-key"
    with SessionLocal() as s:
        s.add(_op(pid, board_id, base, idempotency_key=key))
        s.commit()
    with SessionLocal() as s:
        s.add(_op(pid, board_id, base, idempotency_key=key))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_same_key_may_be_used_on_a_different_board():
    pid_a, board_a, base_a = _board("t367-a")
    pid_b, board_b, base_b = _board("t367-b")
    with SessionLocal() as s:
        s.add(_op(pid_a, board_a, base_a, idempotency_key="shared"))
        s.add(_op(pid_b, board_b, base_b, idempotency_key="shared"))
        s.commit()  # scoped per board, so this is legitimate


# --- referential invariants ------------------------------------------------


def test_base_version_must_belong_to_the_board():
    pid, _board_id_a, _ = _board("t367-base-a")
    pid2, board_b, base_b = _board("t367-base-b")
    with SessionLocal() as s:
        # A base that belongs to another board must be refused at INSERT.
        s.add(_op(pid, board_b, base_b, idempotency_key=new_id()))
        s.commit()  # sanity: same board is fine

    pid3, board_c, _base_c = _board("t367-base-c")
    with SessionLocal() as s:
        s.add(_op(pid3, board_c, base_b, idempotency_key=new_id()))
        with pytest.raises(IntegrityError):
            s.commit()


def test_applied_requires_a_result_version_in_the_same_board():
    pid, board_id, base = _board("t367-applied")
    op = _op(pid, board_id, base)
    with SessionLocal() as s:
        s.add(op)
        s.commit()
        op_id = op.id

    with SessionLocal() as s:
        row = s.get(PlanOperation, op_id)
        row.status = "applied"
        # No result version: refused.
        with pytest.raises(IntegrityError):
            s.commit()


def test_applied_with_a_valid_result_version_is_accepted():
    pid, board_id, base = _board("t367-applied-ok")
    with SessionLocal() as s:
        v2 = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=2, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="manual_edit", parent_version_id=base,
        )
        s.add(v2)
        s.flush()
        op = _op(pid, board_id, base)
        s.add(op)
        s.flush()
        op.status = "applied"
        op.result_version_id = v2.id
        s.commit()  # must not raise


def test_a_terminal_operation_cannot_be_rewritten():
    """A second apply must replay, never overwrite the recorded outcome."""
    pid, board_id, base = _board("t367-terminal")
    with SessionLocal() as s:
        v2 = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=2, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="manual_edit", parent_version_id=base,
        )
        s.add(v2)
        s.flush()
        op = _op(pid, board_id, base)
        s.add(op)
        s.flush()
        op.status = "applied"
        op.result_version_id = v2.id
        s.commit()
        op_id = op.id

    with SessionLocal() as s:
        row = s.get(PlanOperation, op_id)
        row.status = "proposed"
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_rejected_operation_is_also_terminal():
    pid, board_id, base = _board("t367-rejected")
    with SessionLocal() as s:
        op = _op(pid, board_id, base)
        s.add(op)
        s.flush()
        op.status = "rejected"
        s.commit()
        op_id = op.id

    with SessionLocal() as s:
        row = s.get(PlanOperation, op_id)
        row.status = "applied"
        with pytest.raises(IntegrityError):
            s.commit()


def test_only_one_applied_operation_per_result_version():
    """Two different operations must not both claim the same produced version."""
    pid, board_id, base = _board("t367-double-claim")
    with SessionLocal() as s:
        v2 = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=2, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={"state": "ok", "issues": []},
            actor="user", reason="manual_edit", parent_version_id=base,
        )
        s.add(v2)
        s.flush()
        for _ in range(2):
            op = _op(pid, board_id, base, idempotency_key=new_id())
            s.add(op)
            s.flush()
            op.status = "applied"
            op.result_version_id = v2.id
        with pytest.raises(IntegrityError):
            s.commit()


# --- lock dimensions (§5.3 preconditions) ---------------------------------


def test_lock_dimensions_validate_and_normalise():
    from app.services.storyboard_schema import LOCK_DIMENSIONS, ShotSnapshot

    shot = ShotSnapshot(
        shot_id="s1", asset_id="a", segment_id="g",
        source_in_sec=0.0, source_out_sec=1.0, locked=["position", "position", "range"],
    )
    assert shot.locked == ["position", "range"], "dimensions must be deduplicated"

    with pytest.raises(Exception):
        ShotSnapshot(shot_id="s1", asset_id="a", segment_id="g",
                     source_in_sec=0.0, source_out_sec=1.0, locked=["nonsense"])
    assert set(LOCK_DIMENSIONS) == {"reference", "range", "position", "all"}


def test_all_expands_to_every_dimension():
    from app.services.storyboard_schema import ShotSnapshot

    shot = ShotSnapshot(shot_id="s1", asset_id="a", segment_id="g",
                        source_in_sec=0.0, source_out_sec=1.0, locked=["all"])
    assert shot.lock_set() == frozenset({"reference", "range", "position"})

    unlocked = ShotSnapshot(shot_id="s1", asset_id="a", segment_id="g",
                            source_in_sec=0.0, source_out_sec=1.0, locked=None)
    assert unlocked.lock_set() == frozenset()


def test_legacy_boolean_lock_still_reads_back():
    """Snapshots written before TSB-367 stored a bool; they must stay readable."""
    from app.services.storyboard_schema import ShotSnapshot

    shot = ShotSnapshot.model_validate({
        "shot_id": "s1", "asset_id": "a", "segment_id": "g",
        "source_in_sec": 0.0, "source_out_sec": 1.0, "locked": True,
    })
    assert shot.lock_set() == frozenset({"reference", "range", "position"})
