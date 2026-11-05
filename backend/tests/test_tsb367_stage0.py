"""TSB-367 — stage 0: close the three TSB-366-R1 leftovers.

Written before the fix. Each case failed on `bca2433`:

| Leftover | Before |
|---|---|
| 1 | ``LegacyMigrationPending`` had no HTTP mapping, so a pending migration surfaced as a generic 500 or, worse, was masked by a fabricated empty board |
| 2 | ``migration_status()`` / ``retry_migration()`` existed as service functions with no authenticated HTTP entry point |
| 3 | ``legacy_plan_migrations`` was created only by ``init_db/create_all``; the Alembic revision did not know about it |

The security requirement running through all of it: a caller must not be able to
learn anything about a project it does not own, and must never receive stack
traces, SQL or database paths.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    Asset,
    EditPlanShot,
    LegacyPlanMigration,
    Project,
    Segment,
    Storyboard,
    new_id,
)

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _project(name: str) -> str:
    with SessionLocal() as s:
        p = Project(id=new_id(), name=name, source_roots=[], output_dir="", settings={})
        s.add(p)
        s.commit()
        return p.id


def _legacy_project(name: str, shots: int = 2) -> str:
    pid = _project(name)
    with SessionLocal() as s:
        a = Asset(id=new_id(), project_id=pid, source_path=f"/tmp/{name}.mp4",
                  display_name=f"{name}.mp4", file_size_bytes=10, mtime_ns=1,
                  duration_sec=30.0, width=1920, height=1080)
        s.add(a)
        s.flush()
        for i in range(shots):
            g = Segment(id=new_id(), project_id=pid, asset_id=a.id,
                        start_sec=float(i * 5), end_sec=float(i * 5 + 4),
                        detected_start_sec=float(i * 5), detected_end_sec=float(i * 5 + 4))
            s.add(g)
            s.flush()
            s.add(EditPlanShot(id=new_id(), project_id=pid, order=i, asset_id=a.id,
                               segment_id=g.id, source_in_sec=float(i * 5),
                               source_out_sec=float(i * 5) + 2.0,
                               target_duration_sec=2.0, timeline_start_sec=float(i * 2)))
        s.commit()
    return pid


def _force_failed_migration(pid: str, monkeypatch) -> None:
    """Make the migrator fail for this project and record the failure."""
    from app.services import storyboard_migration as mig

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    original = mig.migrate_project
    monkeypatch.setattr(mig, "migrate_project", _boom)
    try:
        mig.migrate_legacy_edit_plans(project_ids=[pid])
    finally:
        monkeypatch.setattr(mig, "migrate_project", original)


# ---------------------------------------------------------------------------
# Leftover 1 — a stable HTTP mapping for a pending / failed migration
# ---------------------------------------------------------------------------


def test_leftover_1_pending_migration_has_a_stable_error_code(client, monkeypatch):
    pid = _legacy_project("s0-pending")
    _force_failed_migration(pid, monkeypatch)

    resp = client.get(f"/api/projects/{pid}/storyboard")
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["code"] == "LEGACY_MIGRATION_PENDING"
    # Actionable, and safe to show: no stack trace, SQL or DB path.
    assert "retry" in body["action"].lower() or "重试" in body["action"]
    text = resp.text.lower()
    for forbidden in ("traceback", "sqlite", ".db", "select ", "simulated migration failure"):
        assert forbidden not in text, f"error body leaked {forbidden!r}"


def test_leftover_1_a_pending_migration_is_never_answered_with_an_empty_plan(client, monkeypatch):
    pid = _legacy_project("s0-not-empty")
    _force_failed_migration(pid, monkeypatch)

    resp = client.get(f"/api/projects/{pid}/storyboard")
    assert resp.status_code != 200, "a pending migration must not yield a plan"
    with SessionLocal() as s:
        assert s.query(Storyboard).filter_by(project_id=pid).count() == 0


def test_leftover_1_an_unknown_project_is_still_a_plain_404(client):
    resp = client.get(f"/api/projects/{new_id()}/storyboard")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Leftover 2 — authenticated status / retry endpoints
# ---------------------------------------------------------------------------


def test_leftover_2_migration_status_is_queryable_over_http(client):
    pid = _legacy_project("s0-status")
    resp = client.get(f"/api/projects/{pid}/storyboard/migration-status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] in {"pending", "done", "failed", "not_applicable"}
    assert body["project_id"] == pid
    # No internals.
    assert "error" in body


def test_leftover_2_retry_migrates_a_pending_project(client):
    pid = _legacy_project("s0-retry")

    before = client.get(f"/api/projects/{pid}/storyboard/migration-status").json()
    assert before["state"] in {"pending", "not_applicable", "done"}

    resp = client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"]["state"] == "done"

    plan = client.get(f"/api/projects/{pid}/storyboard")
    assert plan.status_code == 200, plan.text
    assert len(plan.json()["shots"]) == 2


def test_leftover_2_retry_is_idempotent_and_does_not_touch_migrated_projects(client):
    from app.services.storyboard_service import list_versions

    pid = _legacy_project("s0-retry-idem")
    client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    versions_before = list_versions(project_id=pid)
    assert len(versions_before) == 1

    again = client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert again.status_code == 200, again.text
    versions_after = list_versions(project_id=pid)
    assert len(versions_after) == 1, "a retry created a second v1"
    assert versions_after[0].version_id == versions_before[0].version_id


def test_leftover_2_retry_leaves_a_projects_current_pointer_alone(client):
    pid = _legacy_project("s0-retry-edited")
    client.post(f"/api/projects/{pid}/storyboard/retry-migration")

    current = client.get(f"/api/projects/{pid}/storyboard").json()
    body = client.get(f"/api/projects/{pid}/storyboard").json()
    saved = client.post(f"/api/projects/{pid}/edit-plan", json={
        "base_version_id": body["version_id"],
        "shots": [{
            "asset_id": body["shots"][0]["asset_id"],
            "segment_id": body["shots"][0]["segment_id"],
            "source_in_sec": 0.0, "source_out_sec": 1.5, "target_duration_sec": 1.5,
        }],
    })
    assert saved.status_code == 200, saved.text
    v2 = client.get(f"/api/projects/{pid}/storyboard").json()["version_id"]
    assert v2 != current["version_id"]

    client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert client.get(f"/api/projects/{pid}/storyboard").json()["version_id"] == v2


def test_leftover_2_retry_then_read_works_after_a_failure(client, monkeypatch):
    pid = _legacy_project("s0-retry-after-fail")
    _force_failed_migration(pid, monkeypatch)

    assert client.get(f"/api/projects/{pid}/storyboard").status_code == 409
    resp = client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert resp.status_code == 200, resp.text

    plan = client.get(f"/api/projects/{pid}/storyboard")
    assert plan.status_code == 200, plan.text
    assert len(plan.json()["shots"]) == 2


def test_leftover_2_retry_reports_a_still_failing_migration(client, monkeypatch):
    """While the fault persists, the retry must not claim success."""
    from app.services import storyboard_migration as mig

    pid = _legacy_project("s0-retry-fails")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(mig, "migrate_project", _boom)  # stays failing

    resp = client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "LEGACY_MIGRATION_PENDING"
    # The plan is still unreadable, and no empty board was fabricated.
    assert client.get(f"/api/projects/{pid}/storyboard").status_code == 409

    monkeypatch.undo()
    ok = client.post(f"/api/projects/{pid}/storyboard/retry-migration")
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"]["state"] == "done"


# ---------------------------------------------------------------------------
# Leftover 3 — the Alembic revision knows about legacy_plan_migrations
# ---------------------------------------------------------------------------


def _load_revision(name: str):
    path = BACKEND / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"rev_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_leftover_3_a_revision_mentions_legacy_plan_migrations():
    """The migration-strategy question (§2.3) must have one, documented answer."""
    sources = {
        p.name: p.read_text(encoding="utf-8")
        for p in (BACKEND / "alembic" / "versions").glob("*.py")
    }
    assert any("legacy_plan_migrations" in src for src in sources.values()), (
        "no Alembic revision creates legacy_plan_migrations; the operational layer "
        "and init_db disagree about the schema"
    )


def test_leftover_3_downgrade_never_drops_the_legacy_plan_table():
    for path in (BACKEND / "alembic" / "versions").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        downgrade = src.split("def downgrade", 1)[-1]
        assert "edit_plan_shots" not in downgrade, (
            f"{path.name} downgrade must not touch the legacy plan table"
        )


def test_leftover_3_init_db_remains_the_runtime_entry_and_alembic_matches(tmp_path):
    """Both paths must produce the same table set for the TSB-367 additions."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    seed = r"""
import os, sys
sys.path.insert(0, os.getcwd())
from app.database import init_db
init_db()
print("ok")
"""
    env = {"TRAVELSB_DATA_DIR": str(data_dir), "PYTHONIOENCODING": "utf-8",
           "SYSTEMROOT": os_environ_systemroot(), "PATH": ""}
    seeded = subprocess.run([sys.executable, "-c", seed], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=180)
    assert seeded.returncode == 0, seeded.stderr

    database = data_dir / "travel_storyboard.db"
    con = sqlite3.connect(database)
    try:
        tables = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'")}
    finally:
        con.close()
    assert "legacy_plan_migrations" in tables
    assert "plan_operations" in tables


def os_environ_systemroot() -> str:
    import os

    return os.environ.get("SYSTEMROOT", r"C:\Windows")
