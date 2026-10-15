"""TSB-320: durable health and Runner-only terminal outcome, through real API."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Job, SegmentSemanticResult, SegmentMotionReview
from app.services import runner as r, staging_cleanup as cleanup
from app.services.vision import semantic, motion
from app.services.vision.errors import ModelError
from test_vision_semantics import project, stubbed, _make_job  # noqa: F401
from test_vision_cache_versions import seed


@pytest.fixture(params=['semantic', 'motion'])
def visual(request, project, stubbed, tmp_path, monkeypatch):
    pid = project['id']; data = seed(project, tmp_path, 1)
    jid = _make_job(pid, segment_ids=[data['segments'][0]['segment_id']])
    worker = semantic if request.param == 'semantic' else motion
    calls = []
    if worker is motion:
        with session_scope() as s: s.get(Job, jid).job_type = 'motion_review'
        def sample(*args):
            frames = []
            for i in range(2):
                path = args[3] / f'{i}.jpg'; Image.new('RGB', (16, 16), 'green').save(path)
                frames.append(motion.MotionFrame(str(path), i+1.))
            return frames
        monkeypatch.setattr(motion, '_sample_frames', sample)
        def infer(*a, **kw):
            calls.append(1)
            return {'motion_type':'pan', 'stability_risk':'low', 'action_summary':'view',
                    'evidence_timestamps_sec':[1.,2.], 'limitations':[], 'needs_human_review':False}, {}
        monkeypatch.setattr(motion, '_analyze_motion', infer)
    else:
        calls = stubbed.calls
    task = motion.motion_review_task if worker is motion else semantic.vision_analyze_task
    return pid, jid, worker, task, calls


def row(client, jid):
    response = client.get('/api/jobs/'+jid); assert response.status_code == 200
    return response.json()


def locked(*args):
    raise PermissionError('injected locked staging')


def test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free(visual, client, monkeypatch):
    pid,jid,worker,task,calls = visual
    with monkeypatch.context() as m:
        m.setattr(cleanup, '_remove_file', locked)
        r.JobRunner()._run(jid, task)
    before = row(client,jid)
    assert before['status'] == 'partial_failed'
    assert before['stats']['analyzed'] == 1 and before['stats']['failed'] == 0
    assert before['stats']['cleanup']['ok'] is False
    assert before['stats']['cleanup']['residual_count'] > 0 and before['stats']['cleanup']['residual_bytes'] > 0
    assert any(f['kind']=='cleanup' and f['error_code']=='CLEANUP_FAILED' for f in before['failures']['items'])
    with session_scope() as s:
        assert s.get(Job,jid).status == 'partial_failed'
        model = SegmentMotionReview if worker is motion else SegmentSemanticResult
        model_ids = [v.id for v in s.scalars(select(model)).all()]
        assert model_ids
    paid = len(calls)
    response = client.post(f'/api/projects/{pid}/jobs/{jid}/cleanup-retry')
    assert response.status_code == 200
    after = row(client,jid)
    assert after['stats']['cleanup']['ok'] and after['stats']['cleanup']['remediated']
    assert (after['status'],after['finished_at']) == (before['status'],before['finished_at'])
    audit = next(f for f in after['failures']['items'] if f.get('cleanup_audit'))
    assert audit['error_code']=='CLEANUP_FAILED' and audit['resolved_at']
    assert len(calls)==paid
    with session_scope() as s: assert [v.id for v in s.scalars(select(model)).all()]==model_ids


def test_worker_never_claims_success_before_runner_finalization(visual,client,monkeypatch):
    _,jid,worker,task,_ = visual
    original = worker.update_job; premature = []
    def observe(job_id,**fields):
        if fields.get('status') in ('succeeded','partial_failed'): premature.append(fields['status'])
        return original(job_id,**fields)
    monkeypatch.setattr(worker,'update_job',observe)
    r.JobRunner()._run(jid,task)
    assert premature==[]
    assert row(client,jid)['status']=='succeeded' and row(client,jid)['stats']['cleanup']['ok']


def test_cancel_during_failed_cleanup_wins_and_terminal_time_is_immutable(visual,client,monkeypatch):
    pid,jid,worker,task,_=visual
    entered,release=Event(),Event();original=worker.cleanup_staging_or_log
    def gated(*args,**kwargs):
        result=original(*args,**kwargs);entered.set();assert release.wait(10);return result
    monkeypatch.setattr(cleanup,'_remove_file',locked)
    monkeypatch.setattr(worker,'cleanup_staging_or_log',gated)
    with ThreadPoolExecutor(1) as pool:
        future=pool.submit(r.JobRunner()._run,jid,task)
        assert entered.wait(10)
        try:
            assert row(client,jid)['status']=='running'
            assert client.post('/api/jobs/'+jid+'/cancel').json()['status']=='cancelling'
            assert client.post(f'/api/projects/{pid}/jobs/scan').status_code==409
        finally: release.set()
        future.result(10)
    before=row(client,jid);assert before['status']=='cancelled' and not before['stats']['cleanup']['ok']
    assert before['finished_at']
    assert not r.update_job(jid,status='succeeded')
    assert row(client,jid)['finished_at']==before['finished_at']


def test_stats_failure_and_cleanup_failure_both_remain_visible(visual,client,monkeypatch):
    _,jid,worker,task,_=visual
    monkeypatch.setattr(worker,'merge_job_stats',lambda *a: (_ for _ in ()).throw(RuntimeError('original stats failure')))
    monkeypatch.setattr(cleanup,'_remove_file',locked)
    r.JobRunner()._run(jid,task)
    result=row(client,jid)
    assert result['status']=='failed' and 'original stats failure' in result['error']
    assert result['stats']['cleanup']['ok'] is False
    assert any(f.get('cleanup_audit') for f in result['failures']['items'])


def test_cleanup_report_storage_failure_cannot_become_success(visual,client,monkeypatch):
    _,jid,_,task,_=visual
    monkeypatch.setattr(r,'record_cleanup_result',lambda *a: (_ for _ in ()).throw(OSError('report storage unavailable')))
    monkeypatch.setattr(cleanup,'_remove_file',locked)
    r.JobRunner()._run(jid,task)
    result=row(client,jid)
    assert result['status']=='failed' and 'CLEANUP_REPORT_FAILED' in result['error']


def test_stale_business_failure_snapshot_cannot_erase_cleanup_audit(visual,client,monkeypatch):
    pid,jid,_,_,_=visual
    from test_vision_staging_ownership import visual_stage
    owner,path=visual_stage(pid,'vision_analyze')
    snapshot=r.JobFailures(owner)
    cleanup.create_owned_staging(pid,path,owner_job_id=owner);(path/'bytes').write_bytes(b'owned')
    monkeypatch.setattr(cleanup,'_remove_file',locked)
    cleanup.cleanup_staging_or_log(pid,path,owner_job_id=owner)
    snapshot.record(kind='budget',ref=jid,message='budget failed',error_code='MODEL_BUDGET_EXCEEDED')
    assert any(f.get('cleanup_audit') for f in row(client,owner)['failures']['items'])


@pytest.mark.parametrize('loss', ['restart', 'link', 'sibling'])
def test_unsafe_stage_is_visible_without_deleting_foreign_content(visual,client,monkeypatch,tmp_path,loss):
    from test_staging_ownership import link_directory
    pid,jid,worker,task,_=visual; original=worker.cleanup_staging_or_log
    protected=tmp_path/'external';protected.mkdir();keep=protected/'keep';keep.write_bytes(b'foreign bytes')
    def replaced(project_id,path,**kwargs):
        if loss=='restart': cleanup._OWNERS.pop(path)
        elif loss=='link':
            path.rename(path.with_name(path.name+'-retained'));link_directory(path,protected)
        else:
            from test_vision_staging_ownership import visual_stage
            other,foreign=visual_stage(pid,'vision_analyze')
            cleanup.create_owned_staging(pid,foreign,owner_job_id=other)
            (foreign/'keep').write_bytes(b'foreign bytes')
            result=original(project_id,foreign,**kwargs)
            assert (foreign/'keep').read_bytes()==b'foreign bytes'
            return result
        return original(project_id,path,**kwargs)
    monkeypatch.setattr(worker,'cleanup_staging_or_log',replaced)
    r.JobRunner()._run(jid,task)
    result=row(client,jid)
    assert result['status']=='partial_failed'
    assert result['stats']['cleanup']['error_code']=='STAGING_NOT_OWNED'
    assert result['stats']['cleanup']['residual_bytes'] is None
    assert keep.read_bytes()==b'foreign bytes'
