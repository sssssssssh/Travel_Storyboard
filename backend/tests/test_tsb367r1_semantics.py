"""TSB-367-R1 — move_shot / lock / section semantics (R1-06, R1-07, R1-08).

Tests first. On `eb63003`:

* ``move_shot`` accepts ``section_id`` in the schema but the executor never writes
  it, so a cross-section move silently keeps the old section;
* the executor ``remove``s the shot before looking up the anchor, so using the shot
  itself as anchor raises ``SHOT_NOT_FOUND`` from ``_index_of`` instead of a
  documented outcome;
* ``_apply_lock_shot`` leaves ``["all"]`` in place when unlocking one dimension, so
  the other dimensions become impossible to unlock;
* nothing states whether section order drives shot order.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.plan_operations import (
    OperationExecutionError,
    ProjectContext,
    execute_operations,
)
from app.services.storyboard_schema import StoryboardSnapshot


def _ids() -> dict[str, str]:
    return {
        "project": str(uuid.uuid4()),
        "asset": str(uuid.uuid4()),
        "seg1": str(uuid.uuid4()),
        "seg2": str(uuid.uuid4()),
        "seg3": str(uuid.uuid4()),
        "sec1": "section-1",
        "sec2": "section-2",
        "sec3": "section-3",
    }


def _snapshot(ids: dict[str, str]) -> StoryboardSnapshot:
    return StoryboardSnapshot.from_dict({
        "schema_version": "1.0",
        "sections": [
            {"section_id": ids["sec1"], "title": "开场", "kind": "custom"},
            {"section_id": ids["sec2"], "title": "中段", "kind": "custom"},
            {"section_id": ids["sec3"], "title": "结尾", "kind": "custom"},
        ],
        "shots": [
            {"shot_id": "a", "section_id": ids["sec1"], "asset_id": ids["asset"],
             "segment_id": ids["seg1"], "source_in_sec": 0.0, "source_out_sec": 2.0,
             "target_duration_sec": 2.0, "timeline_start_sec": 0.0},
            {"shot_id": "b", "section_id": ids["sec1"], "asset_id": ids["asset"],
             "segment_id": ids["seg2"], "source_in_sec": 5.0, "source_out_sec": 7.0,
             "target_duration_sec": 2.0, "timeline_start_sec": 2.0},
            {"shot_id": "c", "section_id": ids["sec2"], "asset_id": ids["asset"],
             "segment_id": ids["seg3"], "source_in_sec": 10.0, "source_out_sec": 12.0,
             "target_duration_sec": 2.0, "timeline_start_sec": 4.0},
        ],
        "constraints": {},
        "validation_summary": {"state": "ok", "issues": []},
    })


def _context(ids: dict[str, str]) -> ProjectContext:
    return ProjectContext(
        project_id=ids["project"],
        assets={ids["asset"]: {"duration_sec": 60.0}},
        segments={
            ids["seg1"]: {"asset_id": ids["asset"], "start_sec": 0.0, "end_sec": 4.0},
            ids["seg2"]: {"asset_id": ids["asset"], "start_sec": 5.0, "end_sec": 9.0},
            ids["seg3"]: {"asset_id": ids["asset"], "start_sec": 10.0, "end_sec": 14.0},
        },
    )


def _run(ids, ops, *, snapshot=None):
    return execute_operations(
        snapshot=snapshot or _snapshot(ids), operations=ops, context=_context(ids),
    )


# ---------------------------------------------------------------------------
# R1-06: cross-section move
# ---------------------------------------------------------------------------


def test_r1_06_a_move_with_section_id_updates_the_shot_section():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "a", "after_shot_id": "c",
        "section_id": ids["sec2"],
    }])
    moved = next(s for s in result.snapshot.shots if s.shot_id == "a")
    assert moved.section_id == ids["sec2"], "the executor must write the new section"


def test_r1_06_a_move_without_section_id_keeps_the_original_section():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "a", "after_shot_id": "c",
    }])
    moved = next(s for s in result.snapshot.shots if s.shot_id == "a")
    assert moved.section_id == ids["sec1"]


def test_r1_06_a_move_to_a_nonexistent_section_is_refused():
    ids = _ids()
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{
            "type": "move_shot", "shot_id": "a", "after_shot_id": "c",
            "section_id": "no-such-section",
        }])
    assert excinfo.value.code == "SECTION_NOT_FOUND"


def test_r1_06_the_result_never_contains_a_dangling_section_reference():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "a", "after_shot_id": "c",
        "section_id": ids["sec3"],
    }])
    known = {s.section_id for s in result.snapshot.sections}
    for shot in result.snapshot.shots:
        assert shot.section_id is None or shot.section_id in known


def test_r1_06_a_position_locked_shot_cannot_move_across_sections():
    ids = _ids()
    # `StoryboardSnapshot` is a plain helper class; go through its dict form.
    raw = _snapshot(ids).to_dict()
    for shot in raw["shots"]:
        if shot["shot_id"] == "a":
            shot["locked"] = ["position"]
    snapshot = StoryboardSnapshot.from_dict(raw)
    with pytest.raises(OperationExecutionError) as excinfo:
        _run(ids, [{
            "type": "move_shot", "shot_id": "a", "after_shot_id": "c",
            "section_id": ids["sec2"],
        }], snapshot=snapshot)
    assert excinfo.value.code == "LOCK_CONFLICT"


# ---------------------------------------------------------------------------
# R1-06: self-anchor
# ---------------------------------------------------------------------------


def test_r1_06_after_self_is_a_documented_noop_not_a_crash():
    ids = _ids()
    before = [s.shot_id for s in _snapshot(ids).shots]
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "b", "after_shot_id": "b",
    }])
    assert [s.shot_id for s in result.snapshot.shots] == before


def test_r1_06_before_self_is_a_documented_noop_not_a_crash():
    ids = _ids()
    before = [s.shot_id for s in _snapshot(ids).shots]
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "b", "before_shot_id": "b",
    }])
    assert [s.shot_id for s in result.snapshot.shots] == before


def test_r1_06_self_anchor_never_raises_a_runtime_error():
    """A crash here would be a 500; the contract is an explicit outcome."""
    ids = _ids()
    for op in (
        {"type": "move_shot", "shot_id": "b", "after_shot_id": "b"},
        {"type": "move_shot", "shot_id": "b", "before_shot_id": "b"},
    ):
        try:
            _run(ids, [op])
        except OperationExecutionError:
            pass  # an explicit refusal is acceptable
        except (IndexError, KeyError, StopIteration, ValueError) as exc:
            pytest.fail(f"self-anchor raised {type(exc).__name__}: {exc}")


def test_r1_06_self_anchor_with_a_new_section_still_moves_the_shot():
    """A no-op *position* may still carry a section change — that is intent."""
    ids = _ids()
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "b", "after_shot_id": "b",
        "section_id": ids["sec3"],
    }])
    moved = next(s for s in result.snapshot.shots if s.shot_id == "b")
    assert moved.section_id == ids["sec3"]


# ---------------------------------------------------------------------------
# R1-07: unlocking one dimension out of `all`
# ---------------------------------------------------------------------------


def test_r1_07_unlocking_range_from_all_leaves_reference_and_position():
    ids = _ids()
    locked = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": True, "dimensions": ["all"],
    }])
    shot = next(s for s in locked.snapshot.shots if s.shot_id == "a")
    assert shot.locked == ["all"]

    unlocked = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": False, "dimensions": ["range"],
    }], snapshot=locked.snapshot)
    shot_after = next(s for s in unlocked.snapshot.shots if s.shot_id == "a")
    assert set(shot_after.locked or []) == {"reference", "position"}
    # Canonical form: never keep the `all` shorthand alongside concrete dimensions.
    assert "all" not in (shot_after.locked or [])


def test_r1_07_after_partial_unlock_trim_is_allowed_but_move_and_replace_are_not():
    ids = _ids()
    locked = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": True, "dimensions": ["all"],
    }])
    partial = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": False, "dimensions": ["range"],
    }], snapshot=locked.snapshot)

    # trim is permitted now that `range` is free
    trimmed = execute_operations(
        snapshot=partial.snapshot,
        operations=[{"type": "trim_shot", "shot_id": "a",
                     "source_in_sec": 0.5, "source_out_sec": 1.5}],
        context=_context(ids),
    )
    assert trimmed.snapshot is not None

    # move still blocked (position)
    with pytest.raises(OperationExecutionError) as excinfo:
        execute_operations(
            snapshot=partial.snapshot,
            operations=[{"type": "move_shot", "shot_id": "a",
                         "after_shot_id": "c"}],
            context=_context(ids),
        )
    assert excinfo.value.code == "LOCK_CONFLICT"

    # replace still blocked (reference)
    with pytest.raises(OperationExecutionError) as excinfo:
        execute_operations(
            snapshot=partial.snapshot,
            operations=[{"type": "replace_shot", "target_shot_id": "a",
                         "asset_id": ids["asset"], "segment_id": ids["seg3"],
                         "source_in_sec": 10.0, "source_out_sec": 12.0,
                         "target_duration_sec": 2.0}],
            context=_context(ids),
        )
    assert excinfo.value.code == "LOCK_CONFLICT"


def test_r1_07_unlocking_all_dimensions_one_by_one_reaches_unlocked():
    ids = _ids()
    snapshot = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": True, "dimensions": ["all"],
    }]).snapshot
    for dim in ("reference", "range", "position"):
        snapshot = execute_operations(
            snapshot=snapshot,
            operations=[{"type": "lock_shot", "shot_id": "a",
                         "locked": False, "dimensions": [dim]}],
            context=_context(ids),
        ).snapshot
    shot = next(s for s in snapshot.shots if s.shot_id == "a")
    assert shot.locked is None


def test_r1_07_locked_state_stays_canonical():
    ids = _ids()
    snapshot = _run(ids, [{
        "type": "lock_shot", "shot_id": "a", "locked": True, "dimensions": ["all"],
    }]).snapshot
    snapshot = execute_operations(
        snapshot=snapshot,
        operations=[{"type": "lock_shot", "shot_id": "a", "locked": False,
                     "dimensions": ["range"]}],
        context=_context(ids),
    ).snapshot
    shot = next(s for s in snapshot.shots if s.shot_id == "a")
    assert shot.locked == sorted(shot.locked or []), "dimensions must be sorted"
    assert len(shot.locked or []) == len(set(shot.locked or [])), "must be unique"


# ---------------------------------------------------------------------------
# R1-08: section order is metadata, shot order is the timeline
# ---------------------------------------------------------------------------


def test_r1_08_moving_a_section_does_not_reorder_shots():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_section", "section_id": ids["sec2"],
        "before_section_id": ids["sec1"],
    }])
    assert [s.section_id for s in result.snapshot.sections] == [
        ids["sec2"], ids["sec1"], ids["sec3"],
    ]
    assert [s.shot_id for s in result.snapshot.shots] == ["a", "b", "c"], (
        "section order is metadata; it must not silently reorder the timeline"
    )


def test_r1_08_the_timeline_follows_the_shot_array_not_the_section_order():
    ids = _ids()
    result = _run(ids, [{
        "type": "move_section", "section_id": ids["sec2"],
        "before_section_id": ids["sec1"],
    }])
    running = 0.0
    for shot in result.snapshot.shots:
        assert shot.timeline_start_sec == pytest.approx(running)
        running += shot.target_duration_sec


def test_r1_08_the_contract_is_documented_in_the_executor():
    import inspect

    from app.services import plan_operations

    source = inspect.getsource(plan_operations)
    assert "metadata" in source.lower(), (
        "the section-order contract must be stated in the code, not left implicit"
    )


def test_r1_08_discontiguous_sections_are_reported_for_review():
    """A shot order that interleaves sections is legal but worth flagging."""
    ids = _ids()
    result = _run(ids, [{
        "type": "move_shot", "shot_id": "c", "after_shot_id": "a",
    }])
    # Timeline is now sec1, sec2, sec1 — the sections are no longer contiguous.
    orders = [s.section_id for s in result.snapshot.shots]
    assert orders == [ids["sec1"], ids["sec2"], ids["sec1"]]
    codes = {issue.code for issue in result.validation.issues}
    assert "SECTIONS_DISCONTIGUOUS" in codes
