"""TSB-314: observable API continuity, append-only judgement and WAL migration."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
import sqlite3

import pytest
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app import database
from app.database import session_scope
from app.models import Asset, Job, Project, Segment, SegmentMotionReview
from app.services.vision import motion


@pytest.fixture
def shot(client, tmp_path):
    pid = client.post('/api/projects', json={'name': 'T314'}).json()['id']
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'original-read-only-source')
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path=str(source), scan_status='succeeded')
        s.add(asset); s.flush()
        seg = Segment(project_id=pid, asset_id=asset.id, start_sec=1, end_sec=5,
                      detected_start_sec=0, detected_end_sec=6, user_rating='favorite',
                      story_role='opening')
        s.add(seg); s.flush()
        target = {'segment_id': seg.id, 'asset_id': asset.id, 'start_sec': 1., 'end_sec': 5.}
    frames = []
    for i in range(2):
        path = tmp_path / f'{i}.jpg'
        Image.new('RGB', (16, 16), (i * 70, 30, 50)).save(path)
        frames.append(motion.MotionFrame(str(path), float(i + 2)))
    return pid, target, frames, source


def store(shot, **changes):
    pid, target, frames, _ = shot
    result = {'motion_type': 'pan', 'stability_risk': 'low', 'action_summary': 'lake view',
              'evidence_timestamps_sec': [2., 3.], 'limitations': [], 'needs_human_review': False}
    result.update(changes)
    # Snapshot production selection, rather than inventing source identity.
    snap = motion._select_targets(pid, [target['segment_id']], 1)[0]
    with session_scope() as s:
        job = Job(project_id=pid, job_type='motion_review', status='running')
        s.add(job); s.flush(); jid = job.id
    try:
        motion._store(pid, snap, result, frames, provider_name='stub', model_id='stub', usage={}, job_id=jid)
    finally:
        with session_scope() as s:
            s.get(Job, jid).status = 'succeeded'


def detail(client, shot):
    res = client.get(f"/api/segments/{shot[1]['segment_id']}/motion-review")
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize('verdict', ['confirm', 'reject', 'unclear'])
def test_same_evidence_rerun_keeps_current_api_verdict(client, shot, verdict):
    store(shot)
    sid = shot[1]['segment_id']
    first = client.post(f'/api/segments/{sid}/motion-review/verdict',
                        json={'verdict': verdict, 'note': 'human note survives'}).json()
    store(shot)
    current = detail(client, shot)
    assert current['human_verdict'] == verdict
    assert current['human_state'] == 'confirmed_current'
    assert current['human_note'] == 'human note survives'
    assert current['human_verdict_id'] == first['human_verdict_id']
    assert current['id'] != first['id']
    listed = client.get(f'/api/projects/{shot[0]}/motion-reviews').json()
    assert listed == [current]
    assert len(current['human_history']) == 1
    with session_scope() as s:
        seg = s.get(Segment, sid)
        assert (seg.start_sec, seg.end_sec, seg.detected_start_sec, seg.user_rating, seg.story_role) == (1, 5, 0, 'favorite', 'opening')


@pytest.mark.parametrize('change', ['trim', 'source', 'bytes', 'conclusion', 'signature'])
def test_changed_evidence_requires_reconfirmation_and_retains_history(client, shot, change):
    store(shot)
    sid = shot[1]['segment_id']
    assert motion.record_human_verdict(sid, 'reject', 'keep history')
    if change == 'trim':
        with session_scope() as s:
            s.get(Segment, sid).start_sec = 1.5
    elif change == 'source':
        shot[3].write_bytes(b'replaced-source-same-segment-id')
    elif change == 'bytes':
        Image.new('RGB', (16, 16), 'white').save(shot[2][0].path)
    elif change == 'signature':
        with session_scope() as s:
            s.get(Asset, shot[1]['asset_id']).analyzed_source_signature = 'changed'
    # Validity must also be recomputed on read, even before another model run.
    if change in ('trim', 'source', 'signature'):
        assert detail(client, shot)['human_state'] == 'needs_reconfirmation'
    store(shot, **({'motion_type': 'static'} if change == 'conclusion' else {}))
    current = detail(client, shot)
    assert current['human_state'] == 'needs_reconfirmation'
    assert current['human_verdict'] == ''  # compatibility must not advertise stale judgement
    assert current['human_invalid_reason']
    assert current['human_history'][0]['verdict'] == 'reject'
    assert current['human_history'][0]['note'] == 'keep history'
    assert current['human_history'][0]['is_effective'] is False
    assert motion.record_human_verdict(sid, 'unclear', 'new evidence checked')
    assert detail(client, shot)['human_state'] == 'confirmed_current'


def test_stale_page_verdict_is_409_without_rebinding_to_new_result(client, shot):
    store(shot)
    old = detail(client, shot)
    store(shot, motion_type='tilt')
    sid = shot[1]['segment_id']
    res = client.post(f'/api/segments/{sid}/motion-review/verdict', json={
        'verdict': 'confirm', 'motion_review_id': old['id'],
        'input_signature': old.get('input_signature', '')})
    assert res.status_code == 409
    assert detail(client, shot)['human_state'] == 'not_reviewed'


def test_concurrent_confirmations_append_history_last_commit_is_effective(client, shot):
    store(shot)
    gate = Barrier(2)
    sid = shot[1]['segment_id']
    def confirm(value):
        gate.wait(timeout=10)
        return motion.record_human_verdict(sid, value, value + '-note')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, v) for v in ('confirm', 'reject')]
        assert all(f.result(timeout=15) for f in futures)
    out = detail(client, shot)
    assert out['human_state'] == 'confirmed_current'
    assert len(out['human_history']) == 2
    assert sum(h['is_effective'] for h in out['human_history']) == 1
    assert out['human_history'][0]['id'] == out['human_verdict_id']
    assert {h['note'] for h in out['human_history']} == {'confirm-note', 'reject-note'}
    history = client.get(f'/api/segments/{sid}/motion-review/verdicts?limit=1&offset=1')
    assert history.status_code == 200
    assert history.json()['count'] == 1
    assert history.json()['items'][0] == out['human_history'][1]


def test_old_database_is_backed_up_before_create_all_and_backfill_is_once(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.db'
    eng = create_engine(f'sqlite:///{path}')
    # Exclude the future audit table, reproducing the exact old schema.
    tables = [t for t in database.Base.metadata.sorted_tables if t.name != 'segment_motion_verdicts']
    database.Base.metadata.create_all(eng, tables=tables)
    with Session(eng) as s:
        p = Project(name='retain'); s.add(p); s.flush()
        a = Asset(project_id=p.id, source_path='/not-read-by-migration'); s.add(a); s.flush()
        seg = Segment(project_id=p.id, asset_id=a.id, start_sec=2, end_sec=6, user_rating='favorite')
        s.add(seg); s.flush()
        row = SegmentMotionReview(project_id=p.id, asset_id=a.id, segment_id=seg.id,
                                  human_verdict='confirm', human_note='legacy human note')
        s.add(row); s.commit(); rid = row.id
    with eng.begin() as c:
        if 'input_signature' in {r[1] for r in c.execute(text('PRAGMA table_info(segment_motion_reviews)'))}:
            c.execute(text('ALTER TABLE segment_motion_reviews DROP COLUMN input_signature'))
    wal = sqlite3.connect(path)
    wal.execute('PRAGMA journal_mode=WAL'); wal.execute('PRAGMA wal_autocheckpoint=0')
    wal.execute("UPDATE segment_motion_reviews SET human_note='committed WAL note'"); wal.commit()
    monkeypatch.setattr(database, 'engine', eng)
    monkeypatch.setattr(database, '_settings', SimpleNamespace(resolved_database_url=f'sqlite:///{path}'))
    try:
        database.init_db()
        backups = list(tmp_path.glob('*.bak'))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as copy:
            assert copy.execute('SELECT human_note FROM segment_motion_reviews').fetchone()[0] == 'committed WAL note'
            assert copy.execute("SELECT count(*) FROM sqlite_master WHERE name='segment_motion_verdicts'").fetchone()[0] == 0
        with eng.connect() as c:
            rows = c.execute(text('SELECT * FROM segment_motion_verdicts')).mappings().all()
            assert len(rows) == 1
            assert rows[0]['motion_review_id'] == rid
            assert rows[0]['note'] == 'committed WAL note'
            assert rows[0]['input_signature'] == ''  # unknown legacy evidence stays unknown
        database.init_db()
        assert list(tmp_path.glob('*.bak')) == backups
        with eng.connect() as c:
            assert c.execute(text('SELECT count(*) FROM segment_motion_verdicts')).scalar_one() == 1
            assert c.execute(text('SELECT user_rating FROM segments')).scalar_one() == 'favorite'
    finally:
        wal.close(); eng.dispose()


def test_backup_failure_blocks_schema_creation(tmp_path, monkeypatch):
    path = tmp_path / 'backup-failure.db'
    eng = create_engine(f'sqlite:///{path}')
    with eng.begin() as c:
        c.execute(text('CREATE TABLE untouched (id TEXT)'))
    monkeypatch.setattr(database, 'engine', eng)
    monkeypatch.setattr(database, '_settings', SimpleNamespace(resolved_database_url=f'sqlite:///{path}'))
    def fail():
        raise OSError('backup unavailable')
    monkeypatch.setattr(database, '_backup_sqlite_file', fail)
    try:
        with pytest.raises(OSError, match='backup unavailable'):
            database.init_db()
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM sqlite_master WHERE name='segment_motion_reviews'")).scalar_one() == 0
    finally:
        eng.dispose()
