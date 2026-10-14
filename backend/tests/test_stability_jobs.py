"""TSB-203: terminal CAS, queued cancel, serialized recovery and failures."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.database import session_scope
from app.models import Job
from app.services.runner import JobRunner, JobFailures, update_job


def job_row(client, status="pending", **kwargs):
    pid = client.post('/api/projects', json={'name': 'state-test'}).json()['id']
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status=status, **kwargs)
        s.add(job)
        s.flush()
        return job.id


def test_cancel_before_dispatch_never_calls_task(client):
    jid = job_row(client)
    assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
    called = []
    JobRunner()._run(jid, lambda _: called.append(True))
    assert called == []
    assert client.get(f'/api/jobs/{jid}').json()['status'] == 'cancelled'


def test_last_item_cancel_survives_normal_return(client):
    jid = job_row(client)
    entered, release = threading.Event(), threading.Event()
    local = JobRunner()
    def task(_):
        entered.set()
        assert release.wait(5)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(local._run, jid, task)
        assert entered.wait(5)
        assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
        release.set()
        future.result(5)
    assert client.get(f'/api/jobs/{jid}').json()['status'] == 'cancelled'


def test_terminal_cannot_change_and_cancel_returns_conflict(client):
    jid = job_row(client, 'succeeded')
    update_job(jid, status='failed', error='must not overwrite')
    assert client.get(f'/api/jobs/{jid}').json()['status'] == 'succeeded'
    assert client.post(f'/api/jobs/{jid}/cancel').status_code == 409


def test_failure_collector_restores_and_deduplicates(client):
    jid = job_row(client, failures={'count': 1, 'items': [{'kind': 'asset', 'ref': 'a', 'message': 'old'}]})
    failures = JobFailures(jid)
    assert len(failures) == 1
    failures.record(kind='asset', ref='a', message='retry failed')
    assert len(failures) == 1
    failures.record(kind='asset', ref='b', message='second')
    assert len(failures) == 2


def test_same_project_workers_serialize(client):
    jid = job_row(client)
    with session_scope() as s:
        first = s.get(Job, jid)
        second = Job(project_id=first.project_id, job_type='scan', status='running')
        s.add(second)
        s.flush()
        jid2 = second.id
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    local = JobRunner()
    def first_task(_):
        entered.set()
        assert release.wait(5)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(local._run, jid, first_task)
        assert entered.wait(5)
        two = pool.submit(local._run, jid2, lambda _: second_entered.set())
        try:
            assert not second_entered.wait(.15)
        finally:
            release.set()
        one.result(5)
        two.result(5)
    assert second_entered.is_set()


def test_recovery_queues_active_jobs_and_never_resumes_cancelled(client, monkeypatch):
    import app.services.runner as module
    jid = job_row(client, 'running', failures={'count': 1, 'items': [{'kind': 'asset', 'ref': 'broken', 'message': 'persisted'}]})
    from datetime import datetime, timedelta, timezone
    # Windows clocks may produce equal creation timestamps. Give this ordering
    # test explicit persisted times, preserving every serialization assertion.
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        first = s.get(Job, jid)
        first.created_at = created
        pid = first.project_id
        second = Job(project_id=pid, job_type='scan', status='running', created_at=created + timedelta(seconds=1))
        cancelled = Job(project_id=pid, job_type='detect', status='cancelled')
        s.add_all([second, cancelled]); s.flush(); jid2, cancelled_id = second.id, cancelled.id
    local = JobRunner()
    monkeypatch.setattr(module, 'runner', local)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def task(current):
        seen.append(current)
        if current == jid:
            entered.set(); assert release.wait(5)
        else:
            finished.set()
    monkeypatch.setitem(module.TASK_REGISTRY, 'detect', task)
    monkeypatch.setitem(module.TASK_REGISTRY, 'scan', task)
    try:
        resumed = module.resume_interrupted_jobs()
        assert jid in resumed and jid2 in resumed and cancelled_id not in resumed
        assert entered.wait(5)
        assert seen == [jid]
        release.set(); assert finished.wait(5)
        local._futures[jid2].result(5)
        assert seen == [jid, jid2]
        with session_scope() as s:
            assert s.get(Job, jid).failures['count'] == 1
            assert s.get(Job, jid).status == 'partial_failed'
    finally:
        release.set(); local.shutdown()


def test_full_hash_creation_does_not_enumerate_or_read_sources(client, monkeypatch, tmp_path):
    from app.api import jobs
    from app.services import indexer, analyzer
    pid = client.post('/api/projects', json={'name': 'async-hash', 'source_roots': [str(tmp_path)]}).json()['id']
    def forbidden(*a, **kw):
        raise AssertionError('request thread must not build media manifest')
    monkeypatch.setattr(indexer, 'scan_manifest', forbidden)
    monkeypatch.setattr(analyzer, 'detect_manifest', forbidden)
    monkeypatch.setattr(jobs.runner, 'submit', lambda jid: None)
    import time
    for kind in ('scan', 'detect'):
        start = time.monotonic()
        response = client.post(f'/api/projects/{pid}/jobs/{kind}', json={'options': {'full_hash': True}})
        assert response.status_code == 202, response.text
        assert time.monotonic() - start < 2
        row = response.json()
        with session_scope() as s:
            saved = s.get(Job, row['id']).config_snapshot
        assert saved['full_hash'] is True
        assert 'items' not in saved
        assert row['stats']['signature_mode'] == 'full'
        assert client.post(f"/api/jobs/{row['id']}/cancel").status_code == 200
        # TSB-261: cancel now writes the non-terminal `cancelling` state, and the
        # worker settles it to `cancelled` after cleanup. `submit` is stubbed out
        # here, so no worker exists to do that: settle it the way the startup
        # convergence sweep would, otherwise the next iteration is (correctly)
        # blocked by an exclusive `cancelling` job.
        from app.services.runner import converge_pending_cancellations
        converge_pending_cancellations()


def test_real_background_hash_can_wait_while_http_returns_202(client, tmp_path, monkeypatch, record_property):
    from app.services import checkpoints
    from app.services.runner import runner
    pid = client.post('/api/projects', json={'name': 'hash-ui', 'source_roots': [str(tmp_path)]}).json()['id']
    (tmp_path / 'long.mp4').write_bytes(b'x' * (2 * 1024 * 1024))
    entered, release = threading.Event(), threading.Event()
    original = checkpoints.read_hash_block
    def paused(stream, size):
        entered.set(); assert release.wait(5)
        return original(stream, size)
    monkeypatch.setattr(checkpoints, 'read_hash_block', paused)
    start = time.monotonic()
    response = client.post(f'/api/projects/{pid}/jobs/scan', json={'options': {'full_hash': True}})
    elapsed = time.monotonic() - start
    assert response.status_code == 202 and elapsed < 2
    record_property('full_hash_create_job_elapsed_sec', elapsed)
    jid = response.json()['id']
    try:
        assert entered.wait(5)
        row = client.get(f'/api/jobs/{jid}').json()
        assert row['status'] == 'running'
        assert row['stats']['phase'] == 'hashing'
        assert row['stats']['current_file'] == 'long.mp4'
        assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
    finally:
        release.set()
        runner._futures[jid].result(5)
    assert client.get(f'/api/jobs/{jid}').json()['status'] == 'cancelled'
