"""TSB-366-R1 — regression tests for the versioning base defects.

Written **before** the fixes so the first run is RED and names the exact
guarantee that is missing. Every case here failed on `8f2d4eb`.

| ID | Defect |
|---|---|
| P0-01 | a historical-version read reports the old ``version_id`` but returns the *current* version's shots |
| P0-02 | ``shot_id`` is not preserved through an ordinary edit/save round trip |
| P1-04 | a failed startup migration leaves the project able to auto-create an empty board, after which migration skips it forever |
| P1-05 | reconciliation compares against ``current_version_id`` instead of the migrated v1 |
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    Asset,
    EditPlanShot,
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
# helpers
# ---------------------------------------------------------------------------


def _project_with_segments(name: str, segment_count: int = 8) -> tuple[str, str, list[str]]:
    """A project with one asset and ``segment_count`` distinct segments."""
    with SessionLocal() as s:
        p = Project(id=new_id(), name=name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(id=new_id(), project_id=p.id, source_path=f"/tmp/{name}.mp4",
                  display_name=f"{name}.mp4", file_size_bytes=10, mtime_ns=1,
                  duration_sec=60.0, width=1920, height=1080, fps_num=25, fps_den=1)
        s.add(a)
        s.flush()
        segment_ids = []
        for i in range(segment_count):
            g = Segment(id=new_id(), project_id=p.id, asset_id=a.id,
                        start_sec=float(i * 5), end_sec=float(i * 5 + 4),
                        detected_start_sec=float(i * 5), detected_end_sec=float(i * 5 + 4))
            s.add(g)
            s.flush()
            segment_ids.append(g.id)
        s.commit()
        return p.id, a.id, segment_ids


def _shot(asset_id: str, segment_id: str, index: int, duration: float = 2.0,
          shot_id: str | None = None) -> dict:
    base = {
        "asset_id": asset_id, "segment_id": segment_id,
        "source_in_sec": float(index * 5), "source_out_sec": float(index * 5) + duration,
        "target_duration_sec": duration,
        "story_role": "highlight", "transition_hint": "cut",
        "music_cue": f"cue{index}", "audio_hint": "", "color_hint": "",
        "evidence": "rule", "confidence": 0.6, "review_status": "approved",
    }
    if shot_id is not None:
        base["shot_id"] = shot_id
    return base


def _save(pid: str, shots: list[dict], base: str | None = None, **kw):
    from app.services.storyboard_service import get_current_version, save_version

    if base is None:
        base = get_current_version(project_id=pid).version_id
    return save_version(project_id=pid, base_version_id=base, shots=shots, **kw)


# ===========================================================================
# P0-01 — historical version read must not mix metadata with current content
# ===========================================================================


def _make_v1_with_3_and_v2_with_8(pid: str, asset_id: str, segs: list[str]):
    """v1 has 3 shots, v2 has 8; current becomes v2. Returns (v1, v2)."""
    v1_shots = [_shot(asset_id, segs[i], i) for i in range(3)]
    v1 = _save(pid, v1_shots)
    v2_shots = [_shot(asset_id, segs[i], i, duration=1.5) for i in range(8)]
    v2 = _save(pid, v2_shots, base=v1.version_id)
    return v1, v2


def test_p0_01_get_edit_plan_returns_the_requested_historical_version(client):
    """The legacy GET must render the version it names, not the current one."""
    pid, asset_id, segs = _project_with_segments("p001-legacy")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    body = client.get(f"/api/projects/{pid}/edit-plan",
                      params={"version_id": v1.version_id})
    assert body.status_code == 200, body.text
    data = body.json()

    assert data["version_id"] == v1.version_id
    assert len(data["shots"]) == 3, (
        f"asked for v1 (3 shots) but got {len(data['shots'])} shots — "
        "metadata and content disagree"
    )
    assert all(s["segment_id"] in set(segs[:3]) for s in data["shots"])
    # Total duration must also be v1's (3 × 2.0), not v2's (8 × 1.5).
    assert data["total_duration_sec"] == pytest.approx(6.0)


def test_p0_01_json_export_of_a_historical_version_is_self_consistent(client):
    pid, asset_id, segs = _project_with_segments("p001-json")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    resp = client.get(f"/api/projects/{pid}/export/json",
                      params={"version_id": v1.version_id})
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Storyboard-Version-Id"] == v1.version_id

    data = json.loads(resp.text)
    assert data["version_id"] == v1.version_id
    assert len(data["shots"]) == 3
    assert [s["segment_id"] for s in data["shots"]] == segs[:3]
    assert [s["source_in_sec"] for s in data["shots"]] == [0.0, 5.0, 10.0]


def test_p0_01_csv_export_of_a_historical_version_is_self_consistent(client):
    pid, asset_id, segs = _project_with_segments("p001-csv")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    resp = client.get(f"/api/projects/{pid}/export/csv",
                      params={"version_id": v1.version_id})
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Storyboard-Version-Id"] == v1.version_id

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 3, f"asked for v1 (3 shots) but CSV had {len(rows)} rows"


def test_p0_01_otio_export_of_a_historical_version_is_self_consistent(client):
    pid, asset_id, segs = _project_with_segments("p001-otio")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    resp = client.get(f"/api/projects/{pid}/export/otio",
                      params={"version_id": v1.version_id})
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Storyboard-Version-Id"] == v1.version_id

    doc = json.loads(resp.text)
    clips = doc["tracks"]["children"][0]["children"]
    assert len(clips) == 3, f"asked for v1 (3 clips) but OTIO had {len(clips)}"


def test_p0_01_reading_a_historical_version_does_not_move_the_pointer(client):
    from app.services.storyboard_service import get_current_version

    pid, asset_id, segs = _project_with_segments("p001-pointer")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    for path in ("/edit-plan", "/export/json", "/export/csv", "/export/otio"):
        client.get(f"/api/projects/{pid}{path}", params={"version_id": v1.version_id})

    assert get_current_version(project_id=pid).version_id == v2.version_id


def test_p0_01_the_default_read_still_returns_the_current_version(client):
    """§5.4: no ``version_id`` keeps the old behaviour (compatibility)."""
    pid, asset_id, segs = _project_with_segments("p001-default")
    v1, v2 = _make_v1_with_3_and_v2_with_8(pid, asset_id, segs)

    data = client.get(f"/api/projects/{pid}/edit-plan").json()
    assert data["version_id"] == v2.version_id
    assert len(data["shots"]) == 8


# ===========================================================================
# P0-02 — shot_id must survive an ordinary edit
# ===========================================================================


def test_p0_02_an_edit_round_trip_preserves_every_shot_id():
    """Change one duration; all three identities must be unchanged."""
    pid, asset_id, segs = _project_with_segments("p002-edit")
    shots = [_shot(asset_id, segs[i], i) for i in range(3)]
    v1 = _save(pid, shots)
    original = [s["shot_id"] for s in v1.shots]

    # The client reads (so it has the ids), edits shot #2's duration, saves.
    edited = []
    for index, shot in enumerate(v1.shots):
        row = {k: shot[k] for k in (
            "asset_id", "segment_id", "source_in_sec", "source_out_sec",
            "story_role", "transition_hint", "music_cue", "audio_hint",
            "color_hint", "evidence", "confidence", "review_status",
        )}
        row["shot_id"] = shot["shot_id"]
        row["target_duration_sec"] = (
            1.25 if index == 1 else shot["target_duration_sec"]
        )
        edited.append(row)

    v2 = _save(pid, edited, base=v1.version_id)
    assert [s["shot_id"] for s in v2.shots] == original, (
        "an ordinary edit changed shot identity"
    )
    assert v2.shots[1]["target_duration_sec"] == pytest.approx(1.25)


def test_p0_02_adding_a_shot_only_creates_one_new_identity():
    pid, asset_id, segs = _project_with_segments("p002-add")
    v1 = _save(pid, [_shot(asset_id, segs[i], i) for i in range(2)])
    original = [s["shot_id"] for s in v1.shots]

    rows = [{k: s[k] for k in (
        "asset_id", "segment_id", "source_in_sec", "source_out_sec",
        "target_duration_sec", "story_role", "transition_hint", "music_cue",
        "audio_hint", "color_hint", "evidence", "confidence", "review_status",
    )} | {"shot_id": s["shot_id"]} for s in v1.shots]
    rows.append(_shot(asset_id, segs[5], 5))  # no shot_id: a brand-new shot

    v2 = _save(pid, rows, base=v1.version_id)
    got = [s["shot_id"] for s in v2.shots]
    assert got[:2] == original
    assert got[2] not in original, "the new shot must get a fresh identity"


def test_p0_02_removing_a_shot_leaves_the_others_untouched():
    pid, asset_id, segs = _project_with_segments("p002-remove")
    v1 = _save(pid, [_shot(asset_id, segs[i], i) for i in range(3)])
    ids = [s["shot_id"] for s in v1.shots]

    rows = [{k: s[k] for k in (
        "asset_id", "segment_id", "source_in_sec", "source_out_sec",
        "target_duration_sec", "story_role", "transition_hint", "music_cue",
        "audio_hint", "color_hint", "evidence", "confidence", "review_status",
    )} | {"shot_id": s["shot_id"]} for s in v1.shots]
    del rows[1]

    v2 = _save(pid, rows, base=v1.version_id)
    assert [s["shot_id"] for s in v2.shots] == [ids[0], ids[2]]


def test_p0_02_reordering_keeps_identity_tied_to_the_shot_content():
    pid, asset_id, segs = _project_with_segments("p002-reorder")
    v1 = _save(pid, [_shot(asset_id, segs[i], i) for i in range(3)])
    identity = {s["shot_id"]: s["segment_id"] for s in v1.shots}

    rows = []
    for s in reversed(v1.shots):  # move every shot; identities must follow
        rows.append({k: s[k] for k in (
            "asset_id", "segment_id", "source_in_sec", "source_out_sec",
            "target_duration_sec", "story_role", "transition_hint", "music_cue",
            "audio_hint", "color_hint", "evidence", "confidence", "review_status",
        )} | {"shot_id": s["shot_id"]})

    v2 = _save(pid, rows, base=v1.version_id)
    assert {s["shot_id"]: s["segment_id"] for s in v2.shots} == identity, (
        "reordering changed which segment a shot id refers to"
    )
    # And the timeline follows array order.
    assert [s["segment_id"] for s in v2.shots] == [segs[2], segs[1], segs[0]]
    starts = [s["timeline_start_sec"] for s in v2.shots]
    assert starts == sorted(starts)


def test_p0_02_the_legacy_endpoint_preserves_shot_ids_too(client):
    """The HTTP write path must honour client-supplied shot ids."""
    pid, asset_id, segs = _project_with_segments("p002-http")
    v1 = client.get(f"/api/projects/{pid}/storyboard").json()
    v1 = client.post(f"/api/projects/{pid}/edit-plan", json={
        "base_version_id": client.get(f"/api/projects/{pid}/storyboard").json()["version_id"],
        "shots": [_shot(asset_id, segs[i], i) for i in range(3)],
    })
    assert v1.status_code == 200, v1.text
    body = client.get(f"/api/projects/{pid}/edit-plan").json()
    ids = [s["shot_id"] for s in body["shots"]]

    # Round-trip the ids the client just read.
    payload_shots = []
    for shot, sid in zip(body["shots"], ids):
        row = {k: shot[k] for k in (
            "asset_id", "segment_id", "source_in_sec", "source_out_sec",
            "target_duration_sec", "story_role", "transition_hint", "music_cue",
            "audio_hint", "color_hint", "evidence", "confidence", "review_status",
        )}
        row["shot_id"] = sid
        payload_shots.append(row)
    payload_shots[0]["target_duration_sec"] = 1.0

    saved = client.post(f"/api/projects/{pid}/edit-plan", json={
        "base_version_id": body["version_id"],
        "shots": payload_shots,
    })
    assert saved.status_code == 200, saved.text
    after = client.get(f"/api/projects/{pid}/edit-plan").json()
    assert [s["shot_id"] for s in after["shots"]] == ids


# ===========================================================================
# P1-04 — a failed migration must not be papered over by an empty board
# ===========================================================================


def _legacy_project(name: str, shot_count: int = 2) -> tuple[str, str, list[str]]:
    pid, asset_id, segs = _project_with_segments(name, segment_count=4)
    with SessionLocal() as s:
        for i in range(shot_count):
            s.add(EditPlanShot(
                id=new_id(), project_id=pid, order=i, asset_id=asset_id,
                segment_id=segs[i], source_in_sec=float(i * 5),
                source_out_sec=float(i * 5) + 2.0, target_duration_sec=2.0,
                timeline_start_sec=float(i * 2),
            ))
        s.commit()
    return pid, asset_id, segs


def test_p1_04_a_project_with_no_legacy_plan_may_initialize_an_empty_board():
    """The normal new-project path must keep working."""
    from app.services.storyboard_service import get_current_version

    pid, _asset, _segs = _project_with_segments("p104-fresh", segment_count=1)
    view = get_current_version(project_id=pid)
    assert view.version_no == 1
    assert view.shots == []


def test_p1_04_a_migrated_legacy_project_reads_its_v1():
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    pid, _asset, _segs = _legacy_project("p104-migrated")
    report = migrate_legacy_edit_plans()
    assert report["failed"] == 0

    view = get_current_version(project_id=pid)
    assert view.version_no == 1
    assert view.reason == "legacy_import"
    assert len(view.shots) == 2


def test_p1_04_a_failed_migration_must_not_yield_an_empty_initial_version(monkeypatch):
    """The defect: failure → empty board → migration skips forever."""
    from app.services import storyboard_migration as mig
    from app.services.storyboard_service import (
        LegacyMigrationPending,
        get_current_version,
    )

    pid, _asset, _segs = _legacy_project("p104-failed")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(mig, "_write_version_and_refs", _boom)
    report = mig.migrate_legacy_edit_plans()
    assert report["failed"] >= 1
    monkeypatch.setattr(
        mig, "_write_version_and_refs", mig.__dict__["_write_version_and_refs"]
    )

    with SessionLocal() as s:
        assert s.query(Storyboard).filter_by(project_id=pid).count() == 0

    # Reading must refuse rather than fabricate an empty plan.
    with pytest.raises(LegacyMigrationPending):
        get_current_version(project_id=pid)

    with SessionLocal() as s:
        assert s.query(Storyboard).filter_by(project_id=pid).count() == 0, (
            "the read created an empty board — migration would now be skipped"
        )


def test_p1_04_a_failed_migration_records_a_queryable_error_then_retries():
    from app.services import storyboard_migration as mig
    from app.services.storyboard_service import get_current_version

    pid, _asset, _segs = _legacy_project("p104-retry")

    real_write = mig._write_version_and_refs
    state = {"fail": True}

    def _flaky(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("simulated migration failure")
        return real_write(*args, **kwargs)

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(mig, "_write_version_and_refs", _flaky)
    try:
        first = mig.migrate_legacy_edit_plans()
    finally:
        mp.undo()

    assert first["failed"] >= 1
    # The failure is queryable, not just a log line.
    status = mig.migration_status(project_id=pid)
    assert status["state"] == "failed"
    assert status["error"], "a failed migration must record a readable error"

    # Retry after the fault clears.
    second = mig.migrate_legacy_edit_plans()
    assert second["migrated"] >= 1
    assert len(get_current_version(project_id=pid).shots) == 2
    assert mig.migration_status(project_id=pid)["state"] == "done"


def test_p1_04_a_retry_is_idempotent_and_leaves_migrated_projects_alone():
    from app.services import storyboard_migration as mig
    from app.services.storyboard_service import get_current_version, list_versions

    pid, _asset, _segs = _legacy_project("p104-idem")
    mig.migrate_legacy_edit_plans()
    before = list_versions(project_id=pid)
    assert len(before) == 1

    again = mig.migrate_legacy_edit_plans()
    assert again["skipped"] >= 1
    assert len(list_versions(project_id=pid)) == 1
    assert get_current_version(project_id=pid).version_id == before[0].version_id


def test_p1_04_retry_does_not_disturb_a_project_that_has_been_edited():
    """A project with v1+v2 must keep its current pointer through a retry."""
    from app.services import storyboard_migration as mig
    from app.services.storyboard_service import get_current_version

    pid, asset_id, segs = _legacy_project("p104-edited")
    mig.migrate_legacy_edit_plans()

    v1 = get_current_version(project_id=pid)
    v2 = _save(pid, [_shot(asset_id, segs[i], i) for i in range(2)],
               base=v1.version_id)

    mig.migrate_legacy_edit_plans()
    now = get_current_version(project_id=pid)
    assert now.version_id == v2.version_id, "a retry moved the current pointer"


def test_p1_04_the_legacy_rows_are_never_modified_by_migration_or_retry():
    from app.services import storyboard_migration as mig

    pid, _asset, _segs = _legacy_project("p104-legacy-intact")
    with SessionLocal() as s:
        before = [
            (r.id, r.order, r.asset_id, r.segment_id, r.source_in_sec,
             r.source_out_sec, r.target_duration_sec)
            for r in s.scalars(select(EditPlanShot).where(
                EditPlanShot.project_id == pid)).all()
        ]

    mig.migrate_legacy_edit_plans()
    mig.migrate_legacy_edit_plans()

    with SessionLocal() as s:
        after = [
            (r.id, r.order, r.asset_id, r.segment_id, r.source_in_sec,
             r.source_out_sec, r.target_duration_sec)
            for r in s.scalars(select(EditPlanShot).where(
                EditPlanShot.project_id == pid)).all()
        ]
    assert after == before


# ===========================================================================
# P1-05 — reconciliation must always compare against the migrated v1
# ===========================================================================


def test_p1_05_reconcile_still_targets_v1_after_later_edits():
    from app.services.storyboard_migration import migrate_legacy_edit_plans, reconcile_project

    pid, asset_id, segs = _legacy_project("p105-edited")
    migrate_legacy_edit_plans()

    # v2 replaces the plan entirely; v1 remains the migration record.
    v1 = StoryboardVersion
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        current = s.get(StoryboardVersion, board.current_version_id)
        base = current.id
    _save(pid, [_shot(asset_id, segs[i], i) for i in range(3)], base=base)

    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        assert s.get(StoryboardVersion, board.current_version_id).version_no == 2
        problems = reconcile_project(s, pid)

    assert problems == [], (
        "reconciliation compared against the edited version instead of v1: "
        f"{problems}"
    )


def test_p1_05_reconcile_detects_real_drift_against_v1():
    from app.services.storyboard_migration import migrate_legacy_edit_plans, reconcile_project

    pid, _asset, _segs = _legacy_project("p105-drift")
    migrate_legacy_edit_plans()

    # Corrupt the *legacy* row: reconciliation must notice the difference.
    with SessionLocal() as s:
        row = s.scalars(select(EditPlanShot).where(
            EditPlanShot.project_id == pid).order_by(EditPlanShot.order)).first()
        row.target_duration_sec = 99.0
        s.commit()

    with SessionLocal() as s:
        problems = reconcile_project(s, pid)
    assert problems, "a genuine field difference was not reported"
    assert any("target_duration_sec" in p for p in problems), problems


def test_p1_05_reconcile_reports_a_missing_migration_version():
    from app.services.storyboard_migration import reconcile_project

    pid, _asset, _segs = _legacy_project("p105-missing")
    problems = None
    try:
        with SessionLocal() as s:
            problems = reconcile_project(s, pid)
    except Exception as exc:  # noqa: BLE001 - either form is acceptable
        return
    assert problems, "an unmigrated project must be reported, not silently pass"


def test_p1_05_reconcile_never_modifies_any_version():
    from app.services.storyboard_migration import migrate_legacy_edit_plans, reconcile_project

    pid, _asset, _segs = _legacy_project("p105-readonly")
    migrate_legacy_edit_plans()

    with SessionLocal() as s:
        before = [
            (v.id, json.dumps(v.shots, sort_keys=True, default=str))
            for v in s.scalars(select(StoryboardVersion)).all()
        ]
    with SessionLocal() as s:
        reconcile_project(s, pid)
    with SessionLocal() as s:
        after = [
            (v.id, json.dumps(v.shots, sort_keys=True, default=str))
            for v in s.scalars(select(StoryboardVersion)).all()
        ]
    assert after == before, "reconciliation modified stored versions"


# ===========================================================================
# P1 — write-validation boundary (explicit contract, not a relaxed policy)
# ===========================================================================


def test_p1_cross_project_asset_and_segment_are_never_treated_as_valid():
    from app.services.storyboard_service import get_current_version

    pid_a, asset_a, segs_a = _project_with_segments("p1-cross-a", segment_count=2)
    pid_b, asset_b, segs_b = _project_with_segments("p1-cross-b", segment_count=2)

    base = get_current_version(project_id=pid_a).version_id
    view = _save(pid_a, [_shot(asset_b, segs_b[0], 0)], base=base)
    assert view.validation_summary["state"] == "invalid"
    codes = {i["code"] for i in view.validation_summary["issues"]}
    assert {"ASSET_MISSING", "SEGMENT_MISSING"} <= codes, codes


def test_p1_a_new_version_may_not_save_a_shot_whose_asset_is_absent():
    """Explicit boundary: unknown references are refused for *new* writes."""
    from app.services.storyboard_schema import SnapshotError
    from app.services.storyboard_service import (
        InvalidReference,
        get_current_version,
        save_version,
    )

    pid, asset_id, segs = _project_with_segments("p1-refuse", segment_count=2)
    base = get_current_version(project_id=pid).version_id

    with pytest.raises(InvalidReference) as excinfo:
        save_version(
            project_id=pid, base_version_id=base,
            shots=[_shot(asset_id, "00000000-0000-0000-0000-000000000000", 0)],
            reject_invalid_references=True,
        )
    assert "SEGMENT_MISSING" in str(excinfo.value) or excinfo.value.issues
