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


def test_review_lineage_migration_backs_up_and_is_idempotent(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    path = tmp_path / 'legacy-review.db'
    legacy = sqlite3.connect(path)
    legacy.execute('CREATE TABLE review_requests (id TEXT PRIMARY KEY, proxy_ref TEXT, reason TEXT)')
    legacy.execute("INSERT INTO review_requests VALUES ('old', 'historic.mp4', 'retain reason')")
    legacy.commit(); legacy.close()
    url = f'sqlite:///{path}'
    engine = create_engine(url)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, '_settings', SimpleNamespace(resolved_database_url=url))
    try:
        database.init_db()
        with engine.connect() as connection:
            row = connection.execute(text('SELECT * FROM review_requests')).mappings().one()
            assert row['proxy_ref'] == 'historic.mp4' and row['reason'] == 'retain reason'
            assert row['supersedes_request_id'] == row['superseded_by_request_id'] == ''
            assert row['disposition_at'] is None
        backups = list(tmp_path.glob('legacy-review.pre-migration-*.bak'))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as copy:
            assert copy.execute('SELECT * FROM review_requests').fetchone() == ('old', 'historic.mp4', 'retain reason')
        database.init_db()
        assert list(tmp_path.glob('legacy-review.pre-migration-*.bak')) == backups
        assert database._apply_migrations() == []
    finally:
        engine.dispose()



def test_legacy_full_detection_recovers_lightweight_source_signature_without_reading_source(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.models import Job, Project
    path = tmp_path / 'legacy-full.db'
    engine = create_engine(f'sqlite:///{path}')
    database.Base.metadata.create_all(engine)
    with Session(engine) as s:
        project = Project(name='legacy full'); s.add(project); s.flush()
        asset = Asset(project_id=project.id, source_path='/not-available.mp4',
                      analyzed_fingerprint='full-digest', analyzed_source_signature='old')
        s.add(asset); s.flush(); aid = asset.id
        s.add(Job(project_id=project.id, job_type='detect', status='succeeded', resume_token={
            'items': [{'key': aid, 'scan_signature': 'full-digest', 'quick_signature': 'detected-quick-digest'}]}))
        s.commit()
    with engine.begin() as c: c.execute(text('ALTER TABLE assets DROP COLUMN analyzed_source_signature'))
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, '_settings', SimpleNamespace(resolved_database_url=f'sqlite:///{path}'))
    try:
        database.init_db()
        with engine.connect() as c:
            assert c.execute(select(Asset.analyzed_source_signature).where(Asset.id == aid)).scalar_one() == 'detected-quick-digest'
        assert len(list(tmp_path.glob('*.bak'))) == 1
        database.init_db()
        assert len(list(tmp_path.glob('*.bak'))) == 1
    finally:
        engine.dispose()
