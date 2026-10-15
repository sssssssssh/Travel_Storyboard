"""TSB-316: Event barriers after provider return and at the writer boundary."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, Segment, SegmentSemanticResult, SegmentMotionReview
from app.services.runner import JobRunner
from app.services.vision import motion, semantic
from test_vision_semantics import project, stubbed, _make_job, _job_row  # noqa: F401
from test_vision_cache_versions import seed


@pytest.mark.parametrize('pipeline', ['semantic', 'motion'])
@pytest.mark.parametrize('change', ['cancel', 'trim', 'exclude', 'delete', 'source', 'asset', 'job_project'])
def test_after_model_before_writer_never_publishes_stale(pipeline, change, project, stubbed, tmp_path, monkeypatch, client):
    data = seed(project, tmp_path, 1); pid = project['id']; sid = data['segments'][0]['segment_id']
    jid = _make_job(pid, segment_ids=[sid])
    module = semantic if pipeline == 'semantic' else motion
    table = SegmentSemanticResult if pipeline == 'semantic' else SegmentMotionReview
    task = semantic.vision_analyze_task if pipeline == 'semantic' else motion.motion_review_task
    store_name = '_store_result' if pipeline == 'semantic' else '_store'
    original = getattr(module, store_name)
    reached, resume = Event(), Event()
    if pipeline == 'motion':
        with session_scope() as s: s.get(Job, jid).job_type = 'motion_review'
        frames = []
        from PIL import Image
        for i in range(2):
            p = tmp_path / f'm{i}.jpg'; Image.new('RGB', (16, 16), 'red').save(p)
            frames.append(motion.MotionFrame(str(p), i + 1.))
        monkeypatch.setattr(motion, '_sample_frames', lambda *a: frames)
        monkeypatch.setattr(motion, '_analyze_motion', lambda *a, **kw: ({
            'motion_type': 'pan', 'stability_risk': 'low', 'action_summary': 'view',
            'evidence_timestamps_sec': [1., 2.], 'limitations': [], 'needs_human_review': False,
        }, {}))
    def pause(*args, **kwargs):
        reached.set(); assert resume.wait(15)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, store_name, pause)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(JobRunner()._run, jid, task)
        try:
            assert reached.wait(15)
            if change == 'source':
                (tmp_path / 'a.mp4').write_bytes(b'replaced-before-publication')
            else:
                with session_scope() as s:
                    if change == 'cancel': s.get(Job, jid).status = 'cancelling'
                    elif change == 'trim': s.get(Segment, sid).start_sec += 0.0001
                    elif change == 'exclude': s.get(Segment, sid).user_rating = 'exclude'
                    elif change == 'delete': s.delete(s.get(Segment, sid))
                    elif change == 'asset': s.get(Asset, data['assets']['a.mp4']).analyzed_source_signature = 'new'
                    elif change == 'job_project':
                        from app.models import Project
                        other = Project(name='other'); s.add(other); s.flush()
                        s.get(Job, jid).project_id = other.id
            if change == 'cancel':
                conflict = client.post(f'/api/projects/{pid}/jobs/vision_analyze', json={'upload_acknowledged': True})
                assert conflict.status_code == 409
        finally:
            resume.set()
        future.result(timeout=20)
    with session_scope() as s:
        assert s.scalars(select(table).where(table.segment_id == sid, table.is_current.is_(True))).all() == []
    row = _job_row(jid)
    assert row.status == ('cancelled' if change == 'cancel' else 'partial_failed')
    if change != 'cancel':
        assert row.failures['items'][0]['error_code'] in {'SOURCE_CHANGED', 'SEGMENT_INVALID', 'JOB_INVALID'}
    assert row.stats.get('analyzed', 0) == 0


@pytest.mark.parametrize('pipeline', ['semantic', 'motion'])
def test_writer_first_commits_once_then_cancel_converges(pipeline, project, stubbed, tmp_path, monkeypatch):
    from app.database import begin_write
    data = seed(project, tmp_path, 1); pid = project['id']; sid = data['segments'][0]['segment_id']
    jid = _make_job(pid, force=True, segment_ids=[sid])
    module = semantic if pipeline == 'semantic' else motion
    table = SegmentSemanticResult if pipeline == 'semantic' else SegmentMotionReview
    task = semantic.vision_analyze_task if pipeline == 'semantic' else motion.motion_review_task
    name = '_store_result' if pipeline == 'semantic' else '_store'
    if pipeline == 'motion':
        from PIL import Image
        with session_scope() as s: s.get(Job, jid).job_type = 'motion_review'
        frames = []
        for i in range(2):
            p = tmp_path / f'w{i}.jpg'; Image.new('RGB', (16, 16), 'blue').save(p)
            frames.append(motion.MotionFrame(str(p), i + 1.))
        monkeypatch.setattr(motion, '_sample_frames', lambda *a: frames)
        monkeypatch.setattr(motion, '_analyze_motion', lambda *a, **kw: ({
            'motion_type': 'pan', 'stability_risk': 'low', 'action_summary': 'view',
            'evidence_timestamps_sec': [1., 2.], 'limitations': [], 'needs_human_review': False,
        }, {}))
    locked, unlock, committed, finish, cancel_started = (Event() for _ in range(5))
    validator = module.validate_publication
    original = getattr(module, name)
    def while_locked(*a, **kw):
        validator(*a, **kw)
        locked.set(); assert unlock.wait(15)
    def after_commit(*a, **kw):
        result = original(*a, **kw)
        committed.set(); assert finish.wait(15)
        return result
    monkeypatch.setattr(module, 'validate_publication', while_locked)
    monkeypatch.setattr(module, name, after_commit)
    def cancel():
        cancel_started.set()
        with session_scope() as s:
            begin_write(s)
            s.get(Job, jid).status = 'cancelling'
    with ThreadPoolExecutor(2) as pool:
        worker = pool.submit(JobRunner()._run, jid, task)
        try:
            assert locked.wait(15)
            cancellation = pool.submit(cancel)
            assert cancel_started.wait(10)
            assert not cancellation.done()  # writer reservation blocks cancellation
            unlock.set()
            assert committed.wait(15)
            cancellation.result(timeout=15)
        finally:
            unlock.set(); finish.set()
        worker.result(timeout=20)
    assert _job_row(jid).status == 'cancelled'
    with session_scope() as s:
        assert len(s.scalars(select(table).where(table.segment_id == sid, table.is_current.is_(True))).all()) == 1
