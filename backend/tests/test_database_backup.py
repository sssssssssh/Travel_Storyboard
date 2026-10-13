"""WAL-aware backup and legacy detected-zero preservation."""
import sqlite3
from types import SimpleNamespace
from sqlalchemy import text
from app import database
from app.database import session_scope
from app.models import Asset, Segment


def test_backup_includes_committed_wal_rows(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    conn = sqlite3.connect(path)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('CREATE TABLE sample (value TEXT)')
        conn.execute("INSERT INTO sample VALUES ('committed in WAL')")
        conn.commit()
        monkeypatch.setattr(database, '_settings', SimpleNamespace(resolved_database_url=f'sqlite:///{path}'))
        backup = database._backup_sqlite_file()
        assert backup
        with sqlite3.connect(backup) as copy:
            assert copy.execute('SELECT value FROM sample').fetchone()[0] == 'committed in WAL'
    finally:
        conn.close()


def test_unrelated_migration_does_not_change_detected_zero(client):
    pid = client.post('/api/projects', json={'name': 'legacy-zero'}).json()['id']
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path='/unused.mp4'); s.add(asset); s.flush()
        segment = Segment(project_id=pid, asset_id=asset.id, start_sec=2, end_sec=8,
                          detected_start_sec=0, detected_end_sec=10, is_trimmed=True)
        s.add(segment); s.flush(); sid = segment.id
    database._legacy_backfill()
    with session_scope() as s:
        assert s.get(Segment, sid).detected_start_sec == 0
