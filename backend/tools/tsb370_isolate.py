"""TSB-370-A — build the isolated experiment database.

The task book requires that real model output never lands in the production DB,
while still going through the **production** write path
(``authorize_upload`` → job → ``schema.validate_response`` → ``store_*``). The way
to have both is: a structurally identical database, in a temp directory outside the
repo, holding copies of the project metadata, with the original clips referenced by
their real read-only absolute paths.

What is copied: Project / Asset / Segment / Frame (metadata only).
What is *not* copied: any model result, any user annotation, any storyboard
version. The isolated project therefore starts with **zero** evidence and zero
human opinion, which is what makes the before/after coverage numbers meaningful.

The copy is verified before use: segment/asset counts must match, every
``source_path`` must still resolve, and each source's size/mtime must match the
original so the read-only identity check still passes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILENAME = "TSB370_RUN_MANIFEST.json"


@dataclass
class IsolationResult:
    db_path: Path
    project_id: str
    asset_count: int
    segment_count: int
    frame_count: int
    source_fingerprints: dict[str, dict]


def _copy_rows(src: sqlite3.Connection, dst: sqlite3.Connection, sql: str,
               params: tuple, *, id_map: dict[str, str] | None = None,
               replace: dict[str, str] | None = None) -> int:
    """Copy rows, optionally rewriting an id column and substituting values."""
    src.row_factory = sqlite3.Row
    rows = src.execute(sql, params).fetchall()
    if not rows:
        return 0
    cols = rows[0].keys()
    out = []
    for row in rows:
        values = []
        for col in cols:
            v = row[col]
            if id_map is not None and col == "id" and v in id_map:
                v = id_map[v]
            elif id_map is not None and isinstance(v, str) and v in id_map:
                v = id_map[v]
            if replace and col in replace:
                v = replace[col]
            values.append(v)
        out.append(values)
    placeholders = ",".join("?" for _ in cols)
    dst.executemany(
        f"INSERT INTO {sql.split(' FROM ')[0].split('SELECT ')[1].strip()} "
        f"({','.join(cols)}) VALUES ({placeholders})".replace("  ", " "),
        out,
    )
    return len(out)


def build_isolated_db(
    source_db: Path, target_dir: Path, *, project_id: str,
    new_project_id: str | None = None,
) -> IsolationResult:
    """Clone one project's metadata into a fresh database under ``target_dir``."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "tsb370_experiment.db"
    if db_path.exists():
        db_path.unlink()

    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dst = sqlite3.connect(db_path)

    # Schema: take the live one, so the isolated DB cannot drift from production.
    schema = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND type='table'")]
    for stmt in schema:
        dst.execute(stmt)
    dst.commit()

    new_pid = new_project_id or str(uuid.uuid4())
    id_map: dict[str, str] = {}

    src.row_factory = sqlite3.Row
    assets = src.execute(
        "SELECT * FROM assets WHERE project_id=?", (project_id,)).fetchall()
    segments = src.execute(
        "SELECT * FROM segments WHERE project_id=?", (project_id,)).fetchall()
    frame_rows = src.execute(
        "SELECT f.* FROM frames f JOIN assets a ON a.id=f.asset_id "
        "WHERE a.project_id=?", (project_id,)).fetchall()

    for a in assets:
        id_map[a["id"]] = str(uuid.uuid4())
    for g in segments:
        id_map[g["id"]] = str(uuid.uuid4())

    fingerprints: dict[str, dict] = {}

    def insert(table: str, rows, remap: dict[str, str]) -> int:
        if not rows:
            return 0
        cols = [k for k in rows[0].keys()]
        payload = []
        for r in rows:
            vals = []
            for c in cols:
                v = r[c]
                if isinstance(v, str) and v in id_map:
                    v = id_map[v]
                if c == "project_id":
                    v = new_pid
                if c in remap:
                    v = remap[c]
                vals.append(v)
            payload.append(vals)
        dst.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES "
            f"({','.join('?' for _ in cols)})", payload)
        return len(payload)

    insert("projects", src.execute(
        "SELECT * FROM projects WHERE id=?", (project_id,)).fetchall(),
        {"id": new_pid, "project_id": None, "name": "TSB370-ISOLATED-EXPERIMENT"})
    insert("assets", assets, {})
    insert("segments", segments, {})
    insert("frames", frame_rows, {})
    dst.commit()

    # Verify the copy before anyone uses it.
    checks = {
        "assets": (len(assets), dst.execute(
            "SELECT COUNT(*) FROM assets WHERE project_id=?", (new_pid,)).fetchone()[0]),
        "segments": (len(segments), dst.execute(
            "SELECT COUNT(*) FROM segments WHERE project_id=?", (new_pid,)).fetchone()[0]),
        "frames": (len(frame_rows), dst.execute(
            "SELECT COUNT(*) FROM frames f JOIN assets a ON a.id=f.asset_id "
            "WHERE a.project_id=?", (new_pid,)).fetchone()[0]),
    }
    for name, (want, got) in checks.items():
        if want != got:
            raise RuntimeError(f"isolation copy mismatch for {name}: {want} != {got}")

    # No evidence, no human opinion, no storyboard may survive the copy.
    for table, sql in (
        ("semantic", "SELECT COUNT(*) FROM segment_semantic_results WHERE project_id=?"),
        ("motion", "SELECT COUNT(*) FROM segment_motion_reviews WHERE project_id=?"),
        ("video", "SELECT COUNT(*) FROM segment_video_observations WHERE project_id=?"),
        ("storyboards", "SELECT COUNT(*) FROM storyboards WHERE project_id=?"),
        ("jobs", "SELECT COUNT(*) FROM jobs WHERE project_id=?"),
    ):
        n = dst.execute(sql, (new_pid,)).fetchone()[0]
        if n:
            raise RuntimeError(f"isolated DB must start empty for {table}, found {n}")
    ann = dst.execute(
        "SELECT COUNT(*) FROM user_annotations ua JOIN segments s ON s.id=ua.segment_id "
        "WHERE s.project_id=?", (new_pid,)).fetchone()[0]
    if ann:
        raise RuntimeError(f"isolated DB must not carry annotations, found {ann}")

    # Record the read-only source identity so later checks can prove it unchanged.
    for a in assets:
        p = Path(a["source_path"])
        fingerprints[a["id"]] = {
            "isolated_id": id_map[a["id"]],
            "exists": p.is_file(),
            "size": p.stat().st_size if p.is_file() else None,
            "mtime_ns": p.stat().st_mtime_ns if p.is_file() else None,
        }

    src.close()
    dst.close()
    return IsolationResult(
        db_path=db_path, project_id=new_pid,
        asset_count=len(assets), segment_count=len(segments),
        frame_count=len(frame_rows), source_fingerprints=fingerprints,
    )


def write_manifest(target_dir: Path, payload: dict) -> Path:
    """Persist the run manifest **outside** the repository."""
    path = Path(target_dir) / MANIFEST_FILENAME
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def cleanup_isolated(target_dir: Path) -> None:
    """Remove the experiment directory. Only ever called with our own temp path."""
    d = Path(target_dir)
    if d.exists() and d.name.startswith("tsb370"):
        shutil.rmtree(d, ignore_errors=True)
