"""TSB-332 — produce PROTECTION_REPORT.json with measured, not asserted, facts.

The task book requires evidence that this batch did not damage anything: originals
untouched, the production database intact, and the pre-existing result families
unchanged. "We did not write to those paths" is a claim; this script measures.

It records, for the real footage directory and the live database:

* per-file size, mtime and SHA-256 of every original video;
* the production database's size, mtime and per-table row counts;
* which tables this batch *adds* (the new observation table) versus which it must
  not alter;
* the count of pre-existing human verdicts, so a later run can prove none were
  cleared.

Run:  python tools/protection_report.py --json ../docs/validation/v2.1.3/PROTECTION_REPORT.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

#: Real DJI footage used by the earlier M5 acceptance runs. Read-only, and this
#: script only ever opens it for reading.
DEFAULT_FOOTAGE = Path(r"F:\work\project\video")

#: Tables introduced by this batch (TSB-328). Everything else must be untouched.
TABLES_ADDED_BY_THIS_BATCH = {"segment_video_observations"}

#: Tables whose contents carry human decisions or model history that must survive.
TABLES_THAT_MUST_NOT_CHANGE = (
    "segments",
    "assets",
    "projects",
    "segment_semantic_results",
    "segment_motion_reviews",
    "segment_motion_verdicts",
    "edit_plan_shots",
    "review_requests",
)

#: How much of a large source to hash. Full-file hashing of ~4.7 GB across six
#: clips takes minutes and the point here is detection of modification, so a
#: head+tail sample plus size and mtime is proportionate. The sampling strategy is
#: recorded in the report so nobody mistakes it for a full digest.
SAMPLE_BYTES = 4 * 1024 * 1024


def sampled_sha256(path: Path, sample: int = SAMPLE_BYTES) -> dict:
    """Hash the first and last ``sample`` bytes, recording the strategy used."""
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(min(sample, size)))
        if size > sample * 2:
            handle.seek(-sample, 2)
            digest.update(handle.read(sample))
    return {
        "sampled_sha256": digest.hexdigest(),
        "sample_bytes_each_end": min(sample, size),
        "digest_scope": "first+last sample only (not a full-file digest)",
    }


def describe_footage(root: Path) -> dict:
    if not root.is_dir():
        return {"present": False, "path_name": root.name, "files": []}
    files = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        stat = path.stat()
        entry = {
            "name": path.name,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
        try:
            entry.update(sampled_sha256(path))
        except OSError as exc:
            entry["error"] = str(exc)[:200]
        files.append(entry)
    return {
        "present": True,
        # Only the directory *name* goes in the report: the full path is not needed
        # as evidence and would leak the operator's filesystem layout.
        "path_name": root.name,
        "file_count": len(files),
        "total_bytes": sum(f["size_bytes"] for f in files),
        "files": files,
    }


def describe_database(path: Path) -> dict:
    if not path.is_file():
        return {"present": False, "name": path.name}
    stat = path.stat()
    out = {
        "present": True,
        "name": path.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "tables": {},
        "row_counts": {},
    }
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            out["tables"] = tables
            for table in tables:
                if table.startswith("sqlite_"):
                    continue
                try:
                    out["row_counts"][table] = conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                except sqlite3.Error as exc:
                    out["row_counts"][table] = f"unreadable: {exc}"
    except sqlite3.Error as exc:
        out["error"] = str(exc)[:300]
    return out


def build_report(*, footage_root: Path, db_path: Path) -> dict:
    footage = describe_footage(footage_root)
    database = describe_database(db_path)

    present_tables = set(database.get("tables") or [])
    counts = database.get("row_counts") or {}

    return {
        "task": "TSB-332",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": (
            "Originals and the production database are opened read-only. Footage "
            "is identified by size, mtime and a sampled SHA-256 (first+last 4 MiB); "
            "a full-file digest is NOT used and is not claimed."
        ),
        "originals": {
            **footage,
            "opened_read_only": True,
            "note": (
                "任何派生只写入项目工作区的 staging，绝不写回素材目录（C1）"
            ),
        },
        "database": {
            **database,
            "tables_added_by_this_batch": sorted(
                present_tables & TABLES_ADDED_BY_THIS_BATCH
            ),
            "tables_that_must_not_change": list(TABLES_THAT_MUST_NOT_CHANGE),
        },
        "human_state_preserved": {
            table: counts.get(table, "absent")
            for table in TABLES_THAT_MUST_NOT_CHANGE
        },
        "guarantees_verified_by_tests": [
            "原片字节与 mtime 在派生前后不变（test_video_clip.py::"
            "test_the_source_file_is_never_modified）",
            "派生片只落在 create_owned_staging 拥有的暂存目录（C7）",
            "写入视频读数不触碰 semantic/motion 行与人工字段（V20，"
            "test_video_observation.py）",
            "升级旧库时新表创建、既有行数据不变（init_db 幂等）",
        ],
        "not_verified": [
            "未在真实 8~10 段多维度素材上执行端到端门禁",
            "未做 60+ 段 / 200GB 全量性能验证",
            "本报告是**本机现状快照**，用于事后比对；它本身不证明任何操作安全",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the TSB-332 protection report")
    parser.add_argument("--footage", type=Path, default=DEFAULT_FOOTAGE)
    parser.add_argument("--db", type=Path, default=BACKEND / "data" / "travel_storyboard.db")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    report = build_report(footage_root=args.footage, db_path=args.db)

    originals = report["originals"]
    print("== protection report ==")
    print(f"  footage present : {originals.get('present')}")
    if originals.get("present"):
        print(f"  files           : {originals['file_count']} "
              f"({originals['total_bytes'] / 1e9:.2f} GB)")
    database = report["database"]
    print(f"  database present: {database.get('present')}")
    if database.get("present"):
        print(f"  size            : {database['size_bytes']} bytes")
        print(f"  tables          : {len(database.get('tables') or [])}")
        print(f"  added by batch  : {database.get('tables_added_by_this_batch')}")
    print("  human-state tables:")
    for table, count in report["human_state_preserved"].items():
        print(f"    {table:32} {count}")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nreport written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
