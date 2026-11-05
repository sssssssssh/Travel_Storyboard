"""TSB-366 — the operational migration layer (Alembic) and its relationship to
the runtime bootstrap.

Two things are worth testing here, and neither is "does Alembic work in general":

1. **The two trigger definitions must not drift.** ``app.database`` installs the
   storyboard invariants at startup; ``alembic/versions/0001_*.py`` declares the
   same set for operators. If someone adds an invariant to one and forgets the
   other, a freshly-bootstrapped database and an Alembic-built one would enforce
   different rules.
2. **Downgrade must not delete the legacy plan.** That is the §3.4 requirement
   ("不得删除迁移前旧计划数据"); losing it would destroy the very data the
   migration was derived from.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

#: The app itself never imports alembic, but this suite *tests* the operational
#: migration layer, so a missing install is a broken dev environment rather than a
#: platform limitation. Following AGENTS.md §4.5 ("工具缺失必须继续失败"), it fails
#: loudly instead of being added to the platform-skip allow-list — a silently
#: skipped migration test is exactly the class of hole that allow-list exists to
#: prevent.
try:
    import alembic  # noqa: F401
except ImportError as _exc:  # pragma: no cover - environment guard
    raise ImportError(
        "TSB-366 schema-op tests require alembic: pip install -r requirements-dev.txt"
    ) from _exc

BACKEND = Path(__file__).resolve().parents[1]
VERSION_FILE = BACKEND / "alembic" / "versions" / "0001_storyboard_versions.py"


def _load_version_module():
    spec = importlib.util.spec_from_file_location("tsb366_rev_0001", VERSION_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_alembic_revision_declares_the_same_triggers_as_the_app():
    """One source of truth for the trigger invariants (§7.1, TSB-367 §2.3).

    The check spans **every** revision, not just 0001: TSB-367 added plan-operation
    invariants, and if those were added to the app without a matching revision the
    two bootstrap paths would enforce different rules — exactly the drift this test
    exists to catch.
    """
    from app.database import _STORYBOARD_TRIGGERS

    app_names = {name for name, _ddl in _STORYBOARD_TRIGGERS}

    rev_names: set[str] = set()
    for path in (BACKEND / "alembic" / "versions").glob("*.py"):
        spec = importlib.util.spec_from_file_location(f"rev_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr in ("TRIGGERS", "PLAN_OPERATION_TRIGGERS"):
            for name, _ddl in getattr(module, attr, ()):
                rev_names.add(name)

    assert rev_names == app_names, (
        f"trigger sets drifted — app-only: {sorted(app_names - rev_names)}, "
        f"revision-only: {sorted(rev_names - app_names)}"
    )


def test_the_revision_downgrade_never_touches_the_legacy_plan_table():
    """§3.4: downgrade must drop the storyboard tables only."""
    import inspect

    revision = _load_version_module()
    source = inspect.getsource(revision.downgrade)
    assert "storyboards" in source
    assert "edit_plan_shots" not in source, (
        "downgrade must not drop the legacy plan table: it is the migration source"
    )


SEED_SCRIPT = r"""
import os, sys
sys.path.insert(0, os.getcwd())
from app.database import init_db, SessionLocal
from app.models import Asset, EditPlanShot, Project, Segment, new_id
init_db()
pid, aid, gid = new_id(), new_id(), new_id()
with SessionLocal() as s:
    s.add(Project(id=pid, name="iso", source_roots=[], output_dir="", settings={}))
    s.flush()
    s.add(Asset(id=aid, project_id=pid, source_path="/tmp/x.mp4", display_name="x.mp4",
                file_size_bytes=1, mtime_ns=1, duration_sec=20.0, width=640, height=480))
    s.flush()
    s.add(Segment(id=gid, project_id=pid, asset_id=aid, start_sec=0, end_sec=5,
                  detected_start_sec=0, detected_end_sec=5))
    s.flush()
    s.add(EditPlanShot(id=new_id(), project_id=pid, order=0, asset_id=aid,
                       segment_id=gid, source_in_sec=1.0, source_out_sec=4.5,
                       target_duration_sec=3.5))
    s.commit()
print("seeded")
"""


def _subprocess_env(data_dir: Path) -> dict:
    import os

    keep = {"SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT", "PATH"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({"TRAVELSB_DATA_DIR": str(data_dir), "PYTHONIOENCODING": "utf-8"})
    return env


def test_upgrade_downgrade_round_trip_preserves_legacy_rows(tmp_path):
    """A real upgrade -> downgrade cycle on an isolated database.

    Everything runs in subprocesses so the module-level engine in
    ``app.database`` (bound to the test session's data dir) is never disturbed.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = _subprocess_env(data_dir)

    seeded = subprocess.run(
        [sys.executable, "-c", SEED_SCRIPT], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert seeded.returncode == 0, seeded.stderr
    assert "seeded" in seeded.stdout

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
            capture_output=True, text=True, timeout=180,
        )

    stamped = alembic("stamp", "0001_storyboard_versions")
    assert stamped.returncode == 0, stamped.stderr

    down = alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr

    database = data_dir / "travel_storyboard.db"
    con = sqlite3.connect(database)
    try:
        names = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"
        )}
        assert not {"storyboards", "storyboard_versions",
                    "storyboard_source_refs"} & names
        assert "edit_plan_shots" in names, "downgrade dropped the legacy plan table"
        assert con.execute("select count(*) from edit_plan_shots").fetchone()[0] == 1
    finally:
        con.close()

    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr

    con = sqlite3.connect(database)
    try:
        names = {r[0] for r in con.execute(
            "select name from sqlite_master where type='table'"
        )}
        assert {"storyboards", "storyboard_versions",
                "storyboard_source_refs"} <= names
        triggers = {r[0] for r in con.execute(
            "select name from sqlite_master where type='trigger'"
        )}
        assert "trg_storyboard_version_immutable" in triggers
        # The legacy rows survived the whole round trip.
        assert con.execute("select count(*) from edit_plan_shots").fetchone()[0] == 1
    finally:
        con.close()


