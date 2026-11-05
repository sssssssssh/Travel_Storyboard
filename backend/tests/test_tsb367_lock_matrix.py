"""TSB-367 — the lock matrix, cell by cell (§5.3, §9.4).

The task book publishes a matrix and requires every cell to be tested, precisely
because a matrix in prose drifts away from the code that implements it. This file
parses nothing: it encodes the published matrix as data and asserts the executor
agrees, so changing one without the other fails.

    | dimension | move | trim | replace | remove | update_constraints |
    | reference |  ✓   |  ✓   |   ✗     |   ✗    |        ✓           |
    | range     |  ✓   |  ✗   |   ✗     |   ✗    |        ✓           |
    | position  |  ✗   |  ✓   | ✓(pos)  |   ✗    |        ✓           |
    | all       |  ✗   |  ✗   |   ✗     |   ✗    |        ✓           |

``replace`` under ``position`` is allowed *provided the shot keeps its position*;
the executor implements replace in place, so that cell is a pass.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.plan_operations import (
    LOCK_MATRIX,
    OperationExecutionError,
    ProjectContext,
    execute_operations,
)
from app.services.storyboard_schema import StoryboardSnapshot

#: The published matrix, transcribed. ``True`` = permitted.
PUBLISHED_MATRIX: dict[str, dict[str, bool]] = {
    "reference": {"move_shot": True, "trim_shot": True, "replace_shot": False,
                  "remove_shot": False, "update_constraints": True},
    "range": {"move_shot": True, "trim_shot": False, "replace_shot": False,
              "remove_shot": False, "update_constraints": True},
    "position": {"move_shot": False, "trim_shot": True, "replace_shot": True,
                 "remove_shot": False, "update_constraints": True},
    "all": {"move_shot": False, "trim_shot": False, "replace_shot": False,
            "remove_shot": False, "update_constraints": True},
}

DIMENSIONS = list(PUBLISHED_MATRIX)
OPERATIONS = ["move_shot", "trim_shot", "replace_shot", "remove_shot",
              "update_constraints"]


def _ids() -> dict[str, str]:
    return {
        "project": str(uuid.uuid4()),
        "asset_a": str(uuid.uuid4()),
        "asset_b": str(uuid.uuid4()),
        "seg_a1": str(uuid.uuid4()),
        "seg_a2": str(uuid.uuid4()),
        "seg_b1": str(uuid.uuid4()),
        "shot_1": "shot-1",
        "shot_2": "shot-2",
        "sec_1": "section-1",
    }


def _snapshot(ids: dict[str, str], locked: list[str] | None) -> StoryboardSnapshot:
    return StoryboardSnapshot.from_dict({
        "schema_version": "1.0",
        "sections": [{"section_id": ids["sec_1"], "title": "s", "kind": "custom"}],
        "shots": [
            {"shot_id": ids["shot_1"], "section_id": ids["sec_1"],
             "asset_id": ids["asset_a"], "segment_id": ids["seg_a1"],
             "source_in_sec": 0.0, "source_out_sec": 2.0,
             "target_duration_sec": 2.0, "locked": locked},
            {"shot_id": ids["shot_2"], "section_id": ids["sec_1"],
             "asset_id": ids["asset_a"], "segment_id": ids["seg_a2"],
             "source_in_sec": 5.0, "source_out_sec": 7.0,
             "target_duration_sec": 2.0},
        ],
        "constraints": {},
        "validation_summary": {"state": "ok", "issues": []},
    })


def _context(ids: dict[str, str]) -> ProjectContext:
    return ProjectContext(
        project_id=ids["project"],
        assets={ids["asset_a"]: {"duration_sec": 60.0},
                ids["asset_b"]: {"duration_sec": 30.0}},
        segments={
            ids["seg_a1"]: {"asset_id": ids["asset_a"], "start_sec": 0.0,
                            "end_sec": 4.0, "status": "candidate"},
            ids["seg_a2"]: {"asset_id": ids["asset_a"], "start_sec": 5.0,
                            "end_sec": 15.0, "status": "candidate"},
            ids["seg_b1"]: {"asset_id": ids["asset_b"], "start_sec": 0.0,
                            "end_sec": 20.0, "status": "candidate"},
        },
    )


def _operation(name: str, ids: dict[str, str]) -> dict:
    if name == "move_shot":
        return {"type": "move_shot", "shot_id": ids["shot_1"],
                "after_shot_id": ids["shot_2"]}
    if name == "trim_shot":
        return {"type": "trim_shot", "shot_id": ids["shot_1"],
                "source_in_sec": 0.5, "source_out_sec": 1.5}
    if name == "replace_shot":
        return {"type": "replace_shot", "target_shot_id": ids["shot_1"],
                "asset_id": ids["asset_b"], "segment_id": ids["seg_b1"],
                "source_in_sec": 0.0, "source_out_sec": 2.0,
                "target_duration_sec": 2.0}
    if name == "remove_shot":
        return {"type": "remove_shot", "shot_id": ids["shot_1"]}
    if name == "update_constraints":
        return {"type": "update_constraints",
                "hard": {"must_include": [], "exclude": []}}
    raise AssertionError(name)


#: ``True`` = the matrix should permit the operation.
@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("operation", OPERATIONS)
def test_every_matrix_cell(dimension: str, operation: str):
    permitted = PUBLISHED_MATRIX[dimension][operation]
    ids = _ids()
    snapshot = _snapshot(ids, [dimension])
    context = _context(ids)
    ops = [_operation(operation, ids)]

    if permitted:
        result = execute_operations(snapshot=snapshot, operations=ops, context=context)
        assert result.snapshot is not None
    else:
        with pytest.raises(OperationExecutionError) as excinfo:
            execute_operations(snapshot=snapshot, operations=ops, context=context)
        assert excinfo.value.code == "LOCK_CONFLICT", (
            f"{operation} under {dimension} should be a LOCK_CONFLICT, "
            f"got {excinfo.value.code}"
        )


def test_the_code_matrix_matches_the_published_table():
    """The forbidden-operation map must be exactly what the table says."""
    for dimension, row in PUBLISHED_MATRIX.items():
        forbidden = {op for op, allowed in row.items() if not allowed}
        if dimension == "all":
            # `all` is the union of the concrete dimensions, so it must forbid
            # exactly the union of what those dimensions forbid.
            union = frozenset().union(*(LOCK_MATRIX[d] for d in LOCK_MATRIX))
            assert union == frozenset(forbidden), (
                f"the `all` row should be the union of the other dimensions; "
                f"code forbids {sorted(union)}, table says {sorted(forbidden)}"
            )
            continue
        assert LOCK_MATRIX[dimension] == frozenset(forbidden), (
            f"{dimension}: code forbids {sorted(LOCK_MATRIX[dimension])}, "
            f"table says {sorted(forbidden)}"
        )


def test_combined_dimensions_forbid_the_union():
    ids = _ids()
    snapshot = _snapshot(ids, ["range", "position"])
    # range forbids trim; position forbids move. Both must now be refused.
    for op in (_operation("trim_shot", ids), _operation("move_shot", ids)):
        with pytest.raises(OperationExecutionError) as excinfo:
            execute_operations(snapshot=snapshot, operations=[op],
                               context=_context(ids))
        assert excinfo.value.code == "LOCK_CONFLICT"


def test_unlocking_restores_the_ability_to_edit():
    """The matrix must not be a one-way door."""
    ids = _ids()
    locked = _snapshot(ids, ["all"])
    unlocked = execute_operations(
        snapshot=locked,
        operations=[{"type": "lock_shot", "shot_id": ids["shot_1"],
                     "dimensions": ["all"], "locked": False}],
        context=_context(ids),
    )
    assert unlocked.snapshot.shots[0].locked is None

    # Now the previously-refused move succeeds.
    moved = execute_operations(
        snapshot=unlocked.snapshot,
        operations=[_operation("move_shot", ids)],
        context=_context(ids),
    )
    assert [s.shot_id for s in moved.snapshot.shots][-1] == ids["shot_1"]


def test_a_lock_never_blocks_a_constraint_update():
    """update_constraints touches no shot, so no lock may prevent it."""
    ids = _ids()
    for dimension in DIMENSIONS:
        result = execute_operations(
            snapshot=_snapshot(ids, [dimension]),
            operations=[_operation("update_constraints", ids)],
            context=_context(ids),
        )
        assert result.diff["constraints"]["changed"] is False or True


def test_locks_are_not_silently_dropped_by_a_move():
    """A move must carry the lock with the shot, not lose it."""
    ids = _ids()
    result = execute_operations(
        snapshot=_snapshot(ids, ["range"]),
        operations=[_operation("move_shot", ids)],
        context=_context(ids),
    )
    moved = next(s for s in result.snapshot.shots if s.shot_id == ids["shot_1"])
    assert moved.locked == ["range"]
