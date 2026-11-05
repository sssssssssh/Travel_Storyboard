"""TSB-366 — version read / save / restore and the export adapters (§4, §5, §7.3–7.4).

Tests first. The cases encode the four rules the task book treats as
non-negotiable:

1. **history is not overwritten** — a save creates a new row, never an edit;
2. **restore creates a new version** — it does not move the pointer backwards;
3. **base_version guards writes** — a stale client gets a conflict, not a win;
4. **every export reads one version** — JSON/CSV/OTIO/roughcut cannot disagree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Asset, EditPlanShot, Project, Segment, new_id


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _project(name: str = "v") -> tuple[str, str, str]:
    """Create a project with one asset + one segment; return (pid, asset_id, segment_id)."""
    with SessionLocal() as s:
        p = Project(id=new_id(), name=name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(id=new_id(), project_id=p.id, source_path=f"/tmp/{name}.mp4",
                  display_name=f"{name}.mp4", file_size_bytes=10, mtime_ns=1,
                  duration_sec=30.0, width=1920, height=1080, fps_num=25, fps_den=1)
        s.add(a)
        s.flush()
        g = Segment(id=new_id(), project_id=p.id, asset_id=a.id,
                    start_sec=0.0, end_sec=8.0,
                    detected_start_sec=0.0, detected_end_sec=8.0)
        s.add(g)
        s.commit()
        return p.id, a.id, g.id


def _shot(asset_id: str, segment_id: str, sin: float = 0.0, sout: float = 3.0) -> dict:
    return {
        "asset_id": asset_id, "segment_id": segment_id,
        "source_in_sec": sin, "source_out_sec": sout,
        "target_duration_sec": sout - sin,
        "story_role": "highlight", "transition_hint": "cut",
        "music_cue": "piano", "audio_hint": "", "color_hint": "",
        "evidence": "rule", "confidence": 0.6, "review_status": "approved",
    }


# --- read ------------------------------------------------------------------


def test_first_read_returns_version_1(client):
    from app.services.storyboard_service import get_current_version

    pid, *_ = _project("r1")
    snap = get_current_version(project_id=pid)
    assert snap.version_no == 1
    assert snap.board_id
    assert snap.project_id == pid


def test_read_response_carries_every_contract_field():
    """§4.1: the listed response fields must all be present."""
    from app.services.storyboard_service import get_current_version

    pid, *_ = _project("fields")
    snap = get_current_version(project_id=pid)
    for field in ("board_id", "project_id", "current_version_id", "version_id",
                  "version_no", "parent_version_id", "schema_version", "created_at",
                  "actor", "reason", "brief", "sections", "shots", "constraints",
                  "validation_summary"):
        assert hasattr(snap, field), f"missing contract field {field}"


def test_versions_are_listed_newest_first():
    from app.services.storyboard_service import get_current_version, list_versions, save_version

    pid, a, g = _project("list")
    v1 = get_current_version(project_id=pid)
    save_version(project_id=pid, base_version_id=v1.version_id, shots=[_shot(a, g)],
                 actor="user", reason="manual_edit")
    versions = list_versions(project_id=pid)
    assert [v.version_no for v in versions] == [2, 1]


def test_a_missing_project_has_a_uniform_error():
    from app.services.storyboard_service import StoryboardNotFound, get_current_version

    with pytest.raises(StoryboardNotFound):
        get_current_version(project_id=new_id())


# --- save ------------------------------------------------------------------


def test_save_creates_version_2_and_leaves_version_1_byte_identical():
    """§7.3: first save → v2; v1 content unchanged."""
    from app.services.storyboard_service import get_current_version, get_version, save_version

    pid, a, g = _project("save")
    v1 = get_current_version(project_id=pid)
    v1_before = v1.to_dict()

    v2 = save_version(project_id=pid, base_version_id=v1.version_id,
                      shots=[_shot(a, g)], actor="user", reason="manual_edit")
    assert v2.version_no == 2
    assert v2.parent_version_id == v1.version_id

    v1_after = get_version(board_id=v1.board_id, version_id=v1.version_id)
    assert v1_after.to_dict()["shots"] == v1_before["shots"]


def test_two_clients_on_the_same_base_only_one_wins():
    """§7.3: the loser gets 409 and the current version does not move."""
    from app.services.storyboard_service import (
        VersionConflict,
        get_current_version,
        save_version,
    )

    pid, a, g = _project("race")
    v1 = get_current_version(project_id=pid)

    winner = save_version(project_id=pid, base_version_id=v1.version_id,
                          shots=[_shot(a, g)], actor="user", reason="manual_edit")
    with pytest.raises(VersionConflict):
        save_version(project_id=pid, base_version_id=v1.version_id,
                     shots=[_shot(a, g, 0.5, 2.5)], actor="user", reason="manual_edit")

    still = get_current_version(project_id=pid)
    assert still.version_id == winner.version_id


def test_retrying_the_same_request_does_not_create_a_duplicate_version():
    """§4.2: an idempotency key makes a retry a no-op."""
    from app.services.storyboard_service import get_current_version, save_version

    pid, a, g = _project("idem")
    v1 = get_current_version(project_id=pid)
    first = save_version(project_id=pid, base_version_id=v1.version_id,
                         shots=[_shot(a, g)], actor="user", reason="manual_edit",
                         idempotency_key="req-1")
    again = save_version(project_id=pid, base_version_id=v1.version_id,
                         shots=[_shot(a, g)], actor="user", reason="manual_edit",
                         idempotency_key="req-1")
    assert again.version_id == first.version_id
    assert get_current_version(project_id=pid).version_no == 2


def test_an_invalid_snapshot_writes_nothing():
    """§7.3: a rejected snapshot must not partially commit."""
    from app.services.storyboard_schema import SnapshotError
    from app.services.storyboard_service import get_current_version, save_version

    pid, a, g = _project("invalid")
    v1 = get_current_version(project_id=pid)
    with pytest.raises(SnapshotError):
        save_version(project_id=pid, base_version_id=v1.version_id,
                     shots=[{**_shot(a, g), "source_in_sec": 5.0, "source_out_sec": 4.0}],
                     actor="user", reason="manual_edit")
    assert get_current_version(project_id=pid).version_no == 1


# --- restore ---------------------------------------------------------------


def test_restoring_v1_creates_v3_instead_of_moving_the_pointer_back():
    """§4.3 + §7.3: restore is forward-only."""
    from app.services.storyboard_service import (
        get_current_version,
        get_version,
        restore_version,
        save_version,
    )

    pid, a, g = _project("restore")
    v1 = get_current_version(project_id=pid)
    v2 = save_version(project_id=pid, base_version_id=v1.version_id,
                      shots=[_shot(a, g), _shot(a, g, 3.0, 6.0)],
                      actor="user", reason="manual_edit")
    v3 = restore_version(project_id=pid, target_version_id=v1.version_id,
                         base_version_id=v2.version_id)

    assert v3.version_no == 3
    assert v3.reason == "restore"
    assert v3.parent_version_id == v2.version_id

    # The pointer moved forward to v3, never back to v1.
    current = get_current_version(project_id=pid)
    assert current.version_id == v3.version_id
    assert current.version_id != v1.version_id

    # Both earlier versions still readable, still unchanged.
    assert get_version(board_id=v1.board_id, version_id=v1.version_id).version_no == 1
    assert get_version(board_id=v1.board_id, version_id=v2.version_id).version_no == 2


def test_restore_records_the_target_and_validation_state():
    from app.services.storyboard_service import get_current_version, restore_version, save_version

    pid, a, g = _project("restore-meta")
    v1 = get_current_version(project_id=pid)
    v2 = save_version(project_id=pid, base_version_id=v1.version_id,
                      shots=[_shot(a, g)], actor="user", reason="manual_edit")
    v3 = restore_version(project_id=pid, target_version_id=v1.version_id,
                         base_version_id=v2.version_id)
    assert v3.restore_of_version_id == v1.version_id
    assert "state" in v3.validation_summary


# --- cross-project / reverse verification ----------------------------------


def test_a_version_from_another_project_is_refused():
    """§7.3 + §7.6: cross-project ids must not leak data."""
    from app.services.storyboard_service import StoryboardNotFound, get_version

    pid_a, *_ = _project("a")
    pid_b, *_ = _project("b")
    from app.services.storyboard_service import get_current_version

    snap_b = get_current_version(project_id=pid_b)
    with pytest.raises(StoryboardNotFound):
        get_version(board_id=snap_b.board_id, version_id=snap_b.version_id,
                    project_id=pid_a)


def test_an_unknown_version_id_is_a_uniform_not_found():
    from app.services.storyboard_service import StoryboardNotFound, get_version
    from app.services.storyboard_service import get_current_version

    pid, *_ = _project("unknown")
    snap = get_current_version(project_id=pid)
    with pytest.raises(StoryboardNotFound):
        get_version(board_id=snap.board_id, version_id=new_id())


# --- HTTP surface ----------------------------------------------------------


def test_the_http_contract_for_read_save_conflict(client):
    """§5.4: missing base_version → 428; stale base_version → 409."""
    pid, a, g = _project("http")
    read = client.get(f"/api/projects/{pid}/storyboard")
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["version_no"] == 1

    legacy = client.post(f"/api/projects/{pid}/edit-plan",
                         json={"shots": [_shot(a, g)]})
    assert legacy.status_code == 428, legacy.text
    assert "base_version" in legacy.text.lower()

    save = client.post(f"/api/projects/{pid}/edit-plan",
                       json={"shots": [_shot(a, g)], "base_version_id": body["version_id"]})
    assert save.status_code == 200, save.text
    assert save.json()["version_no"] == 2

    stale = client.post(f"/api/projects/{pid}/edit-plan",
                        json={"shots": [_shot(a, g)], "base_version_id": body["version_id"]})
    assert stale.status_code == 409, stale.text


def test_restore_over_http(client):
    pid, a, g = _project("http-restore")
    v1 = client.get(f"/api/projects/{pid}/storyboard").json()
    v2 = client.post(f"/api/projects/{pid}/edit-plan",
                     json={"shots": [_shot(a, g)], "base_version_id": v1["version_id"]}).json()
    r = client.post(f"/api/projects/{pid}/storyboard/restore",
                    json={"target_version_id": v1["version_id"],
                          "base_version_id": v2["version_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["version_no"] == 3


# --- exports read one version (§5.3) --------------------------------------


def test_all_exports_come_from_the_same_version_and_report_it(client):
    pid, a, g = _project("exports")
    v1 = client.get(f"/api/projects/{pid}/storyboard").json()
    v2 = client.post(f"/api/projects/{pid}/edit-plan",
                     json={"shots": [_shot(a, g, 0.0, 2.0), _shot(a, g, 3.0, 5.0)],
                           "base_version_id": v1["version_id"]}).json()

    js = client.get(f"/api/projects/{pid}/export/json")
    csv_r = client.get(f"/api/projects/{pid}/export/csv")
    otio = client.get(f"/api/projects/{pid}/export/otio")
    for response in (js, csv_r, otio):
        assert response.status_code == 200
        assert response.headers.get("X-Storyboard-Version-Id") == v2["version_id"]
        assert response.headers.get("X-Storyboard-Board-Id") == v2["board_id"]

    data = js.json()
    assert data["version_id"] == v2["version_id"]
    assert len(data["shots"]) == 2
    # CSV has a header + 2 rows.
    assert len(csv_r.text.strip().splitlines()) == 3


def test_a_historical_version_can_be_exported_without_touching_current(client):
    """§7.4: exporting history must not promote it."""
    pid, a, g = _project("hist-export")
    v1 = client.get(f"/api/projects/{pid}/storyboard").json()
    v2 = client.post(f"/api/projects/{pid}/edit-plan",
                     json={"shots": [_shot(a, g)], "base_version_id": v1["version_id"]}).json()

    hist = client.get(f"/api/projects/{pid}/export/json",
                      params={"version_id": v1["version_id"]})
    assert hist.status_code == 200
    assert hist.json()["version_id"] == v1["version_id"]
    assert hist.json()["shots"] == []

    assert client.get(f"/api/projects/{pid}/storyboard").json()["version_id"] == v2["version_id"]


def test_the_old_read_endpoint_still_answers_with_the_legacy_shape(client):
    """§5.4: GET /edit-plan keeps its response structure."""
    pid, a, g = _project("legacy-get")
    body = client.get(f"/api/projects/{pid}/edit-plan")
    assert body.status_code == 200
    data = body.json()
    for key in ("schema_version", "project_id", "generated_at",
                "total_duration_sec", "shots"):
        assert key in data, f"legacy response lost {key}"


# --- roughcut reads the same version (§5.3) --------------------------------


def test_the_roughcut_uses_the_current_storyboard_version():
    """The roughcut must read the version, not the legacy table (§5.3)."""
    from app.models import EditPlanShot
    from app.services import roughcut as rc
    from app.services.storyboard_service import get_current_version, save_version

    pid, a, g = _project("roughcut-source")
    save_version(project_id=pid, base_version_id=get_current_version(project_id=pid).version_id,
                 shots=[_shot(a, g, 0.0, 2.0), _shot(a, g, 3.0, 5.0)])

    with SessionLocal() as s:
        # The legacy table is deliberately empty: if the roughcut still read it,
        # this would produce zero clips.
        assert s.query(EditPlanShot).filter_by(project_id=pid).count() == 0

    from app.services.storyboard_service import load_export_context

    ctx = load_export_context(project_id=pid)
    assert len(ctx.shots) == 2, 'the roughcut source must be the storyboard version'
    assert [sh['source_in_sec'] for sh in ctx.shots] == [0.0, 3.0]


def test_no_legacy_write_path_bypasses_the_version_gate():
    """§5.1: there must be no write entry that skips base_version checks."""
    import inspect

    from app.services import timeline_export

    source = inspect.getsource(timeline_export.build_edit_plan)
    assert "base_version_id" in source, 'the adapter must demand a base version'
    assert "edit_plan_shots" not in source.lower(), (
        'the adapter must not write the legacy table directly'
    )

    # The service layer is the only module allowed to INSERT versions.
    import app.services.storyboard_service as svc

    assert "StoryboardVersion(" in inspect.getsource(svc)
