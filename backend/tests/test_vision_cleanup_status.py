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


def _pipeline(kind, pid, segment_id, stubbed_provider, monkeypatch):
    """Build one visual job for *kind*, with its frames and model stubbed out.

    Extracted from the ``visual`` fixture so a single test can run **both**
    pipelines and compare their contracts (TSB-361 B08) instead of relying on
    two parametrized runs that never see each other.
    """
    jid = _make_job(pid, segment_ids=[segment_id])
    worker = semantic if kind == 'semantic' else motion
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
        calls = stubbed_provider.calls
    task = motion.motion_review_task if worker is motion else semantic.vision_analyze_task
    return pid, jid, worker, task, calls


@pytest.fixture(params=['semantic', 'motion'])
def visual(request, project, stubbed, tmp_path, monkeypatch):
    data = seed(project, tmp_path, 1)
    return _pipeline(request.param, project['id'], data['segments'][0]['segment_id'],
                     stubbed, monkeypatch)


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


# ---------------------------------------------------------------------------
# TSB-361 §5.3 — 清理状态隔离回归（B01–B08）
#
# 这一组只回答一件事：**五个状态彼此独立可见**。模型请求成功、结果入库、
# staging 清理成功、当前 Job 终态、retry 是否重新请求模型——任何一项都不能被
# 另一项的结论顶替。参数化保留 ``motion`` 分支（任务书 §5.3 明确要求）。
# ---------------------------------------------------------------------------


def _rows(pid, worker):
    with session_scope() as s:
        model = SegmentMotionReview if worker is motion else SegmentSemanticResult
        return s.scalars(select(model).where(model.project_id == pid)).all()


def test_b01_model_success_with_failed_cleanup_keeps_every_state_distinct(visual, client, monkeypatch):
    """B01：模型成功、清理失败 ⇒ 五个状态各自可见，Job 记为部分失败。

    清理失败既不能抹掉"已分析"的成果，也不能被算成整体成功或整体失败。
    """
    pid, jid, worker, task, calls = visual
    paid, stored_before = len(calls), len(_rows(pid, worker))
    with monkeypatch.context() as m:
        m.setattr(cleanup, '_remove_file', locked)
        r.JobRunner()._run(jid, task)
    result = row(client, jid)
    assert len(calls) - paid == 1, '模型必须被真实请求且仅一次'
    assert len(_rows(pid, worker)) - stored_before == 1, '视觉结果必须真的入库'
    assert result['stats']['cleanup']['ok'] is False
    assert result['stats']['cleanup']['residual_count'] > 0
    assert result['stats']['analyzed'] == 1 and result['stats']['failed'] == 0
    assert result['status'] == 'partial_failed', result['status']


def test_b02_repeated_cleanup_retry_never_re_requests_a_successful_model(visual, client, monkeypatch):
    """B02：连续两次重试清理，都不重新请求已经成功的模型。"""
    pid, jid, worker, task, calls = visual
    with monkeypatch.context() as m:
        m.setattr(cleanup, '_remove_file', locked)
        r.JobRunner()._run(jid, task)
    paid, stored = len(calls), len(_rows(pid, worker))
    for attempt in (1, 2):
        response = client.post(f'/api/projects/{pid}/jobs/{jid}/cleanup-retry')
        assert response.status_code == 200, (attempt, response.text)
        assert len(calls) == paid, f'第 {attempt} 次重试重新调用了模型'
        assert len(_rows(pid, worker)) == stored, f'第 {attempt} 次重试重复写入了结果'
        assert row(client, jid)['stats']['cleanup']['ok'] is True


def test_b03_two_jobs_cleaning_concurrently_only_touch_their_own_staging(project):
    """B03：两个 Job 并发清理，各自的暂存互不影响、互不误删。"""
    from test_vision_staging_ownership import visual_stage
    pid = project['id']
    owner_a, path_a = visual_stage(pid, 'vision_analyze')
    owner_b, path_b = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, path_a, owner_job_id=owner_a)
    cleanup.create_owned_staging(pid, path_b, owner_job_id=owner_b)
    (path_a / 'a.jpg').write_bytes(b'A' * 32)
    (path_b / 'b.jpg').write_bytes(b'B' * 64)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(cleanup.cleanup_staging_or_log, pid, path, owner_job_id=owner)
                   for path, owner in ((path_a, owner_a), (path_b, owner_b))]
        result_a, result_b = [future.result(10) for future in futures]
    assert (result_a.ok, result_a.removed_bytes) == (True, 32)
    assert (result_b.ok, result_b.removed_bytes) == (True, 64)
    assert not path_a.exists() and not path_b.exists()


def test_b04_ownership_left_by_another_test_cannot_change_this_result(project):
    """B04：注册表里遗留的他人登记，不影响本 Job 的清理结论，也不被本 Job 删除。"""
    from test_vision_staging_ownership import visual_stage
    pid = project['id']
    leftover_owner, leftover_path = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, leftover_path, owner_job_id=leftover_owner)
    (leftover_path / 'left.jpg').write_bytes(b'leftover bytes')
    mine_owner, mine_path = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, mine_path, owner_job_id=mine_owner)
    (mine_path / 'mine.jpg').write_bytes(b'mine')
    result = cleanup.cleanup_staging_or_log(pid, mine_path, owner_job_id=mine_owner)
    assert result.ok and result.removed_bytes == len(b'mine')
    assert (leftover_path / 'left.jpg').read_bytes() == b'leftover bytes'
    assert cleanup.is_registered_leaf(pid, leftover_path, owner_job_id=leftover_owner)
    with session_scope() as s:
        assert s.get(Job, leftover_owner).stats.get('cleanup') is None
        assert s.get(Job, mine_owner).stats['cleanup']['ok'] is True


def test_b05_a_lost_receipt_is_never_reported_as_a_successful_cleanup(project):
    """B05：receipt 丢失后同路径又出现目录 ⇒ 必须报未授权，不能按成功处理。"""
    from test_vision_staging_ownership import visual_stage
    pid = project['id']
    owner, path = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, path, owner_job_id=owner)
    (path / 'f.jpg').write_bytes(b'x')
    assert cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner).ok
    cleanup._CLEANED.pop(path, None)          # 进程重启 / 注册表被清
    path.mkdir(parents=True)
    (path / 'new.jpg').write_bytes(b'not ours')
    result = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner)
    assert result.ok is False and result.error_code == cleanup.ERR_NOT_OWNED
    assert (path / 'new.jpg').read_bytes() == b'not ours', '未授权的目录一个字节都不能删'


def test_b06_an_exception_mid_cleanup_keeps_explicit_failure_evidence(project, monkeypatch):
    """B06：删到一半出错 ⇒ 失败码、残留字节、Job 报告与审计条目全部保留。"""
    from test_vision_staging_ownership import visual_stage
    pid = project['id']
    owner, path = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, path, owner_job_id=owner)
    (path / 'one.jpg').write_bytes(b'1' * 10)
    (path / 'two.jpg').write_bytes(b'2' * 20)
    real, seen = cleanup._remove_file, []

    def flaky(target, *a, **kw):
        seen.append(target)
        if len(seen) >= 2:
            raise OSError('injected mid-cleanup failure')
        return real(target, *a, **kw)
    monkeypatch.setattr(cleanup, '_remove_file', flaky)
    result = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner)
    assert len(seen) >= 2, '注入点必须真的落在删除过程中'
    assert result.ok is False and result.error_code == cleanup.ERR_CLEANUP_FAILED
    assert result.retryable is True and result.residual_count >= 1 and result.residual_bytes > 0
    with session_scope() as s:
        job = s.get(Job, owner)
        stats = dict(job.stats or {}); facts = dict(job.failures or {})
    assert stats['cleanup']['ok'] is False and stats['cleanup']['error_code'] == 'CLEANUP_FAILED'
    audit = next(item for item in facts['items'] if item.get('cleanup_audit'))
    assert audit['error_code'] == 'CLEANUP_FAILED' and audit['recorded_at']


def test_b07_repeating_a_cleanup_is_idempotent_and_deletes_nothing_else(project):
    """B07：同一目录反复清理幂等，邻接目录的内容不受影响。"""
    from test_vision_staging_ownership import visual_stage
    pid = project['id']
    owner, path = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, path, owner_job_id=owner)
    (path / 'f.jpg').write_bytes(b'payload')
    neighbour = path.parent / 'other-job'
    neighbour.mkdir()
    (neighbour / 'keep.jpg').write_bytes(b'keep')
    first = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner)
    second = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner)
    third = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=owner)
    assert (first.ok, first.removed_files) == (True, 1)
    assert (second.ok, second.removed_files) == (True, 0)
    assert (third.ok, third.removed_files) == (True, 0)
    assert (neighbour / 'keep.jpg').read_bytes() == b'keep'


CONTRACT = dict(model_requests=1, stored_rows=1, status='partial_failed',
                cleanup_ok=False, error_code='CLEANUP_FAILED', retryable=True,
                retry_model_requests=0, audit_resolved=True)


def test_b08_motion_and_semantic_publish_the_same_cleanup_contract(project, stubbed, client, tmp_path, monkeypatch):
    """B08：motion 与 semantic 在同一条清理状态契约上完全一致。

    两条流水线跑在同一个用例里，逐字段比较契约，而不是各跑一次"都通过"就算
    一致——否则参数化本身会把差异藏起来。
    """
    data = seed(project, tmp_path, 1)
    sid = data['segments'][0]['segment_id']
    contracts = {}
    for kind in ('semantic', 'motion'):
        with monkeypatch.context() as m:
            pid, jid, worker, task, calls = _pipeline(kind, project['id'], sid, stubbed, m)
            paid, stored = len(calls), len(_rows(project['id'], worker))
            m.setattr(cleanup, '_remove_file', locked)
            r.JobRunner()._run(jid, task)
            before = row(client, jid)
            model_requests = len(calls) - paid
            stored_rows = len(_rows(project['id'], worker)) - stored
        # 重试必须在"清理必然失败"的注入之外进行，否则测的是同一个故障。
        paid = len(calls)
        response = client.post(f'/api/projects/{pid}/jobs/{jid}/cleanup-retry')
        assert response.status_code == 200, response.text
        after = row(client, jid)
        audit = next(f for f in after['failures']['items'] if f.get('cleanup_audit'))
        contracts[kind] = dict(
            model_requests=model_requests,
            stored_rows=stored_rows,
            status=before['status'],
            cleanup_ok=before['stats']['cleanup']['ok'],
            error_code=before['stats']['cleanup']['error_code'],
            retryable=before['stats']['cleanup']['retryable'],
            retry_model_requests=len(calls) - paid,
            audit_resolved=bool(audit.get('resolved_at')),
        )
    assert contracts['semantic'] == CONTRACT, contracts['semantic']
    assert contracts['motion'] == CONTRACT, contracts['motion']
    assert contracts['motion'] == contracts['semantic']
