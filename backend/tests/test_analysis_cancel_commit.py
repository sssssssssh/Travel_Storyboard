"""RT273: cancellation committed before BEGIN IMMEDIATE forbids publication."""
import pytest
from sqlalchemy import select
from app.database import session_scope
from app.models import Job, Segment, Frame
from app.services import analyzer
from app.services.runner import JobCancelled
from test_concurrent_annotations import seeded


def test_cancel_lands_after_last_poll_before_write_transaction(client, seeded, monkeypatch):
    pid, asset, sid, fid, old_path, _ = seeded
    old_bytes = old_path.read_bytes()
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status='running')
        s.add(job); s.flush(); jid = job.id
    original = analyzer.begin_write
    def cancel_then_lock(session):
        assert client.post(f'/api/jobs/{jid}/cancel').json()['status'] == 'cancelling'
        original(session)
    monkeypatch.setattr(analyzer, 'begin_write', cancel_then_lock)
    with pytest.raises(JobCancelled):
        analyzer._analyze_asset(pid, asset, job_id=jid)
    with session_scope() as s:
        assert s.get(Frame, fid).image_path
        assert len(s.scalars(select(Frame).where(Frame.asset_id == asset.id)).all()) == 1
        assert len(s.scalars(select(Segment).where(Segment.asset_id == asset.id)).all()) == 1
    assert old_path.read_bytes() == old_bytes
    assert list(old_path.parent.glob('*.jpg')) == [old_path]

from app.models import Asset, UserAnnotation, ReviewRequest, EditPlanShot
from app.services import runner as r
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from sqlalchemy import event
from app.database import engine


@pytest.mark.parametrize('state', ['cancelling', 'cancelled'])
def test_transaction_cancellation_preserves_all_manual_history(client, seeded, monkeypatch, state):
    pid, asset, sid, fid, old_path, _ = seeded
    with session_scope() as s:
        row = s.get(Segment, sid)
        row.user_rating, row.story_role, row.is_trimmed = 'favorite', '开场', True
        row.start_sec, row.end_sec = 2, 8
        s.add_all([UserAnnotation(segment_id=sid, note='must preserve'),
                   ReviewRequest(project_id=pid, asset_id=asset.id, segment_id=sid),
                   EditPlanShot(project_id=pid, asset_id=asset.id, segment_id=sid, order=0, source_in_sec=2, source_out_sec=8, target_duration_sec=6)])
        job = Job(project_id=pid, job_type='detect', status='running')
        s.add(job); s.flush(); jid = job.id
    original = analyzer.begin_write
    def set_cancel(session):
        assert r.update_job(jid, status=state)
        original(session)
    monkeypatch.setattr(analyzer, 'begin_write', set_cancel)
    with pytest.raises(JobCancelled): analyzer._analyze_asset(pid, asset, job_id=jid)
    with session_scope() as s:
        row = s.get(Segment, sid)
        assert (row.user_rating, row.story_role, row.is_trimmed, row.start_sec, row.end_sec) == ('favorite', '开场', True, 2, 8)
        assert s.scalars(select(UserAnnotation).where(UserAnnotation.segment_id == sid)).one().note == 'must preserve'
        assert s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).one().segment_id == sid
        assert s.scalars(select(EditPlanShot).where(EditPlanShot.project_id == pid)).one().segment_id == sid
        assert s.get(Frame, fid) is not None and s.get(Asset, asset.id).analysis_version == asset.analysis_version
    assert old_path.is_file()


def test_analysis_writer_commits_before_queued_cancel_is_accepted(client, seeded, monkeypatch):
    pid, asset, sid, fid, old_path, _ = seeded
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status='pending')
        s.add(job); s.flush(); jid = job.id
    attempted = Event()
    def before_sql(conn, cursor, statement, params, context, many):
        if statement.startswith('UPDATE jobs SET status=') and 'cancelling' in params:
            attempted.set()
    original = analyzer.begin_write
    with ThreadPoolExecutor(1) as pool:
        pending = []
        def lock_then_queue(session):
            original(session)
            pending.append(pool.submit(client.post, f'/api/jobs/{jid}/cancel'))
            assert attempted.wait(10)
            assert not pending[0].done(), 'cancel cannot be accepted while analysis owns SQLite writer'
        monkeypatch.setattr(analyzer, 'begin_write', lock_then_queue)
        event.listen(engine, 'before_cursor_execute', before_sql)
        try:
            def task(_):
                analyzer._analyze_asset(pid, asset, job_id=jid)
                assert pending[0].result(10).status_code == 200
            r.JobRunner()._run(jid, task)
        finally: event.remove(engine, 'before_cursor_execute', before_sql)
    with session_scope() as s:
        assert s.get(Job, jid).status == 'cancelled'
        assert s.get(Asset, asset.id).analysis_version
        frames = s.scalars(select(Frame).where(Frame.asset_id == asset.id)).all()
        assert frames and all(analyzer.resolve_in_project(pid, f.image_path).is_file() for f in frames)
        assert s.get(Frame, fid) is None  # prior version replaced BEFORE cancellation was accepted


def test_cancel_after_new_jpeg_publish_reclaims_only_uncommitted_files(client, seeded, monkeypatch):
    pid, asset, _, fid, old_path, _ = seeded
    before = old_path.read_bytes()
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status='running')
        s.add(job); s.flush(); jid = job.id
    original = analyzer.safe_replace
    def publish_then_cancel(src, dst):
        original(src, dst)
        assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
    monkeypatch.setattr(analyzer, 'safe_replace', publish_then_cancel)
    with pytest.raises(JobCancelled): analyzer._analyze_asset(pid, asset, job_id=jid)
    assert old_path.read_bytes() == before
    assert list(old_path.parent.glob('*.jpg')) == [old_path]
    with session_scope() as s: assert s.get(Frame, fid) is not None


def test_cancel_and_source_change_keep_error_classification_visible(client, seeded, monkeypatch):
    pid, asset, _, fid, old, _ = seeded
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status='pending')
        s.add(job); s.flush(); jid = job.id
    original = analyzer.extract_frame
    def fault(*args, **kw):
        original(*args, **kw)
        assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
        raise RuntimeError('SOURCE_CHANGED: injected test-copy failure')
    monkeypatch.setattr(analyzer, 'extract_frame', fault)
    r.JobRunner()._run(jid, lambda _: analyzer._analyze_asset(pid, asset, job_id=jid))
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'cancelled' and 'SOURCE_CHANGED' in row.error
        assert s.get(Frame, fid) is not None
    assert old.is_file()
