"""TSB-367-R1 — Alembic history must stay honest, and both paths must agree (R1-04).

Tests first. On `eb63003` the `legacy_plan_migrations` table was added to the
**already-published** ``0001`` revision. A database that had already run the old
0001 never re-runs it, so:

* fresh ``init_db()`` has the table,
* fresh ``alembic upgrade head`` has the table,
* a **historical** database upgraded to head does **not**.

That is the drift these tests exist to catch. They run real Alembic in
subprocesses against throwaway data directories, then compare the resulting
schemas **structurally** — columns, types, indexes, foreign keys and triggers —
not merely table names.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

pytest.importorskip("alembic", reason="alembic is a dev-only operational dependency")

#: Tables TSB-366/367 promise, regardless of which bootstrap path built them.
EXPECTED_TABLES = {
    "storyboards",
    "storyboard_versions",
    "storyboard_source_refs",
    "legacy_plan_migrations",
    "plan_operations",
}

#: Triggers that carry the invariants.
EXPECTED_TRIGGERS = {
    "trg_storyboard_version_parent_same_board",
    "trg_storyboard_current_version_same_board",
    "trg_storyboard_version_immutable",
    "trg_storyboard_version_restore_target_same_board",
    "trg_plan_operation_base_same_board",
    "trg_plan_operation_status_valid",
    "trg_plan_operation_terminal_immutable",
    "trg_plan_operation_applied_has_result",
}


def _run_backend(args: list[str], *, data_dir: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TRAVELSB_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def _alembic(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return _run_backend(
        ["-m", "alembic", "-c", str(BACKEND / "alembic.ini"), *args],
        data_dir=data_dir,
    )


def _init_db(data_dir: Path) -> subprocess.CompletedProcess:
    return _run_backend(
        ["-c", "from app.database import init_db; init_db()"], data_dir=data_dir,
    )


def _db_path(data_dir: Path) -> Path:
    # One database, one name: whatever the config chooses, both paths must agree.
    candidates = sorted(data_dir.glob("*.db"))
    assert candidates, f"no database created in {data_dir}"
    return candidates[0]


def _schema_fingerprint(db: Path) -> dict:
    """A structural description: tables, columns, types, indexes, FKs, triggers."""
    con = sqlite3.connect(db)
    try:
        tables = {}
        names = [
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        ]
        for name in names:
            columns = {
                r[1]: {"type": (r[2] or "").upper(), "notnull": bool(r[3]),
                       "pk": bool(r[5])}
                for r in con.execute(f"PRAGMA table_info({name})")
            }
            indexes = sorted(
                (r[1], bool(r[2]), tuple(
                    x[2] for x in con.execute(f"PRAGMA index_info({r[1]})")
                ))
                for r in con.execute(f"PRAGMA index_list({name})")
            )
            fks = sorted(
                (r[2], r[3], r[4]) for r in con.execute(f"PRAGMA foreign_key_list({name})")
            )
            tables[name] = {
                "columns": columns,
                "indexes": [list(i) for i in indexes],
                "foreign_keys": [list(f) for f in fks],
            }
        triggers = sorted(
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        )
        return {"tables": tables, "triggers": triggers}
    finally:
        con.close()


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / f"tsb367r1-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# A. fresh database -> alembic upgrade head
# ---------------------------------------------------------------------------


def test_r1_04_a_fresh_alembic_upgrade_creates_every_table(workdir: Path):
    result = _alembic(workdir, "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr

    fp = _schema_fingerprint(_db_path(workdir))
    assert EXPECTED_TABLES <= set(fp["tables"]), (
        f"missing after fresh upgrade: {EXPECTED_TABLES - set(fp['tables'])}"
    )
    assert EXPECTED_TRIGGERS <= set(fp["triggers"]), (
        f"missing triggers after fresh upgrade: {EXPECTED_TRIGGERS - set(fp['triggers'])}"
    )


# ---------------------------------------------------------------------------
# B. historical database (old 0001) -> upgrade head
# ---------------------------------------------------------------------------


def test_r1_04_a_historical_database_reaches_the_same_schema(workdir: Path):
    """Simulate a DB that ran the *old* 0001, i.e. without legacy_plan_migrations."""
    # 1. Build the historical shape: full schema, then drop what old 0001 lacked.
    assert _init_db(workdir).returncode == 0
    db = _db_path(workdir)
    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE IF EXISTS legacy_plan_migrations")
        con.execute("DROP TABLE IF EXISTS plan_operations")
        con.execute("CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)")
        con.execute("DELETE FROM alembic_version")
        con.execute("INSERT INTO alembic_version VALUES ('0001_storyboard_versions')")
        con.commit()
    finally:
        con.close()

    # 2. Upgrade the rest of the way.
    result = _alembic(workdir, "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr

    fp = _schema_fingerprint(db)
    # This is the R1-04 regression: a database stamped at the OLD 0001 never
    # re-runs it, so only a forward revision can add the missing table.
    assert "legacy_plan_migrations" in fp["tables"], (
        "a database stamped at the old 0001 did not gain legacy_plan_migrations"
    )
    assert "plan_operations" in fp["tables"]
    assert EXPECTED_TRIGGERS <= set(fp["triggers"]), (
        f"missing triggers: {EXPECTED_TRIGGERS - set(fp['triggers'])}"
    )


def test_r1_04_upgrading_a_historical_database_preserves_its_data(workdir: Path):
    assert _init_db(workdir).returncode == 0
    db = _db_path(workdir)
    con = sqlite3.connect(db)
    try:
        pid = str(uuid.uuid4())
        con.execute(
            "INSERT INTO projects (id, name, source_roots, output_dir, settings, "
            "created_at, updated_at) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            (pid, "historic", "[]", "", "{}"),
        )
        con.execute("DROP TABLE IF EXISTS legacy_plan_migrations")
        con.execute("DROP TABLE IF EXISTS plan_operations")
        con.execute("CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)")
        con.execute("DELETE FROM alembic_version")
        con.execute("INSERT INTO alembic_version VALUES ('0001_storyboard_versions')")
        con.commit()
    finally:
        con.close()

    result = _alembic(workdir, "upgrade", "head")
    assert result.returncode == 0, result.stdout + result.stderr

    con = sqlite3.connect(db)
    try:
        kept = con.execute("SELECT count(*) FROM projects").fetchone()[0]
    finally:
        con.close()
    assert kept >= 1, "upgrading must not destroy existing rows"


def test_r1_04_upgrading_twice_is_a_noop(workdir: Path):
    assert _alembic(workdir, "upgrade", "head").returncode == 0
    first = _schema_fingerprint(_db_path(workdir))
    again = _alembic(workdir, "upgrade", "head")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _schema_fingerprint(_db_path(workdir)) == first


# ---------------------------------------------------------------------------
# C. both bootstrap paths agree
# ---------------------------------------------------------------------------


def test_r1_04_init_db_and_alembic_produce_the_same_structure(workdir: Path):
    init_dir = workdir / "via-init-db"
    migr_dir = workdir / "via-alembic"
    init_dir.mkdir()
    migr_dir.mkdir()

    assert _init_db(init_dir).returncode == 0
    assert _alembic(migr_dir, "upgrade", "head").returncode == 0

    a = _schema_fingerprint(_db_path(init_dir))
    b = _schema_fingerprint(_db_path(migr_dir))

    # Alembic manages the TSB-366/367 contract; init_db() builds the whole app
    # schema. The contract tables are what must agree.
    assert EXPECTED_TABLES <= set(a["tables"]), f"init_db missing: {EXPECTED_TABLES - set(a['tables'])}"
    assert EXPECTED_TABLES <= set(b["tables"]), f"alembic missing: {EXPECTED_TABLES - set(b['tables'])}"
    assert not (set(b["tables"]) - set(a["tables"])), (
        f"alembic created tables init_db does not know: {set(b['tables']) - set(a['tables'])}"
    )
    assert a["triggers"] == b["triggers"], (
        f"trigger sets differ — init_db only: {set(a['triggers']) - set(b['triggers'])}, "
        f"alembic only: {set(b['triggers']) - set(a['triggers'])}"
    )

    for name in sorted(EXPECTED_TABLES & set(a["tables"]) & set(b["tables"])):
        # Compare column names and types (SQLite is loose about type strings, so a
        # difference here is a real contract difference, not cosmetic).
        cols_a = {k: v["type"] for k, v in a["tables"][name]["columns"].items()}
        cols_b = {k: v["type"] for k, v in b["tables"][name]["columns"].items()}
        assert cols_a == cols_b, f"{name}: columns differ\n init_db={cols_a}\n alembic={cols_b}"
        assert (
            a["tables"][name]["foreign_keys"] == b["tables"][name]["foreign_keys"]
        ), f"{name}: foreign keys differ"


def test_r1_04_the_shared_indexes_exist_on_both_paths(workdir: Path):
    init_dir = workdir / "i"
    migr_dir = workdir / "m"
    init_dir.mkdir()
    migr_dir.mkdir()
    assert _init_db(init_dir).returncode == 0
    assert _alembic(migr_dir, "upgrade", "head").returncode == 0

    def indexes_for(db: Path, table: str) -> set[str]:
        con = sqlite3.connect(db)
        try:
            return {r[1] for r in con.execute(f"PRAGMA index_list({table})")}
        finally:
            con.close()

    for table in ("plan_operations", "storyboard_versions"):
        a = indexes_for(_db_path(init_dir), table)
        b = indexes_for(_db_path(migr_dir), table)
        # Partial/unique indexes that encode invariants must be on both paths.
        important = {i for i in (a | b) if i.startswith(("uq_", "ix_"))}
        assert important <= a, f"{table}: init_db missing {important - a}"
        assert important <= b, f"{table}: alembic missing {important - b}"


# ---------------------------------------------------------------------------
# downgrade contract
# ---------------------------------------------------------------------------


def test_r1_04_downgrade_to_base_leaves_no_storyboard_tables(workdir: Path):
    assert _alembic(workdir, "upgrade", "head").returncode == 0
    result = _alembic(workdir, "downgrade", "base")
    assert result.returncode == 0, result.stdout + result.stderr

    fp = _schema_fingerprint(_db_path(workdir))
    assert not (EXPECTED_TABLES & set(fp["tables"])), (
        f"downgrade left tables behind: {EXPECTED_TABLES & set(fp['tables'])}"
    )


def test_r1_04_downgrade_never_drops_the_legacy_plan_table(workdir: Path):
    """The pre-TSB-366 plan table is user data and must survive a downgrade."""
    assert _init_db(workdir).returncode == 0
    db = _db_path(workdir)
    con = sqlite3.connect(db)
    try:
        pid = str(uuid.uuid4())
        con.execute(
            "INSERT INTO projects (id, name, source_roots, output_dir, settings, "
            "created_at, updated_at) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            (pid, "keepme", "[]", "", "{}"),
        )
        con.commit()
    finally:
        con.close()
    assert _alembic(workdir, "stamp", "head").returncode == 0
    assert _alembic(workdir, "downgrade", "base").returncode == 0

    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows = con.execute("SELECT count(*) FROM projects").fetchone()[0]
    finally:
        con.close()
    assert "edit_plan_shots" in tables, "downgrade must not drop the legacy plan table"
    assert rows >= 1
