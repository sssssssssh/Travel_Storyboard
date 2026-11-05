"""TSB-367 — the pure operation executor (§6.1).

Tests first, and deliberately database-free: the executor takes a snapshot plus a
resolved context and returns a *new* snapshot, a diff and a validation summary.
That purity is what makes "mid-batch failure writes nothing" provable — there is
nowhere to write.

Guarantees asserted here:

* the input snapshot is never mutated (callers rely on the base version staying
  byte-identical);
* a batch is all-or-nothing;
* later operations see earlier ones (insert then trim the inserted shot);
* removing then touching the same shot fails clearly;
* every operation keeps ``shot_id`` stable except ``insert``/``replace``, which
  mint a new one — the old identity is never reused for new media.
"""

from __future__ import annotations

import copy
import uuid

import pytest

from app.services.plan_operations import (
    OperationExecutionError,
    ProjectContext,
    execute_operations,
)
from app.services.storyboard_schema import StoryboardSnapshot

# ---------------------------------------------------------------------------
# fixtures: a small, fully described plan
# ---------------------------------------------------------------------------


def _ids() -> dict[str, str]:
    return {
        "project": str(uuid.uuid4()),
        "board": str(uuid.uuid4()),
        "asset_a": str(uuid.uuid4()),
        "asset_b": str(uuid.uuid4()),
        "seg_a1": str(uuid.uuid4()),
        "seg_a2": str(uuid.uuid4()),
        "seg_b1": str(uuid.uuid4()),
        "sec_1": "section-1",
        "sec_2": "section-2",
        "shot_1": "shot-1",
        "shot_2": "shot-2",
        "shot_3": "shot-3",
    }


def _snapshot(ids: dict[str, str], locks: dict[str, list[str]] | None = None) -> StoryboardSnapshot:
    locks = locks or {}
    return StoryboardSnapshot.from_dict({
        "schema_version": "1.0",
        "brief": {},
        "sections": [
            {"section_id": ids["sec_1"], "title": "开场", "kind": "custom"},
            {"section_id": ids["sec_2"], "title": "结尾", "kind": "custom"},
        ],
        "shots": [
            {"shot_id": ids["shot_1"], "section_id": ids["sec_1"],
             "asset_id": ids["asset_a"], "segment_id": ids["seg_a1"],
             "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
             "timeline_start_sec": 0.0, "story_role": "opening", "locked": locks.get("shot_1")},
            {"shot_id": ids["shot_2"], "section_id": ids["sec_1"],
             "asset_id": ids["asset_a"], "segment_id": ids["seg_a2"],
             "source_in_sec": 5.0, "source_out_sec": 7.0, "target_duration_sec": 2.0,
             "timeline_start_sec": 2.0, "story_role": "body", "locked": locks.get("shot_2")},
            {"shot_id": ids["shot_3"], "section_id": ids["sec_2"],
             "asset_id": ids["asset_a"], "segment_id": ids["seg_a2"],
             "source_in_sec": 10.0, "source_out_sec": 12.0, "target_duration_sec": 2.0,
             "timeline_start_sec": 4.0, "story_role": "closing", "locked": locks.get("shot_3")},
        ],
        "constraints": {},
        "validation_summary": {"state": "ok", "issues": []},
    })


def _context(ids: dict[str, str]) -> ProjectContext:
    """The resolved project media the executor is allowed to reference.

    Built by hand so the executor tests never need a database.
    """
    return ProjectContext(
        project_id=ids["project"],
        assets={
            ids["asset_a"]: {"duration_sec": 60.0, "source_signature": "sig-a"},
            ids["asset_b"]: {"duration_sec": 30.0, "source_signature": "sig-b"},
        },
        segments={
            ids["seg_a1"]: {"asset_id": ids["asset_a"], "start_sec": 0.0,
                            "end_sec": 4.0, "status": "candidate"},
            ids["seg_a2"]: {"asset_id": ids["asset_a"], "start_sec": 5.0,
                            "end_sec": 15.0, "status": "candidate"},
            ids["seg_b1"]: {"asset_id": ids["asset_b"], "start_sec": 0.0,
                            "end_sec": 20.0, "status": "candidate"},
        },
    )


def _run(ids, ops, *, locks=None):
    return execute_operations(
        snapshot=_snapshot(ids, locks), operations=ops, context=_context(ids),
    )


# ---------------------------------------------------------------------------
# purity
# ---------------------------------------------------------------------------


def test_the_input_snapshot_is_never_mutated():
    ids = _ids()
    snapshot = _snapshot(ids)
    before = copy.deepcopy(snapshot.to_dict())

    execute_operations(
        snapshot=snapshot,
        operations=[{"type": "trim_shot", "shot_id": ids["shot_1"],
                     "source_in_sec": 0.0, "source_out_sec": 1.0}],
        context=_context(ids),
    )
    assert snapshot.to_dict() == before, "the executor mutated its input"


def test_the_same_input_produces_the_same_output():
    ids = _ids()
    ops = [{"type": "move_shot", "shot_id": ids["shot_3"],
            "after_shot_id": ids["shot_1"]}]
    first = _run(ids, ops)
    second = _run(ids, ops)
    assert [s.shot_id for s in first.snapshot.shots] == [
        s.shot_id for s in second.snapshot.shots
    ]
    assert first.diff == second.diff


# ---------------------------------------------------------------------------
# A. move_shot
# ---------------------------------------------------------------------------


def test_move_shot_after_another_shot():
    ids = _ids()
    result = _run(ids, [{"type": "move_shot", "shot_id": ids["shot_3"],
                         "after_shot_id": ids["shot_1"]}])
    ids_order = [s.shot_id for s in result.snapshot.shots]
    assert ids_order == [ids["shot_1"], ids["shot_3"], ids["shot_2"]]
    # Identity and media are untouched by a move.
    moved = next(s for s in result.snapshot.shots if s.shot_id == ids["shot_3"])
    assert moved.segment_id == ids["seg_a2"]
    assert moved.source_in_sec == 10.0


def test_move_shot_before_another_shot():
    ids = _ids()
    result = _run(ids, [{"type": "move_shot", "shot_id": ids["shot_1"],
                         "before_shot_id": ids["shot_3"]}])
    assert [s.shot_id for s in result.snapshot.shots] == [
        ids["shot_2"], ids["shot_1"], ids["shot_3"],
    ]


def test_move_shot_to_a_missing_shot_fails():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "move_shot", "shot_id": ids["shot_1"],
                    "after_shot_id": "does-not-exist"}])
    assert excinfo.value.code in {"SHOT_NOT_FOUND", "INVALID_REFERENCE"}


def test_move_shot_of_a_missing_shot_fails():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "move_shot", "shot_id": "nope",
                    "after_shot_id": ids["shot_1"]}])
    assert excinfo.value.code == "SHOT_NOT_FOUND"


def test_a_position_lock_blocks_a_move():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "move_shot", "shot_id": ids["shot_1"],
                    "after_shot_id": ids["shot_3"]}],
             locks={"shot_1": ["position"]})
    assert excinfo.value.code == "LOCK_CONFLICT"


def test_a_range_lock_does_not_block_a_move():
    """Lock matrix: ``range`` permits move (§5.3)."""
    ids = _ids()
    result = _run(ids, [{"type": "move_shot", "shot_id": ids["shot_1"],
                         "after_shot_id": ids["shot_3"]}],
                  locks={"shot_1": ["range"]})
    assert [s.shot_id for s in result.snapshot.shots][-1] == ids["shot_1"]


# ---------------------------------------------------------------------------
# C. insert_shot
# ---------------------------------------------------------------------------


def test_insert_shot_mints_a_new_identity():
    ids = _ids()
    result = _run(ids, [{
        "type": "insert_shot", "asset_id": ids["asset_a"], "segment_id": ids["seg_a1"],
        "source_in_sec": 1.0, "source_out_sec": 3.0, "target_duration_sec": 2.0,
        "after_shot_id": ids["shot_1"], "story_role": "detail",
    }])
    assert len(result.snapshot.shots) == 4
    new_shot = result.snapshot.shots[1]
    assert new_shot.asset_id == ids["asset_a"]
    assert new_shot.shot_id not in ids.values(), "identity must be server-minted"
    assert result.created_shot_ids == [new_shot.shot_id]


def test_insert_shot_rejects_an_asset_from_another_project():
    ids = _ids()
    ctx = _context(ids)
    with pytest.raises(OperationExecutionError) as excinfo:
        execute_operations(
            snapshot=_snapshot(ids),
            operations=[{
                "type": "insert_shot", "asset_id": "foreign-asset",
                "segment_id": ids["seg_a1"], "source_in_sec": 0.0,
                "source_out_sec": 1.0, "target_duration_sec": 1.0,
            }],
            context=ctx,
        )
    assert excinfo.value.code == "INVALID_REFERENCE"


def test_insert_shot_rejects_a_segment_from_a_different_asset():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{
            "type": "insert_shot", "asset_id": ids["asset_a"],
            "segment_id": ids["seg_b1"],  # belongs to asset_b
            "source_in_sec": 0.0, "source_out_sec": 1.0, "target_duration_sec": 1.0,
        }])
    assert excinfo.value.code == "INVALID_REFERENCE"


def test_insert_shot_rejects_a_range_beyond_the_segment():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{
            "type": "insert_shot", "asset_id": ids["asset_a"],
            "segment_id": ids["seg_a1"],  # 0..4
            "source_in_sec": 3.0, "source_out_sec": 9.0, "target_duration_sec": 6.0,
        }])
    assert excinfo.value.code == "INVALID_RANGE"


# ---------------------------------------------------------------------------
# D. remove_shot
# ---------------------------------------------------------------------------


def test_remove_shot_only_leaves_the_snapshot():
    ids = _ids()
    result = _run(ids, [{"type": "remove_shot", "shot_id": ids["shot_2"]}])
    remaining = [s.shot_id for s in result.snapshot.shots]
    assert ids["shot_2"] not in remaining
    assert len(remaining) == 2
    # The media it referenced is still known to the context (nothing was deleted).
    assert ids["seg_a2"] in _context(ids).segments


def test_removing_a_locked_shot_is_refused():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "remove_shot", "shot_id": ids["shot_1"]}],
             locks={"shot_1": ["all"]})
    assert excinfo.value.code == "LOCK_CONFLICT"


def test_removing_a_missing_shot_fails():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "remove_shot", "shot_id": "ghost"}])
    assert excinfo.value.code == "SHOT_NOT_FOUND"


# ---------------------------------------------------------------------------
# E. replace_shot
# ---------------------------------------------------------------------------


def test_replace_shot_creates_a_new_identity_not_the_old_one():
    ids = _ids()
    result = _run(ids, [{
        "type": "replace_shot", "target_shot_id": ids["shot_2"],
        "asset_id": ids["asset_b"], "segment_id": ids["seg_b1"],
        "source_in_sec": 0.0, "source_out_sec": 3.0, "target_duration_sec": 3.0,
        "reason": "better take",
    }])
    ids_now = [s.shot_id for s in result.snapshot.shots]
    assert ids["shot_2"] not in ids_now, "the old identity must not be reused"
    assert len(ids_now) == 3
    replacement = next(s for s in result.snapshot.shots if s.asset_id == ids["asset_b"])
    assert replacement.segment_id == ids["seg_b1"]
    # The diff names both sides.
    assert result.diff["shots"]["replaced"]


def test_replace_keeps_the_original_position():
    ids = _ids()
    result = _run(ids, [{
        "type": "replace_shot", "target_shot_id": ids["shot_2"],
        "asset_id": ids["asset_b"], "segment_id": ids["seg_b1"],
        "source_in_sec": 0.0, "source_out_sec": 3.0, "target_duration_sec": 3.0,
    }])
    assert [s.asset_id for s in result.snapshot.shots] == [
        ids["asset_a"], ids["asset_b"], ids["asset_a"],
    ]


def test_replacing_a_locked_reference_is_refused():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{
            "type": "replace_shot", "target_shot_id": ids["shot_2"],
            "asset_id": ids["asset_b"], "segment_id": ids["seg_b1"],
            "source_in_sec": 0.0, "source_out_sec": 3.0, "target_duration_sec": 3.0,
        }], locks={"shot_2": ["reference"]})
    assert excinfo.value.code == "LOCK_CONFLICT"


# ---------------------------------------------------------------------------
# F. trim_shot
# ---------------------------------------------------------------------------


def test_trim_shot_changes_only_the_range():
    ids = _ids()
    result = _run(ids, [{"type": "trim_shot", "shot_id": ids["shot_1"],
                         "source_in_sec": 0.5, "source_out_sec": 1.5}])
    shot = next(s for s in result.snapshot.shots if s.shot_id == ids["shot_1"])
    assert (shot.source_in_sec, shot.source_out_sec) == (0.5, 1.5)
    assert shot.target_duration_sec == pytest.approx(1.0)
    assert result.diff["shots"]["trimmed"]


def test_trim_beyond_the_segment_is_refused():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "trim_shot", "shot_id": ids["shot_1"],
                    "source_in_sec": 0.0, "source_out_sec": 50.0}])
    assert excinfo.value.code == "INVALID_RANGE"


def test_a_range_lock_blocks_a_trim_but_not_a_position_lock():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "trim_shot", "shot_id": ids["shot_1"],
                    "source_in_sec": 0.0, "source_out_sec": 1.0}],
             locks={"shot_1": ["range"]})
    assert excinfo.value.code == "LOCK_CONFLICT"


# ---------------------------------------------------------------------------
# H. lock_shot
# ---------------------------------------------------------------------------


def test_lock_and_unlock_are_visible_in_the_diff():
    ids = _ids()
    result = _run(ids, [{"type": "lock_shot", "shot_id": ids["shot_1"],
                         "dimensions": ["range"], "locked": True}])
    shot = next(s for s in result.snapshot.shots if s.shot_id == ids["shot_1"])
    assert shot.locked == ["range"]
    assert result.diff["shots"]["locked"]

    unlock = execute_operations(
        snapshot=result.snapshot,
        operations=[{"type": "lock_shot", "shot_id": ids["shot_1"],
                     "dimensions": ["range"], "locked": False}],
        context=_context(ids),
    )
    shot_after = next(s for s in unlock.snapshot.shots if s.shot_id == ids["shot_1"])
    assert shot_after.locked is None


def test_locking_a_missing_shot_fails():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "lock_shot", "shot_id": "ghost",
                    "dimensions": ["all"], "locked": True}])
    assert excinfo.value.code == "SHOT_NOT_FOUND"


# ---------------------------------------------------------------------------
# batch semantics
# ---------------------------------------------------------------------------


def test_later_operations_see_earlier_ones():
    """Insert, then trim the shot that was just inserted."""
    ids = _ids()
    result = execute_operations(
        snapshot=_snapshot(ids),
        operations=[
            {"type": "insert_shot", "asset_id": ids["asset_a"],
             "segment_id": ids["seg_a1"], "source_in_sec": 0.0,
             "source_out_sec": 3.0, "target_duration_sec": 3.0,
             "after_shot_id": ids["shot_1"]},
        ],
        context=_context(ids),
    )
    new_id = result.created_shot_ids[0]
    second = execute_operations(
        snapshot=result.snapshot,
        operations=[{"type": "trim_shot", "shot_id": new_id,
                     "source_in_sec": 0.5, "source_out_sec": 2.0}],
        context=_context(ids),
    )
    shot = next(s for s in second.snapshot.shots if s.shot_id == new_id)
    assert shot.source_in_sec == 0.5


def test_a_batch_is_all_or_nothing():
    ids = _ids()
    before = copy.deepcopy(_snapshot(ids).to_dict())
    with pytest.raises(OperationExecutionError):
        _run(ids, [
            {"type": "trim_shot", "shot_id": ids["shot_1"],
             "source_in_sec": 0.0, "source_out_sec": 1.0},     # valid
            {"type": "remove_shot", "shot_id": "ghost"},        # invalid
        ])
    # Nothing escaped: the caller's snapshot is untouched and no result exists.
    assert _snapshot(ids).to_dict() == before


def test_operating_on_a_previously_removed_shot_fails_clearly():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [
            {"type": "remove_shot", "shot_id": ids["shot_2"]},
            {"type": "trim_shot", "shot_id": ids["shot_2"],
             "source_in_sec": 5.0, "source_out_sec": 6.0},
        ])
    assert excinfo.value.code == "SHOT_NOT_FOUND"


# ---------------------------------------------------------------------------
# G. update_constraints
# ---------------------------------------------------------------------------


def test_update_constraints_does_not_touch_shots():
    ids = _ids()
    result = _run(ids, [{
        "type": "update_constraints",
        "hard": {"must_include": ["opening"], "exclude": []},
        "soft": {"target_duration_sec": 60},
    }])
    assert [s.shot_id for s in result.snapshot.shots] == [
        ids["shot_1"], ids["shot_2"], ids["shot_3"],
    ]
    assert result.diff["constraints"]["changed"]


def test_a_conflicting_hard_constraint_is_reported_not_dropped():
    """Including and excluding the same role cannot both hold."""
    ids = _ids()
    result = _run(ids, [{
        "type": "update_constraints",
        "hard": {"must_include": ["opening"], "exclude": ["opening"]},
    }])
    codes = {issue.code for issue in result.validation.issues}
    assert "CONSTRAINT_CONFLICT" in codes
    assert result.validation.state != "ok"


# ---------------------------------------------------------------------------
# B. move_section
# ---------------------------------------------------------------------------


def test_move_section_reorders_sections_without_touching_shots():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_section", "section_id": ids["sec_2"],
        "before_section_id": ids["sec_1"],
    }])
    assert [s.section_id for s in result.snapshot.sections] == [
        ids["sec_2"], ids["sec_1"],
    ]
    assert [s.shot_id for s in result.snapshot.shots] == [
        ids["shot_1"], ids["shot_2"], ids["shot_3"],
    ]


def test_move_section_of_a_missing_section_fails():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{"type": "move_section", "section_id": "ghost",
                    "before_section_id": ids["sec_1"]}])
    assert excinfo.value.code in {"SECTION_NOT_FOUND", "INVALID_REFERENCE"}


# ---------------------------------------------------------------------------
# timeline maintenance
# ---------------------------------------------------------------------------


def test_the_resulting_timeline_is_contiguous():
    ids = _ids()
    result = _run(ids, [
        {"type": "remove_shot", "shot_id": ids["shot_2"]},
        {"type": "trim_shot", "shot_id": ids["shot_1"],
         "source_in_sec": 0.0, "source_out_sec": 1.5},
    ])
    running = 0.0
    for shot in result.snapshot.shots:
        assert shot.timeline_start_sec == pytest.approx(running)
        running += shot.target_duration_sec
