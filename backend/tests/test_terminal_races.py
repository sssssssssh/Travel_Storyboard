"""RT271: deterministic CAS interleavings; no timing sleeps."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import Job
from app.services import runner as r


def make_job(client, status='pending', count=0):
    pid = client.post('/api/projects', json={'name': 'terminal-race'}).json()['id']
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', status=status, failures={'count': count})
        s.add(job); s.flush()
        return pid, job.id


@pytest.mark.parametrize('outcome', ['succeeded', 'partial_failed', 'failed'])
def test_cancel_wins_between_outcome_check_and_terminal_cas(client, monkeypatch, outcome):
    pid, jid = make_job(client, count=int(outcome == 'partial_failed'))
    entered, release = Event(), Event()
    original = r.update_job
    def gated(job_id, **fields):
        if job_id == jid and fields.get('status') == outcome:
            entered.set()
            assert release.wait(10)
        return original(job_id, **fields)
    monkeypatch.setattr(r, 'update_job', gated)
    def task(job_id):
        if outcome == 'failed':
            raise RuntimeError('real media failure')
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(r.JobRunner()._run, jid, task)
        assert entered.wait(10)
        try:
            assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
            assert client.post(f'/api/jobs/{jid}/cancel').json()['status'] == 'cancelling'
            with session_scope() as s:
                before = s.scalar(select(func.count()).select_from(Job).where(Job.project_id == pid))
            assert client.post(f'/api/projects/{pid}/jobs/scan').status_code == 409
            with session_scope() as s:
                assert s.scalar(select(func.count()).select_from(Job).where(Job.project_id == pid)) == before
        finally:
            release.set()
        future.result(10)
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == 'cancelled', 'lost terminal CAS must settle cancelling before unlock'
        assert job.finished_at is not None
        if outcome == 'partial_failed': assert job.failures['count'] == 1
    monkeypatch.setattr(r.runner, 'submit', lambda _: None)
    assert client.post(f'/api/projects/{pid}/jobs/scan').status_code == 202


@pytest.mark.parametrize('outcome', ['succeeded', 'partial_failed', 'failed'])
def test_committed_terminal_is_not_overwritten_by_cancel(client, outcome):
    _, jid = make_job(client, count=int(outcome == 'partial_failed'))
    def task(_):
        if outcome == 'failed': raise RuntimeError('real failure')
    r.JobRunner()._run(jid, task)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == outcome
        timestamp, error = row.finished_at, row.error
    assert client.post(f'/api/jobs/{jid}/cancel').status_code == 409
    r.JobRunner()._settle_cancelled(jid, started=True)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert (row.status, row.finished_at, row.error) == (outcome, timestamp, error)


def test_task_finally_finishes_under_project_lock_before_terminal(client):
    pid, jid = make_job(client)
    entered, release = Event(), Event()
    def task(_):
        try:
            raise r.JobCancelled('stop')
        finally:
            entered.set(); assert release.wait(10)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(r.JobRunner()._run, jid, task)
        assert entered.wait(10)
        try:
            assert client.post(f'/api/jobs/{jid}/cancel').json()['status'] == 'cancelling'
            with session_scope() as s: assert s.get(Job, jid).finished_at is None
            lock = r.project_lock(pid)
            assert not lock.acquire(blocking=False)
        finally: release.set()
        future.result(10)
    assert r.job_status(jid) == 'cancelled'


def test_bounded_convergence_failure_is_structured_and_does_not_stamp_finish(client, monkeypatch, caplog):
    _, jid = make_job(client, status='running')
    attempts = []
    monkeypatch.setattr(r, 'update_job', lambda *a, **kw: attempts.append(kw) or False)
    with pytest.raises(r.StateConvergenceError): r.finalize_job(jid, 'succeeded')
    assert len(attempts) == 4
    assert 'STATE_CONVERGENCE_FAILED' in caplog.text and jid in caplog.text
    with session_scope() as s: assert s.get(Job, jid).finished_at is None


def test_database_unavailable_reports_job_and_unconfirmed_release(client, monkeypatch, caplog):
    _, jid = make_job(client, status='running')
    def unavailable(_): raise OSError('injected DB I/O failure')
    monkeypatch.setattr(r, 'job_status', unavailable)
    with pytest.raises(r.StateConvergenceError): r.finalize_job(jid, 'succeeded')
    assert jid in caplog.text and 'DB release unconfirmed' in caplog.text


def test_two_thousand_controlled_terminal_cas_losses(client, monkeypatch):
    pid = client.post('/api/projects', json={'name': 'fixed-cas-matrix'}).json()['id']
    with session_scope() as s:
        rows = [Job(project_id=pid, job_type='scan', status='running') for _ in range(2000)]
        s.add_all(rows); s.flush(); ids = [row.id for row in rows]
    original = r.update_job
    def cancel_at_cas(jid, **fields):
        if fields.get('status') in ('succeeded', 'partial_failed', 'failed'):
            assert original(jid, status='cancelling', expected_statuses=('running',))
        return original(jid, **fields)
    monkeypatch.setattr(r, 'update_job', cancel_at_cas)
    for index, jid in enumerate(ids):
        assert r.finalize_job(jid, ('succeeded', 'partial_failed', 'failed')[index % 3]) == 'cancelled'
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.project_id == pid)).all()
        assert all(row.status == 'cancelled' and row.finished_at is not None for row in rows)


@pytest.mark.parametrize('before,after', [('cancelled', 'succeeded'), ('succeeded', 'cancelled'), ('failed', 'running'), ('cancelling', 'running')])
def test_explicit_expected_status_cannot_override_monotonic_state_machine(client, before, after):
    _, jid = make_job(client, status=before)
    assert not r.update_job(jid, expected_statuses=(before,), status=after, error='must not overwrite')
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == before and row.error != 'must not overwrite'
