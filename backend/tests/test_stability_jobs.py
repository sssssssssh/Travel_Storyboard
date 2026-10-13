"""TSB-203: terminal CAS, queued cancel, serialized recovery and failures."""
import threading
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
