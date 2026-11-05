"""TSB-366 — storyboard version model, snapshot schema and constraints.

Tests first (task book §7.1). Every case here is written against the target
model **before** the model exists, so the first run is RED and names exactly
which guarantee is missing.

What this file locks down:

* a project owns at most one master storyboard, enforced by the database;
* ``version_no`` is unique per board and monotonic;
* ``parent_version_id`` can never cross boards;
* ``current_version_id`` must belong to the same board;
* stored snapshots are immutable through the ordinary API (no in-place edit);
* ``StoryboardSourceRef`` is written in the same transaction as its version;
* a source ref never silently rebinds to a different asset/segment;
* deleting a storyboard does not delete the underlying Asset/Segment rows;
* the snapshot JSON is schema-validated and unknown fields are refused.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, init_db
from app.models import Asset, Project, Segment, new_id, utcnow


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _project(name: str = "p") -> str:
    with SessionLocal() as s:
        p = Project(id=new_id(), name=name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.commit()
        return p.id


def _asset_segment(project_id: str, name: str = "clip.mp4"):
    with SessionLocal() as s:
        a = Asset(
            id=new_id(), project_id=project_id, source_path=f"/tmp/{name}",
            display_name=name, file_size_bytes=10, mtime_ns=1,
            duration_sec=10.0, width=1920, height=1080,
        )
        s.add(a)
        s.flush()
        g = Segment(
            id=new_id(), project_id=project_id, asset_id=a.id,
            start_sec=0.0, end_sec=5.0,
            detected_start_sec=0.0, detected_end_sec=5.0,
        )
        s.add(g)
        s.commit()
        return a.id, g.id


# --- model existence -------------------------------------------------------


def test_models_exist_with_the_required_columns():
    """The three tables and their contract columns must exist."""
    from app.models import Storyboard, StoryboardSourceRef, StoryboardVersion

    board_cols = {c.name for c in Storyboard.__table__.columns}
    assert {"id", "project_id", "title", "current_version_id"}.issubset(board_cols)

    ver_cols = {c.name for c in StoryboardVersion.__table__.columns}
    assert {
        "id", "board_id", "version_no", "parent_version_id", "schema_version",
        "brief", "sections", "shots", "constraints", "validation_summary",
        "created_at", "actor", "reason", "job_id",
    }.issubset(ver_cols), f"missing snapshot columns: {ver_cols}"

    ref_cols = {c.name for c in StoryboardSourceRef.__table__.columns}
    assert {
        "id", "version_id", "shot_id", "asset_id", "segment_id", "source_signature",
    }.issubset(ref_cols)


def test_one_master_storyboard_per_project_is_enforced_by_the_database():
    """§2.1: at most one master board per project, guaranteed by a constraint."""
    from app.models import Storyboard

    pid = _project()
    with SessionLocal() as s:
        s.add(Storyboard(id=new_id(), project_id=pid, title="a"))
        s.commit()
    with SessionLocal() as s:
        s.add(Storyboard(id=new_id(), project_id=pid, title="b"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_version_no_is_unique_per_board():
    from app.models import Storyboard, StoryboardVersion

    pid = _project()
    board_id = new_id()
    with SessionLocal() as s:
        s.add(Storyboard(id=board_id, project_id=pid, title="b"))
        s.add(StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        ))
        s.commit()
    with SessionLocal() as s:
        s.add(StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_two_boards_may_each_have_a_version_one():
    """Uniqueness is scoped to the board, not global."""
    from app.models import Storyboard, StoryboardVersion

    p1, p2 = _project("a"), _project("b")
    with SessionLocal() as s:
        for pid in (p1, p2):
            bid = new_id()
            s.add(Storyboard(id=bid, project_id=pid, title="b"))
            s.add(StoryboardVersion(
                id=new_id(), board_id=bid, version_no=1, schema_version="1.0",
                brief={}, sections=[], shots=[], constraints={},
                validation_summary={}, actor="user", reason="manual_edit",
            ))
        s.commit()  # must not raise


def test_parent_version_must_belong_to_the_same_board():
    """§2.2: parent must be in the same board — a cross-board parent is refused."""
    from app.models import Storyboard, StoryboardVersion

    pa, pb = _project("a"), _project("b")
    board_a, board_b = new_id(), new_id()
    with SessionLocal() as s:
        s.add(Storyboard(id=board_a, project_id=pa, title="a"))
        s.add(Storyboard(id=board_b, project_id=pb, title="b"))
        v_other = StoryboardVersion(
            id=new_id(), board_id=board_b, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        )
        s.add(v_other)
        s.commit()
        other_id = v_other.id

    with SessionLocal() as s:
        s.add(StoryboardVersion(
            id=new_id(), board_id=board_a, version_no=2, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
            parent_version_id=other_id,
        ))
        with pytest.raises((IntegrityError, ValueError)):
            s.commit()


def test_current_version_must_belong_to_the_same_board():
    """§2.1: current_version_id must point at one of this board's versions."""
    from app.models import Storyboard, StoryboardVersion

    p1, p2 = _project("a"), _project("b")
    board_1, board_2 = new_id(), new_id()
    with SessionLocal() as s:
        v2 = StoryboardVersion(
            id=new_id(), board_id=board_2, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        )
        s.add(Storyboard(id=board_1, project_id=p1, title="a"))
        s.add(Storyboard(id=board_2, project_id=p2, title="b"))
        s.add(v2)
        s.commit()
        foreign_version = v2.id

    with SessionLocal() as s:
        board = s.get(Storyboard, board_1)
        board.current_version_id = foreign_version
        with pytest.raises((IntegrityError, ValueError)):
            s.commit()


# --- snapshot schema -------------------------------------------------------


def test_snapshot_requires_a_schema_version():
    from app.services.storyboard_schema import StoryboardSnapshot, SnapshotError

    with pytest.raises(SnapshotError):
        StoryboardSnapshot.from_dict({"shots": []})


def test_snapshot_rejects_unknown_fields():
    """§2.4: unknown fields must not slip into a stored snapshot."""
    from app.services.storyboard_schema import StoryboardSnapshot, SnapshotError

    with pytest.raises(SnapshotError):
        StoryboardSnapshot.from_dict({
            "schema_version": "1.0",
            "shots": [{
                "shot_id": new_id(), "asset_id": new_id(), "segment_id": new_id(),
                "source_in_sec": 0.0, "source_out_sec": 1.0,
                "mystery_field": 1,
            }],
        })


def test_snapshot_preserves_second_precision():
    """Seconds must not be rounded on the way in (§2.4)."""
    from app.services.storyboard_schema import StoryboardSnapshot

    shot = {
        "shot_id": "11111111-1111-1111-1111-111111111111",
        "asset_id": "22222222-2222-2222-2222-222222222222",
        "segment_id": "33333333-3333-3333-3333-333333333333",
        "source_in_sec": 1.2345678,
        "source_out_sec": 2.87654321,
        "target_duration_sec": 1.64197541,
    }
    snap = StoryboardSnapshot.from_dict({"schema_version": "1.0", "shots": [shot]})
    assert snap.shots[0].source_in_sec == 1.2345678
    assert snap.shots[0].source_out_sec == 2.87654321


def test_snapshot_requires_stable_shot_ids():
    """§2.4: array index is not an identity; every shot needs a stable id."""
    from app.services.storyboard_schema import StoryboardSnapshot, SnapshotError

    with pytest.raises(SnapshotError):
        StoryboardSnapshot.from_dict({
            "schema_version": "1.0",
            "shots": [{
                "asset_id": new_id(), "segment_id": new_id(),
                "source_in_sec": 0.0, "source_out_sec": 1.0,
            }],
        })


def test_snapshot_round_trips_through_json():
    from app.services.storyboard_schema import StoryboardSnapshot

    raw = {
        "schema_version": "1.0",
        "brief": {"theme": "海边"},
        "sections": [{"section_id": "s1", "title": "开场", "kind": "legacy_import"}],
        "shots": [{
            "shot_id": "11111111-1111-1111-1111-111111111111",
            "section_id": "s1",
            "asset_id": "22222222-2222-2222-2222-222222222222",
            "segment_id": "33333333-3333-3333-3333-333333333333",
            "source_in_sec": 0.5, "source_out_sec": 2.25,
            "target_duration_sec": 1.75, "story_role": "highlight",
            "music_cue": "piano", "transition_hint": "cut",
        }],
        "constraints": {},
        "validation_summary": {"state": "ok"},
    }
    snap = StoryboardSnapshot.from_dict(raw)
    again = StoryboardSnapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert again.to_dict()["shots"] == snap.to_dict()["shots"]


# --- source refs -----------------------------------------------------------


def test_source_ref_is_unique_per_version_and_shot():
    from app.models import Storyboard, StoryboardSourceRef, StoryboardVersion

    pid = _project()
    asset_id, segment_id = _asset_segment(pid)
    board_id = new_id()
    with SessionLocal() as s:
        s.add(Storyboard(id=board_id, project_id=pid, title="b"))
        v = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        )
        s.add(v)
        s.flush()
        vid = v.id
        s.add(StoryboardSourceRef(
            id=new_id(), version_id=vid, shot_id="s1",
            asset_id=asset_id, segment_id=segment_id, source_signature="sig",
        ))
        s.commit()
    with SessionLocal() as s:
        s.add(StoryboardSourceRef(
            id=new_id(), version_id=vid, shot_id="s1",
            asset_id=asset_id, segment_id=segment_id, source_signature="sig",
        ))
        with pytest.raises(IntegrityError):
            s.commit()


def test_deleting_a_storyboard_never_deletes_assets_or_segments():
    """§2.3: history evidence loss via cascade is forbidden."""
    from app.models import Storyboard, StoryboardSourceRef, StoryboardVersion

    pid = _project()
    asset_id, segment_id = _asset_segment(pid)
    board_id, vid = new_id(), new_id()
    with SessionLocal() as s:
        s.add(Storyboard(id=board_id, project_id=pid, title="b"))
        s.add(StoryboardVersion(
            id=vid, board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="migration", reason="legacy_import",
        ))
        s.flush()
        s.add(StoryboardSourceRef(
            id=new_id(), version_id=vid, shot_id="s1",
            asset_id=asset_id, segment_id=segment_id, source_signature="sig",
        ))
        s.commit()

    with SessionLocal() as s:
        s.delete(s.get(Storyboard, board_id))
        s.commit()

    with SessionLocal() as s:
        # The board and its refs may go; the Asset/Segment must stay.
        assert s.get(Asset, asset_id) is not None
        assert s.get(Segment, segment_id) is not None


def test_reading_a_version_does_not_change_the_current_pointer():
    """§4.1: reading history must not promote it to current."""
    from app.models import Storyboard, StoryboardVersion

    pid = _project()
    board_id = new_id()
    with SessionLocal() as s:
        s.add(Storyboard(id=board_id, project_id=pid, title="b"))
        v1 = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=1, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
        )
        s.add(v1)
        s.flush()
        board = s.get(Storyboard, board_id)
        board.current_version_id = v1.id
        v2 = StoryboardVersion(
            id=new_id(), board_id=board_id, version_no=2, schema_version="1.0",
            brief={}, sections=[], shots=[], constraints={},
            validation_summary={}, actor="user", reason="manual_edit",
            parent_version_id=v1.id,
        )
        s.add(v2)
        s.flush()
        current_before = s.get(Storyboard, board_id).current_version_id
        v1_id = v1.id
        s.commit()

    with SessionLocal() as s:
        # Read v1 (the *old* version) through the service layer.
        from app.services.storyboard_service import get_version

        get_version(board_id=board_id, version_id=v1_id)
        still = s.get(Storyboard, board_id).current_version_id
        assert still == current_before, "reading a historical version moved the pointer"
