"""TSB-367 — plan operation service: transaction, concurrency, idempotency (§6.2–6.4).

Tests first. These are the guarantees that make the protocol safe to expose:

* an applied operation produces **exactly one** new immutable version;
* a failure moves nothing — no version, no pointer, no ``applied`` row;
* two clients on the same base: at most one wins, the other gets a conflict;
* replaying the same key returns the same operation rather than a second version;
* the same key with a different body is refused;
* a terminal operation is never re-applied or reset.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    Asset,
    PlanOperation,
    Project,
    Segment,
    Storyboard,
    StoryboardVersion,
    new_id,
)

@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _seeded_project(name: str = "p367") -> dict[str, str]:
    """A project with a board, a v1 holding two shots, and known media."""
    ids = {
        "project": new_id(), "asset": new_id(), "asset2": new_id(),
        "seg1": new_id(), "seg2": new_id(), "seg3": new_id(),
    }
    with SessionLocal() as s:
        s.add(Project(id=ids["project"], name=name, source_roots=[],
                      output_dir="", settings={}))
        s.flush()
        s.add(Asset(id=ids["asset"], project_id=ids["project"],
                    source_path=f"/tmp/{name}.mp4", display_name=f"{name}.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                    width=1920, height=1080))
        s.add(Asset(id=ids["asset2"], project_id=ids["project"],
                    source_path=f"/tmp/{name}-b.mp4", display_name=f"{name}-b.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=30.0,
                    width=1920, height=1080))
        s.flush()
        for key, (start, end) in (("seg1", (0.0, 5.0)), ("seg2", (5.0, 10.0)),
                                  ("seg3", (0.0, 8.0))):
            s.add(Segment(id=ids[key], project_id=ids["project"],
                          asset_id=ids["asset"] if key != "seg3" else ids["asset2"],
                          start_sec=start, end_sec=end,
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
    ids["v2"] = view.version_id
    return ids


def _envelope(ids: dict[str, str], operations: list[dict], **kw) -> dict:
    body = {
        "schema_version": 1,
        "base_version_id": kw.pop("base_version_id", ids["v2"]),
        "idempotency_key": kw.pop("idempotency_key", str(uuid.uuid4())),
        "operations": operations,
        "reason": kw.pop("reason", "test"),
    }
    body.update(kw)
    return body


def _trim(ids: dict[str, str]) -> dict:
    return {"type": "trim_shot", "shot_id": "shot-1",
            "source_in_sec": 0.5, "source_out_sec": 1.5}


#: Versions a freshly seeded project already has: v1 (initial, empty) + v2 (two shots).
SEEDED_VERSIONS = 2


def _version_count(pid: str) -> int:
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == pid)).one()
        return len(s.scalars(select(StoryboardVersion).where(
            StoryboardVersion.board_id == board.id)).all())


# ---------------------------------------------------------------------------
# propose → apply happy path
# ---------------------------------------------------------------------------


def test_propose_creates_a_proposed_operation_with_a_server_diff():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded_project("apply-propose")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    assert op.status == "proposed"
    assert op.diff, "the server must compute a preview diff"
    assert op.diff["shots"]["trimmed"]
    # Proposing alone creates no version.
    assert _version_count(ids["project"]) == SEEDED_VERSIONS


def test_apply_creates_exactly_one_new_version_and_marks_applied():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded_project("apply-ok")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    applied = apply_operation(project_id=ids["project"], operation_id=op.id)

    assert applied.status == "applied"
    assert applied.result_version_id
    assert _version_count(ids["project"]) == SEEDED_VERSIONS + 1

    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
        assert board.current_version_id == applied.result_version_id
        version = s.get(StoryboardVersion, applied.result_version_id)
        assert version.parent_version_id == ids["v2"]
        shot = next(s_ for s_ in version.shots if s_["shot_id"] == "shot-1")
        assert shot["source_out_sec"] == 1.5


def test_the_base_version_is_left_byte_identical():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded_project("apply-base-intact")
    with SessionLocal() as s:
        before = s.get(StoryboardVersion, ids["v2"]).shots

    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    apply_operation(project_id=ids["project"], operation_id=op.id)

    with SessionLocal() as s:
        after = s.get(StoryboardVersion, ids["v2"]).shots
    assert after == before


def test_source_refs_are_written_with_the_new_version():
    from app.models import StoryboardSourceRef
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded_project("apply-refs")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    applied = apply_operation(project_id=ids["project"], operation_id=op.id)

    with SessionLocal() as s:
        refs = s.scalars(select(StoryboardSourceRef).where(
            StoryboardSourceRef.version_id == applied.result_version_id)).all()
    assert len(refs) == 2, "the derived index must be written in the same transaction"


# ---------------------------------------------------------------------------
# failure leaves nothing behind
# ---------------------------------------------------------------------------


def test_a_failing_batch_produces_no_version_and_moves_nothing():
    from app.services.plan_operation_service import OperationFailed, propose_operation

    ids = _seeded_project("apply-fail")
    with SessionLocal() as s:
        pointer_before = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one().current_version_id

    with pytest.raises(OperationFailed) as excinfo:
        propose_operation(project_id=ids["project"], envelope=_envelope(ids, [
            _trim(ids),
            {"type": "remove_shot", "shot_id": "ghost"},
        ]))
    assert excinfo.value.code == "SHOT_NOT_FOUND"

    assert _version_count(ids["project"]) == SEEDED_VERSIONS
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
        assert board.current_version_id == pointer_before
        # No operation row was recorded for a batch that never became valid.
        assert not s.scalars(select(PlanOperation).where(
            PlanOperation.board_id == board.id,
            PlanOperation.status == "applied",
        )).all()


def test_applying_an_unknown_operation_raises_not_found():
    from app.services.plan_operation_service import OperationNotFound, apply_operation

    ids = _seeded_project("apply-unknown")
    with pytest.raises(OperationNotFound):
        apply_operation(project_id=ids["project"], operation_id=new_id())


# ---------------------------------------------------------------------------
# concurrency (§6.3)
# ---------------------------------------------------------------------------


def test_a_stale_base_version_is_a_conflict():
    from app.services.plan_operation_service import (
        PlanVersionConflict,
        apply_operation,
        propose_operation,
    )

    ids = _seeded_project("apply-race")
    first = propose_operation(project_id=ids["project"],
                              envelope=_envelope(ids, [_trim(ids)]))
    apply_operation(project_id=ids["project"], operation_id=first.id)

    # The second proposal is built on the *old* base, which is now stale.
    with pytest.raises(PlanVersionConflict):
        propose_operation(project_id=ids["project"],
                          envelope=_envelope(ids, [_trim(ids)]))


def test_two_clients_on_the_same_base_only_one_applies():
    from app.services.plan_operation_service import (
        PlanVersionConflict,
        apply_operation,
        propose_operation,
    )

    ids = _seeded_project("apply-two")
    a = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    b = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))

    # Preconditions the assertion depends on, checked explicitly so a failure
    # says *which* invariant broke instead of only "no conflict raised".
    assert a.base_version_id == b.base_version_id, "both proposals must share a base"
    assert a.base_version_id == ids["v2"], "the base must be the seeded version"

    applied = apply_operation(project_id=ids["project"], operation_id=a.id)
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
    assert board.current_version_id == applied.result_version_id, (
        "applying the winner must advance the pointer before the loser is tried"
    )
    assert board.current_version_id != b.base_version_id, (
        "the loser's base must now be stale"
    )
    with pytest.raises(PlanVersionConflict):
        apply_operation(project_id=ids["project"], operation_id=b.id)

    assert _version_count(ids["project"]) == SEEDED_VERSIONS + 1, "the loser must not add a version"
    with SessionLocal() as s:
        loser = s.get(PlanOperation, b.id)
        assert loser.status in {"stale", "proposed"}


# ---------------------------------------------------------------------------
# idempotency (§6.4)
# ---------------------------------------------------------------------------


def test_replaying_the_same_key_returns_the_same_operation():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded_project("idem-same")
    body = _envelope(ids, [_trim(ids)], idempotency_key="stable-key")
    first = propose_operation(project_id=ids["project"], envelope=body)
    second = propose_operation(project_id=ids["project"], envelope=body)
    assert second.id == first.id
    assert _version_count(ids["project"]) == SEEDED_VERSIONS


def test_the_same_key_with_a_different_body_is_refused():
    from app.services.plan_operation_service import IdempotencyKeyReused, propose_operation

    ids = _seeded_project("idem-diff")
    propose_operation(project_id=ids["project"],
                      envelope=_envelope(ids, [_trim(ids)], idempotency_key="k1"))

    different = _envelope(ids, [
        {"type": "remove_shot", "shot_id": "shot-2"},
    ], idempotency_key="k1")
    with pytest.raises(IdempotencyKeyReused):
        propose_operation(project_id=ids["project"], envelope=different)


def test_applying_twice_returns_the_recorded_outcome():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded_project("idem-apply")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    first = apply_operation(project_id=ids["project"], operation_id=op.id)
    again = apply_operation(project_id=ids["project"], operation_id=op.id)

    assert again.result_version_id == first.result_version_id
    assert _version_count(ids["project"]) == SEEDED_VERSIONS + 1, "a replay created a second version"


# ---------------------------------------------------------------------------
# reject (§7)
# ---------------------------------------------------------------------------


def test_reject_creates_no_version_and_is_idempotent():
    from app.services.plan_operation_service import (
        propose_operation,
        reject_operation,
    )

    ids = _seeded_project("reject")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    rejected = reject_operation(project_id=ids["project"], operation_id=op.id,
                                reason="not now")
    assert rejected.status == "rejected"
    assert rejected.result_version_id is None
    assert _version_count(ids["project"]) == SEEDED_VERSIONS

    again = reject_operation(project_id=ids["project"], operation_id=op.id,
                             reason="not now")
    assert again.status == "rejected"


def test_a_rejected_operation_cannot_be_applied():
    from app.services.plan_operation_service import (
        OperationAlreadyFinal,
        apply_operation,
        propose_operation,
        reject_operation,
    )

    ids = _seeded_project("reject-then-apply")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    reject_operation(project_id=ids["project"], operation_id=op.id, reason="no")

    with pytest.raises(OperationAlreadyFinal):
        apply_operation(project_id=ids["project"], operation_id=op.id)
    assert _version_count(ids["project"]) == SEEDED_VERSIONS


def test_an_applied_operation_cannot_be_rejected():
    from app.services.plan_operation_service import (
        OperationAlreadyFinal,
        apply_operation,
        propose_operation,
        reject_operation,
    )

    ids = _seeded_project("apply-then-reject")
    op = propose_operation(project_id=ids["project"], envelope=_envelope(ids, [_trim(ids)]))
    apply_operation(project_id=ids["project"], operation_id=op.id)

    with pytest.raises(OperationAlreadyFinal):
        reject_operation(project_id=ids["project"], operation_id=op.id, reason="no")


# ---------------------------------------------------------------------------
# cross-project isolation
# ---------------------------------------------------------------------------


def test_an_operation_from_another_project_is_not_found():
    from app.services.plan_operation_service import OperationNotFound, propose_operation

    ids_a = _seeded_project("iso-a")
    ids_b = _seeded_project("iso-b")
    op = propose_operation(project_id=ids_a["project"],
                           envelope=_envelope(ids_a, [_trim(ids_a)]))

    from app.services.plan_operation_service import apply_operation
    with pytest.raises(OperationNotFound):
        apply_operation(project_id=ids_b["project"], operation_id=op.id)


def test_a_base_version_from_another_board_is_refused():
    from app.services.plan_operation_service import (
        PlanVersionConflict,
        propose_operation,
    )

    ids_a = _seeded_project("board-a")
    ids_b = _seeded_project("board-b")
    with pytest.raises(PlanVersionConflict):
        propose_operation(
            project_id=ids_a["project"],
            envelope=_envelope(ids_a, [_trim(ids_a)], base_version_id=ids_b["v2"]),
        )
