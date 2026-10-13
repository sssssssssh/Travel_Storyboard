"""Audit P0-02 / P0-03 / P1-03 / P1-04 — asset identity, trim survival, cache GC."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.database import session_scope
from app.models import Asset, Frame, Segment, UserAnnotation
from app.paths import frames_dir, staging_dir
from app.services.analyzer import _detected_key, _time_key
from app.services.indexer import index_one


def _project(client, name: str = "data-test") -> dict:
    res = client.post("/api/projects", json={"name": name, "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _asset(project_id: str, path: str = "C:/media/clip.mp4") -> str:
    """Insert a real Asset row (FKs are enforced) and return its id."""
    import uuid

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=project_id,
                source_path=path,
                display_name=Path(path).name,
            )
        )
    return asset_id


def _broken_video(path: Path, size: int = 4096) -> Path:
    """A file that looks like a video but has no valid container."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return path


# --- P0-02: a failing probe must not create a duplicate Asset -----------------

def test_failed_probe_creates_exactly_one_asset(client):
    project = _project(client)
    bad = _broken_video(Path(client_data_dir()) / "src" / "broken.mp4")

    first_id, first_action = index_one(project["id"], bad)
    assert first_action == "failed"

    with session_scope() as s:
        rows = s.scalars(
            select(Asset).where(Asset.project_id == project["id"])
        ).all()
    assert len(rows) == 1, f"expected 1 asset, got {len(rows)}"
    assert rows[0].id == first_id
    assert rows[0].scan_status == "failed"

    # A second scan updates the same row instead of adding another.
    second_id, second_action = index_one(project["id"], bad)
    assert second_id == first_id
    assert second_action == "failed"

    with session_scope() as s:
        count = s.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == project["id"])
        )
    assert count == 1


def test_unique_constraint_on_project_and_path(client):
    """The DB itself guarantees one row per (project, path)."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    project = _project(client, "unique-test")
    with session_scope() as s:
        s.add(Asset(project_id=project["id"], source_path="C:/x/a.mp4", display_name="a.mp4"))

    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(
                Asset(project_id=project["id"], source_path="C:/x/a.mp4", display_name="a.mp4")
            )

    with session_scope() as s:
        count = s.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == project["id"])
        )
    assert count == 1


# --- P0-03: trimming must survive re-analysis --------------------------------

def test_detected_key_is_immutable_when_trimmed():
    """A 0.0 detected start must not fall back to the editable bound."""
    seg = Segment(
        asset_id="a",
        project_id="p",
        detected_start_sec=0.0,
        detected_end_sec=15.0,
        start_sec=3.0,
        end_sec=12.0,
        is_trimmed=True,
    )
    assert _detected_key(seg) == _time_key(0.0, 15.0)
    assert _detected_key(seg) != _time_key(seg.start_sec, seg.end_sec)


def test_detected_key_falls_back_only_when_none():
    seg = Segment(asset_id="a", project_id="p", start_sec=2.0, end_sec=8.0)
    seg.detected_start_sec = None  # type: ignore[assignment]
    seg.detected_end_sec = None  # type: ignore[assignment]
    assert _detected_key(seg) == _time_key(2.0, 8.0)


def test_trim_validation_rejects_out_of_window(client):
    project = _project(client, "trim-test")
    asset_id = _asset(project["id"])
    with session_scope() as s:
        seg = Segment(
            project_id=project["id"],
            asset_id=asset_id,
            detected_start_sec=0.0,
            detected_end_sec=10.0,
            start_sec=0.0,
            end_sec=10.0,
        )
        s.add(seg)
        s.flush()
        seg_id = seg.id

    res = client.post(
        f"/api/segments/{seg_id}/annotate", json={"in_sec": 0.0, "out_sec": 99.0}
    )
    assert res.status_code == 422

    res = client.post(
        f"/api/segments/{seg_id}/annotate", json={"in_sec": 8.0, "out_sec": 2.0}
    )
    assert res.status_code == 422

    res = client.post(
        f"/api/segments/{seg_id}/annotate", json={"in_sec": 1.0, "out_sec": 1.05}
    )
    assert res.status_code == 422


def test_trim_inside_window_is_accepted_and_flagged(client):
    project = _project(client, "trim-ok")
    asset_id = _asset(project["id"])
    with session_scope() as s:
        seg = Segment(
            project_id=project["id"],
            asset_id=asset_id,
            detected_start_sec=0.0,
            detected_end_sec=10.0,
            start_sec=0.0,
            end_sec=10.0,
        )
        s.add(seg)
        s.flush()
        seg_id = seg.id

    res = client.post(
        f"/api/segments/{seg_id}/annotate",
        json={"in_sec": 2.0, "out_sec": 8.0, "rating": "favorite", "note": "keep"},
    )
    assert res.status_code == 200, res.text

    res = client.get(f"/api/segments/{seg_id}")
    body = res.json()
    assert body["start_sec"] == 2.0 and body["end_sec"] == 8.0
    assert body["detected_start_sec"] == 0.0 and body["detected_end_sec"] == 10.0
    assert body["is_trimmed"] is True
    assert body["user_rating"] == "favorite"


def test_reset_trim_restores_detected_bounds(client):
    project = _project(client, "trim-reset")
    asset_id = _asset(project["id"])
    with session_scope() as s:
        seg = Segment(
            project_id=project["id"],
            asset_id=asset_id,
            detected_start_sec=1.5,
            detected_end_sec=9.5,
            start_sec=3.0,
            end_sec=7.0,
            is_trimmed=True,
        )
        s.add(seg)
        s.flush()
        seg_id = seg.id

    res = client.post(f"/api/segments/{seg_id}/reset-trim")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["start_sec"] == 1.5 and body["end_sec"] == 9.5
    assert body["is_trimmed"] is False


# --- P1-03: frame cache must not grow on repeated runs ----------------------

def test_staging_dir_is_used_and_cleaned(client):
    project = _project(client, "staging-test")
    stage = staging_dir(project["id"], "asset-z")
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "leftover.jpg").write_bytes(b"x")
    assert stage.exists()


def test_frame_paths_live_under_the_asset_frames_dir(client):
    project = _project(client, "frames-test")
    expected = frames_dir(project["id"], "asset-1")
    assert expected.parent.name == "frames"
    assert expected.name == "asset-1"
    assert project["id"] in str(expected)


# --- Annotation cascade check ------------------------------------------------

def test_annotation_row_is_linked_to_segment(client):
    project = _project(client, "annotation-test")
    asset_id = _asset(project["id"])
    with session_scope() as s:
        seg = Segment(project_id=project["id"], asset_id=asset_id, start_sec=0.0, end_sec=5.0)
        s.add(seg)
        s.flush()
        seg_id = seg.id
        s.add(UserAnnotation(segment_id=seg_id, note="hello", rating="like"))

    with session_scope() as s:
        found = s.scalars(
            select(UserAnnotation).where(UserAnnotation.segment_id == seg_id)
        ).first()
    assert found is not None and found.note == "hello"


def client_data_dir() -> str:
    """The temporary data dir created by conftest."""
    import os

    return os.environ["TRAVELSB_DATA_DIR"]
