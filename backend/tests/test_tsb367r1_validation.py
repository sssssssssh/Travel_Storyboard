"""TSB-367-R1 — the final snapshot must be fully validated (R1-03).

Tests first. On `eb63003` the executor computes an ``ok`` / ``needs_review`` /
``invalid`` state but the **service layer never looks at it**, so an invalid plan
can still become a proposal and a version.

The contract these tests pin:

* every listed reference rule is checked on the **final** snapshot (not just on
  what the batch touched);
* ``invalid`` blocks both proposing and applying;
* ``needs_review`` is preserved (it is a legitimate state to save) and recorded on
  the version;
* nothing is auto-repaired: ids are never substituted, shots are never dropped;
* an already-stored historical version with broken references stays readable.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Asset, PlanOperation, Project, Segment, Storyboard, new_id


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _seeded(name: str, *, with_foreign: bool = False) -> dict[str, str]:
    ids = {"project": new_id(), "asset": new_id(), "seg1": new_id(),
           "seg2": new_id(), "other_project": new_id(), "foreign_asset": new_id(),
           "foreign_seg": new_id()}
    with SessionLocal() as s:
        s.add(Project(id=ids["project"], name=name, source_roots=[],
                      output_dir="", settings={}))
        s.add(Project(id=ids["other_project"], name=f"{name}-other",
                      source_roots=[], output_dir="", settings={}))
        s.flush()
        s.add(Asset(id=ids["asset"], project_id=ids["project"],
                    source_path=f"/tmp/{name}.mp4", display_name=f"{name}.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                    width=1920, height=1080))
        if with_foreign:
            s.add(Asset(id=ids["foreign_asset"], project_id=ids["other_project"],
                        source_path=f"/tmp/{name}-x.mp4", display_name="x.mp4",
                        file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                        width=1920, height=1080))
        s.flush()
        for key, (start, end) in (("seg1", (0.0, 5.0)), ("seg2", (5.0, 10.0))):
            s.add(Segment(id=ids[key], project_id=ids["project"],
                          asset_id=ids["asset"], start_sec=start, end_sec=end,
                          detected_start_sec=start, detected_end_sec=end))
        if with_foreign:
            s.add(Segment(id=ids["foreign_seg"], project_id=ids["other_project"],
                          asset_id=ids["foreign_asset"], start_sec=0.0, end_sec=5.0,
                          detected_start_sec=0.0, detected_end_sec=5.0))
        s.commit()

    from app.services.storyboard_service import get_current_version, save_version

    base = get_current_version(project_id=ids["project"])
    view = save_version(
        project_id=ids["project"], base_version_id=base.version_id,
        shots=[
            {"shot_id": "shot-1", "asset_id": ids["asset"], "segment_id": ids["seg1"],
             "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0},
        ],
    )
    ids["board"] = view.board_id
    ids["version"] = view.version_id
    return ids


def _env(ids: dict[str, str], ops: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "base_version_id": ids["version"],
        "idempotency_key": str(uuid.uuid4()),
        "operations": ops,
        "reason": "r1-03",
    }


def _version_count(pid: str) -> int:
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        from app.models import StoryboardVersion

        return len(s.scalars(select(StoryboardVersion).where(
            StoryboardVersion.board_id == board.id)).all())


# ---------------------------------------------------------------------------
# executor-level: full final validation
# ---------------------------------------------------------------------------


def _context(ids: dict[str, str]) -> "object":
    from app.services.plan_operations import ProjectContext

    with SessionLocal() as s:
        assets = {
            a.id: {"duration_sec": float(a.duration_sec or 0.0), "source_signature": ""}
            for a in s.scalars(select(Asset).where(Asset.project_id == ids["project"])).all()
        }
        segments = {
            g.id: {"asset_id": g.asset_id, "start_sec": float(g.start_sec),
                   "end_sec": float(g.end_sec), "status": getattr(g, "status", "") or ""}
            for g in s.scalars(select(Segment).where(Segment.project_id == ids["project"])).all()
        }
    return ProjectContext(project_id=ids["project"], assets=assets, segments=segments)


def _snapshot_with(shots: list[dict]):
    from app.services.storyboard_schema import StoryboardSnapshot

    return StoryboardSnapshot.from_dict({
        "schema_version": "1.0", "sections": [], "shots": shots,
        "constraints": {}, "validation_summary": {"state": "ok", "issues": []},
    })


def test_r1_03_a_healthy_plan_validates_ok():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-ok")
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": ids["asset"], "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state == "ok"


def test_r1_03_an_asset_that_does_not_exist_is_invalid():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-noasset")
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": new_id(), "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state == "invalid"
    assert any(i.code == "ASSET_MISSING" for i in result.validation.issues)


def test_r1_03_a_segment_that_does_not_exist_is_invalid():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-noseg")
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": ids["asset"], "segment_id": new_id(),
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state == "invalid"
    assert any(i.code == "SEGMENT_MISSING" for i in result.validation.issues)


def test_r1_03_an_asset_segment_mismatch_is_invalid():
    """The segment exists but belongs to a different asset of the same project."""
    from app.services.plan_operations import execute_operations, ProjectContext

    ids = _seeded("r103-mismatch")
    ctx = _context(ids)
    # Point the segment at a second asset in the same project.
    with SessionLocal() as s:
        other = Asset(id=new_id(), project_id=ids["project"],
                      source_path="/tmp/other.mp4", display_name="other.mp4",
                      file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                      width=1920, height=1080)
        s.add(other)
        s.commit()
        other_id = other.id

    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": other_id, "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=ctx)
    assert result.validation.state == "invalid"


def test_r1_03_a_cross_project_reference_is_invalid():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-cross", with_foreign=True)
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": ids["foreign_asset"],
        "segment_id": ids["foreign_seg"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state == "invalid"


def test_r1_03_a_range_outside_the_segment_is_flagged():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-range")
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": ids["asset"], "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 99.0, "target_duration_sec": 99.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state in {"needs_review", "invalid"}
    assert any(i.code in {"OUT_OF_RANGE", "INVALID_RANGE"} for i in result.validation.issues)


def test_r1_03_a_duplicate_shot_id_is_invalid():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-dup")
    shot = {
        "shot_id": "dup", "asset_id": ids["asset"], "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }
    # Constructing through the snapshot parser refuses duplicates, which is the
    # first line of defence; here we prove the *executor* would too if one slipped
    # through by validating a hand-built snapshot.
    from app.services.storyboard_schema import ShotSnapshot, StoryboardSnapshot

    snap = StoryboardSnapshot(shots=[ShotSnapshot(**shot), ShotSnapshot(**shot)])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))
    assert result.validation.state == "invalid"
    assert any(i.code == "DUPLICATE_SHOT_ID" for i in result.validation.issues)


# ---------------------------------------------------------------------------
# service-level: invalid blocks proposal and version
# ---------------------------------------------------------------------------


def _break_base_snapshot(ids: dict[str, str]) -> None:
    """Point the saved version at a segment from *another* project.

    The foreign-key constraint is satisfied (it is a real asset/segment), so this
    is a representable-but-invalid plan — exactly the shape R1-03 is about.
    """
    import json

    from sqlalchemy import text

    # The immutable-version trigger must stay; suspend it only for this setup so a
    # representable-but-invalid historical version can be created.
    with SessionLocal() as s:
        s.execute(text("DROP TRIGGER IF EXISTS trg_storyboard_version_immutable"))
        row = s.execute(
            text("SELECT shots FROM storyboard_versions WHERE id = :v"),
            {"v": ids["version"]},
        ).one()
        shots = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        shots = [dict(shot) for shot in shots]
        for shot in shots:
            shot["asset_id"] = ids["foreign_asset"]
            shot["segment_id"] = ids["foreign_seg"]
        s.execute(
            text("UPDATE storyboard_versions SET shots = :s WHERE id = :v"),
            {"s": json.dumps(shots), "v": ids["version"]},
        )
        s.commit()
    # Recreate it so later tests in this module are still protected.
    from app.database import _create_storyboard_triggers

    _create_storyboard_triggers()


def _break_one_shot(ids: dict[str, str]) -> None:
    """Break a shot the batch will NOT touch, leaving the batch itself valid."""
    import json

    from sqlalchemy import text

    with SessionLocal() as s:
        s.execute(text("DROP TRIGGER IF EXISTS trg_storyboard_version_immutable"))
        row = s.execute(
            text("SELECT shots FROM storyboard_versions WHERE id = :v"),
            {"v": ids["version"]},
        ).one()
        shots = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        shots = [dict(shot) for shot in shots]
        # Add a second shot pointing at another project's segment.
        shots.append({
            "shot_id": "broken-shot",
            "section_id": None,
            "asset_id": ids["asset"],
            "segment_id": ids["foreign_seg"],
            "source_in_sec": 0.0,
            "source_out_sec": 1.0,
            "target_duration_sec": 1.0,
            "timeline_start_sec": 2.0,
        })
        s.execute(
            text("UPDATE storyboard_versions SET shots = :s WHERE id = :v"),
            {"s": json.dumps(shots), "v": ids["version"]},
        )
        s.commit()
    from app.database import _create_storyboard_triggers

    _create_storyboard_triggers()


def test_r1_03_an_invalid_plan_cannot_become_a_proposal():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r103-block-propose", with_foreign=True)
    _break_one_shot(ids)

    with pytest.raises(Exception) as excinfo:
        propose_operation(project_id=ids["project"], envelope=_env(ids, [{
            "type": "trim_shot", "shot_id": "shot-1",
            "source_in_sec": 0.0, "source_out_sec": 1.0,
        }]))
    # The exact code matters: an empty `pytest.raises(Exception)` would pass even
    # if some *other* guard refused first, which would hide a missing invalid gate.
    assert getattr(excinfo.value, "code", "") == "INVALID_SNAPSHOT"


def test_r1_03_an_invalid_final_snapshot_never_becomes_the_current_version():
    from app.services import plan_operation_service as svc

    ids = _seeded("r103-block-version", with_foreign=True)
    op = svc.propose_operation(project_id=ids["project"], envelope=_env(ids, [{
        "type": "trim_shot", "shot_id": "shot-1",
        "source_in_sec": 0.0, "source_out_sec": 1.0,
    }]))
    before = _version_count(ids["project"])
    with SessionLocal() as s:
        pointer_before = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one().current_version_id

    # Break the plan between propose and apply: the proposal was valid, the plan
    # it would publish is not.
    _break_one_shot(ids)

    with pytest.raises(Exception) as excinfo:
        svc.apply_operation(project_id=ids["project"], operation_id=op.id)
    assert getattr(excinfo.value, "code", "") == "INVALID_SNAPSHOT"

    assert _version_count(ids["project"]) == before, "an invalid plan produced a version"
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
        assert board.current_version_id == pointer_before


# ---------------------------------------------------------------------------
# needs_review is preserved, history stays readable
# ---------------------------------------------------------------------------


def test_r1_03_needs_review_is_saved_and_recorded_on_the_version():
    from app.services.plan_operation_service import apply_operation, propose_operation
    from app.models import StoryboardVersion

    ids = _seeded("r103-needsreview")
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, [{
        "type": "trim_shot", "shot_id": "shot-1",
        "source_in_sec": 0.0, "source_out_sec": 1.0,
    }]))

    # Mark the segment as needing review: a legitimate, savable state.
    with SessionLocal() as s:
        seg = s.get(Segment, ids["seg1"])
        seg.status = "needs_review"
        s.commit()

    applied = apply_operation(project_id=ids["project"], operation_id=op.id)
    with SessionLocal() as s:
        version = s.get(StoryboardVersion, applied.result_version_id)
        summary = version.validation_summary
    assert summary["state"] == "needs_review"
    assert any(i["code"] == "SEGMENT_NEEDS_REVIEW" for i in summary["issues"])


def test_r1_03_a_historical_version_with_broken_references_stays_readable():
    from app.services.storyboard_service import get_version, list_versions

    ids = _seeded("r103-history", with_foreign=True)
    _break_base_snapshot(ids)

    # The already-saved version still reads, with its original reference intact.
    versions = list_versions(project_id=ids["project"])
    assert versions
    kept = get_version(board_id=ids["board"], version_id=ids["version"])
    # The stored reference is returned unchanged — not repaired, not dropped.
    assert any(shot["segment_id"] == ids["foreign_seg"] for shot in kept.shots)
    assert len(kept.shots) == 1


def test_r1_03_nothing_is_auto_repaired_or_dropped():
    from app.services.plan_operations import execute_operations

    ids = _seeded("r103-norepair")
    broken_asset = new_id()
    snap = _snapshot_with([{
        "shot_id": "s1", "asset_id": broken_asset, "segment_id": ids["seg1"],
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }])
    result = execute_operations(snapshot=snap, operations=[], context=_context(ids))

    # The shot is still there, still pointing at the broken id: reported, not fixed.
    assert len(result.snapshot.shots) == 1
    assert result.snapshot.shots[0].asset_id == broken_asset
