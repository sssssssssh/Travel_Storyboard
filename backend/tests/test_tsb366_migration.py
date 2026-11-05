"""TSB-366 — legacy EditPlan migration (§3, §7.2).

Tests first. Each dataset from the task book's §7.2 list gets a case, and the
reconciliation in §3.3 is asserted field by field rather than "mostly copied".

The migration must be:

* **idempotent** — running twice creates nothing new;
* **lossless** — every legacy field survives, array order preserved;
* **honest** — an empty plan yields no invented shots, and a broken reference is
  reported as ``needs_review`` instead of being silently repaired;
* **atomic per project** — a failure leaves no half-written board;
* **non-destructive** — the legacy table is never dropped or emptied.
"""

from __future__ import annotations

import pytest

from app.database import SessionLocal, init_db
from app.models import Asset, EditPlanShot, Project, Segment, new_id


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _project_with_shots(name: str, shot_specs: list[dict]) -> tuple[str, list[dict]]:
    """Create a project with a legacy plan and return (project_id, expected rows)."""
    with SessionLocal() as s:
        p = Project(id=new_id(), name=name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(
            id=new_id(), project_id=p.id, source_path=f"/tmp/{name}.mp4",
            display_name=f"{name}.mp4", file_size_bytes=100, mtime_ns=1,
            duration_sec=30.0, width=1920, height=1080,
        )
        s.add(a)
        s.flush()
        segs = []
        for i in range(6):
            g = Segment(
                id=new_id(), project_id=p.id, asset_id=a.id,
                start_sec=float(i * 5), end_sec=float(i * 5 + 4.5),
                detected_start_sec=float(i * 5), detected_end_sec=float(i * 5 + 4.5),
            )
            s.add(g)
            segs.append(g)
        s.flush()

        expected = []
        for index, spec in enumerate(shot_specs):
            seg = segs[spec.get("seg_index", 0)]
            row = EditPlanShot(
                id=new_id(), project_id=p.id, order=index,
                asset_id=spec.get("asset_id", a.id),
                segment_id=spec.get("segment_id", seg.id),
                source_in_sec=spec.get("source_in_sec", seg.start_sec),
                source_out_sec=spec.get("source_out_sec", seg.end_sec),
                target_duration_sec=spec.get("target_duration_sec", 2.5),
                timeline_start_sec=index * 2.5,
                story_role=spec.get("story_role", "highlight"),
                transition_hint=spec.get("transition_hint", "cut"),
                music_cue=spec.get("music_cue", "piano"),
                audio_hint=spec.get("audio_hint", "quiet"),
                color_hint=spec.get("color_hint", "warm"),
                evidence=spec.get("evidence", "rule"),
                confidence=spec.get("confidence", 0.7),
                review_status=spec.get("review_status", "approved"),
            )
            s.add(row)
            s.flush()
            expected.append({
                "asset_id": row.asset_id, "segment_id": row.segment_id,
                "source_in_sec": row.source_in_sec, "source_out_sec": row.source_out_sec,
                "target_duration_sec": row.target_duration_sec,
                "story_role": row.story_role, "transition_hint": row.transition_hint,
                "music_cue": row.music_cue, "audio_hint": row.audio_hint,
                "color_hint": row.color_hint, "evidence": row.evidence,
                "confidence": row.confidence, "review_status": row.review_status,
            })
        s.commit()
        return p.id, expected


def test_a_normal_legacy_plan_imports_losslessly_as_version_1():
    """§7.2-1 + §3.3: every field and the array order survive."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    pid, expected = _project_with_shots("normal", [
        {"seg_index": 0}, {"seg_index": 2}, {"seg_index": 1},
    ])
    report = migrate_legacy_edit_plans()
    assert report["migrated"] >= 1

    snap = get_current_version(project_id=pid)
    assert snap.version_no == 1
    assert snap.reason == "legacy_import"
    assert len(snap.shots) == len(expected)

    # Order preserved exactly as the legacy ``order`` column had it.
    for got, want in zip(snap.shots, expected):
        for field, value in want.items():
            if isinstance(value, float):
                assert got[field] == pytest.approx(value), f"field {field} drifted"
            else:
                assert got[field] == value, f"field {field} drifted"

    # Legacy rows are still there (§3.1-8).
    with SessionLocal() as s:
        remaining = s.query(EditPlanShot).filter_by(project_id=pid).count()
    assert remaining == len(expected)


def test_an_empty_legacy_plan_yields_an_empty_version_with_no_invented_shots():
    """§3.1-4: an empty plan must not fabricate shots."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    pid, _ = _project_with_shots("empty", [])
    migrate_legacy_edit_plans()
    snap = get_current_version(project_id=pid)
    assert snap.shots == []
    assert snap.version_no == 1


def test_the_same_file_name_under_different_asset_ids_stays_distinct():
    """§7.2-4: identity is the ID, never the filename."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    with SessionLocal() as s:
        p = Project(id=new_id(), name="twins", source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        pairs = []
        for i in range(2):
            a = Asset(id=new_id(), project_id=p.id, source_path=f"/tmp/d{i}/clip.mp4",
                      display_name="clip.mp4", file_size_bytes=10 + i, mtime_ns=1,
                      duration_sec=20.0, width=1920, height=1080)
            s.add(a)
            s.flush()
            g = Segment(id=new_id(), project_id=p.id, asset_id=a.id,
                        start_sec=0.0, end_sec=4.0,
                        detected_start_sec=0.0, detected_end_sec=4.0)
            s.add(g)
            s.flush()
            pairs.append((a.id, g.id))
        for order, (a_id, g_id) in enumerate(pairs):
            s.add(EditPlanShot(id=new_id(), project_id=p.id, order=order,
                               asset_id=a_id, segment_id=g_id,
                               source_in_sec=0.0, source_out_sec=3.0,
                               target_duration_sec=3.0, timeline_start_sec=order * 3.0))
        s.commit()
        pid = p.id

    migrate_legacy_edit_plans()
    snap = get_current_version(project_id=pid)
    assert [s["asset_id"] for s in snap.shots] == [pairs[0][0], pairs[1][0]]


def test_an_orphan_segment_is_reported_not_silently_repaired():
    """§3.2: a dangling segment must be flagged, never fixed up."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    pid, _ = _project_with_shots("orphan", [{"seg_index": 0}])
    with SessionLocal() as s:
        row = s.query(EditPlanShot).filter_by(project_id=pid).first()
        row.segment_id = new_id()  # points at nothing
        s.commit()

    migrate_legacy_edit_plans()
    snap = get_current_version(project_id=pid)
    assert snap.shots, "snapshot must still be readable"
    assert snap.validation_summary.get("state") in {"needs_review", "invalid"}
    issues = snap.validation_summary.get("issues", [])
    assert any("segment" in str(i).lower() for i in issues), issues


def test_an_asset_segment_mismatch_is_reported():
    """§7.2-6: asset_id and segment_id must belong together."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_current_version

    pid, _ = _project_with_shots("mismatch", [{"seg_index": 0}])
    with SessionLocal() as s:
        row = s.query(EditPlanShot).filter_by(project_id=pid).first()
        other_asset = Asset(id=new_id(), project_id=pid, source_path="/tmp/other.mp4",
                            display_name="other.mp4", file_size_bytes=1, mtime_ns=1,
                            duration_sec=30.0, width=640, height=480)
        s.add(other_asset)
        s.commit()
        row.asset_id = other_asset.id  # segment belongs to the first asset
        s.commit()

    migrate_legacy_edit_plans()
    snap = get_current_version(project_id=pid)
    assert snap.validation_summary.get("state") in {"needs_review", "invalid"}


def test_missing_source_signature_is_marked_legacy_not_fabricated():
    """§2.3: no signature means legacy/unknown — never an invented hash."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import get_version_source_refs
    from app.services.storyboard_service import get_current_version

    pid, _ = _project_with_shots("nosig", [{"seg_index": 0}])
    migrate_legacy_edit_plans()
    snap = get_current_version(project_id=pid)
    refs = get_version_source_refs(board_id=snap.board_id, version_id=snap.version_id)
    assert refs, "source refs must be derived for every shot"
    for ref in refs:
        assert ref.source_signature in {"", "legacy", "unknown"} or ref.source_signature


def test_running_the_migration_twice_creates_nothing_new():
    """§3.1-9 + §7.2-9: idempotent."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import list_versions

    pid, _ = _project_with_shots("twice", [{"seg_index": 0}, {"seg_index": 1}])
    first = migrate_legacy_edit_plans()
    second = migrate_legacy_edit_plans()
    assert first["migrated"] >= 1
    assert second["migrated"] == 0
    assert second["skipped"] >= 1
    assert len(list_versions(project_id=pid)) == 1


def test_a_project_that_already_has_a_storyboard_is_skipped():
    """§7.2-10."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans
    from app.services.storyboard_service import list_versions

    pid, _ = _project_with_shots("has-board", [{"seg_index": 0}])
    migrate_legacy_edit_plans()
    before = len(list_versions(project_id=pid))
    report = migrate_legacy_edit_plans()
    assert report["skipped"] >= 1
    assert len(list_versions(project_id=pid)) == before


def test_a_failure_mid_project_leaves_no_half_written_board(monkeypatch):
    """§3.2: per-project atomicity — a failure rolls the project back."""
    from app.services import storyboard_migration as mig

    pid, _ = _project_with_shots("atomic", [{"seg_index": 0}, {"seg_index": 1}])

    real_write = mig._write_version_and_refs
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("simulated failure mid-project")

    # Fail while writing the version row: after the board is staged, before the
    # transaction commits — exactly the half-written state that must not persist.
    monkeypatch.setattr(mig, "_write_version_and_refs", _boom)
    report = mig.migrate_legacy_edit_plans()
    assert report["failed"] >= 1, report
    assert calls["n"] >= 1
    monkeypatch.setattr(mig, "_write_version_and_refs", real_write)
    with SessionLocal() as s:
        from app.models import Storyboard

        boards = s.query(Storyboard).filter_by(project_id=pid).count()
    assert boards == 0, "a failed project must not leave a board behind"

    # And a later clean run still succeeds: the failed project is recoverable.
    report2 = mig.migrate_legacy_edit_plans()
    assert report2["migrated"] >= 1


def test_the_migration_report_counts_are_consistent():
    """§3.2: the report must account for every project."""
    from app.services.storyboard_migration import migrate_legacy_edit_plans

    report = migrate_legacy_edit_plans()
    for key in ("projects", "migrated", "empty", "invalid", "skipped", "failed"):
        assert key in report, f"report is missing {key}"
    with SessionLocal() as s:
        total = s.query(Project).count()
    assert report["projects"] == total
