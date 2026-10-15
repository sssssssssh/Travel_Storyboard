"""TSB-319 local migration audit. Stop the app first; run from backend/.

Takes a WAL-consistent backup, applies additive migrations only with --apply,
compares all old columns/rows, and rehearses restoring INTO A THROWAWAY COPY.
Never prints data values, credentials, source paths, or an absolute backup path.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(connection, columns=None):
    if columns is None:
        tables = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        columns = {name: [r[1] for r in connection.execute('PRAGMA table_info(' + quote(name) + ')')]
                   for name in tables}
    result = {}
    for name, names in columns.items():
        values = ['SELECT ' + ','.join(map(quote, names)) + ' FROM ' + quote(name)]
        rows = sorted(json.dumps(list(row), ensure_ascii=False, default=str, separators=(',', ':'))
                      for row in connection.execute(values[0]))
        result[name] = {'rows': len(rows), 'sha256': hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
    return columns, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    from app import database
    try:
        path = database.engine.url.database
        if not path or path == ':memory:' or not Path(path).is_file():
            raise ValueError('DATABASE_NOT_FOUND')
        with sqlite3.connect(path) as source:
            tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'jobs' in tables and source.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('pending','running','cancelling')").fetchone()[0]:
                raise ValueError('ACTIVE_JOBS_PRESENT')
        backup = database._backup_sqlite_file()
        if not backup:
            raise ValueError('BACKUP_UNAVAILABLE')
        with sqlite3.connect(backup) as before:
            columns, baseline = snapshot(before)
        if args.apply:
            database.init_db()
        with sqlite3.connect(path) as after:
            _, actual = snapshot(after, columns)
            all_columns, _ = snapshot(after)
            integrity = after.execute('PRAGMA integrity_check').fetchone()[0]
        with tempfile.TemporaryDirectory(prefix='tsb-restore-audit-') as temp:
            restored = Path(temp) / 'restore-copy.db'
            with sqlite3.connect(backup) as original, sqlite3.connect(restored) as dest:
                original.backup(dest)
                _, restored_state = snapshot(dest, columns)
                restore_integrity = dest.execute('PRAGMA integrity_check').fetchone()[0]
        ok = actual == baseline == restored_state and integrity == restore_integrity == 'ok'
        report = {'ok': ok, 'applied': args.apply, 'backup_reference': Path(backup).name,
                  'backup_location': 'beside the configured SQLite file; never uploaded',
                  'all_old_rows_and_columns_unchanged': actual == baseline,
                  'restore_copy_matches_before': restored_state == baseline,
                  'integrity_check': integrity, 'restore_integrity_check': restore_integrity,
                  'added_tables': sorted(set(all_columns) - set(columns)),
                  'before': baseline, 'after_old_column_projection': actual,
                  'source_videos_read_or_written': False}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else 'DB_AUDIT_FAILED'
        print(json.dumps({'ok': False, 'error_code': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
