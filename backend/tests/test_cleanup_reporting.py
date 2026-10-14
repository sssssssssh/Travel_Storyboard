"""RT274: honest accounting, persistence and authenticated bounded retry."""
import pytest
from app.database import session_scope
from app.models import Job
from app.services import runner as r, staging_cleanup as c
from test_staging_ownership import owned


def test_cleanup_failure_survives_progress_and_other_stage_success(client, monkeypatch):
    pid, jid, stage = owned(client)
    with monkeypatch.context() as m:
        m.setattr(c, '_remove_file', lambda *a: (_ for _ in ()).throw(PermissionError('locked')))
        assert not c.cleanup_staging_or_log(pid, stage, owner_job_id=jid).ok
    other = c.create_owned_staging(pid, stage.with_name('staging-second'), owner_job_id=jid)
    assert c.cleanup_staging_or_log(pid, other, owner_job_id=jid).ok
    r.update_job(jid, stats={'done': 9})
    with session_scope() as s:
        cleanup = s.get(Job, jid).stats['cleanup']
        assert not cleanup['ok'] and cleanup['residual_bytes'] == len(b'owned bytes')
        assert cleanup['error_code'] == 'CLEANUP_FAILED'
        assert all(not p.startswith('/') for p in cleanup['residual_paths'])
        assert s.get(Job, jid).stats['done'] == 9


def test_terminal_cleanup_retry_is_authenticated_scoped_and_idempotent(client, monkeypatch):
    pid, jid, stage = owned(client)
    url = f'/api/projects/{pid}/jobs/{jid}/cleanup-retry'
    with monkeypatch.context() as m:
        m.setattr(c, '_remove_file', lambda *a: (_ for _ in ()).throw(PermissionError('locked')))
        c.cleanup_staging_or_log(pid, stage, owner_job_id=jid)
    assert client.post(url).status_code == 409  # worker still runs
    assert client.post(url, headers={'X-Access-Token': ''}).status_code in (401, 403)
    other = client.post('/api/projects', json={'name': 'other'}).json()['id']
    assert client.post(f'/api/projects/{other}/jobs/{jid}/cleanup-retry').status_code == 404
    r.finalize_job(jid, 'cancelled')
    response = client.post(url)
    assert response.status_code == 200, response.text
    assert response.json()['stats']['cleanup']['ok']
    assert response.json()['stats']['cleanup']['residual_bytes'] == 0
    assert client.post(url).json()['stats']['cleanup']['ok']
    assert not stage.exists()


def test_retry_cannot_adopt_old_or_invented_cleanup_paths(client):
    pid, jid, stage = owned(client)
    c._OWNERS.pop(stage)  # simulate process restart: creation evidence is gone
    r.finalize_job(jid, 'cancelled')
    assert client.post(f'/api/projects/{pid}/jobs/{jid}/cleanup-retry').status_code == 409
    assert (stage / 'owned.bin').read_bytes() == b'owned bytes'


def test_legacy_single_cleanup_failure_survives_resumed_stage_success(client):
    pid, jid, stage = owned(client)
    legacy = stage.with_name('staging-legacy'); legacy.mkdir()
    leftover = legacy / 'legacy.bin'; leftover.write_bytes(b'legacy residual')
    previous = c.CleanupResult(path=str(legacy), ok=False, error_code='CLEANUP_FAILED',
        residual_paths=[str(leftover)], retryable=True).as_dict()
    previous.pop('residual_bytes')  # original v2.0.6 format had no measured byte count
    r.merge_job_stats(jid, {'cleanup': previous})
    assert c.cleanup_staging_or_log(pid, stage, owner_job_id=jid).ok
    r.update_job(jid, stats={'done': 1})
    with session_scope() as s:
        result = s.get(Job, jid).stats['cleanup']
        assert result['ok'] is False, 'a resumed run must not erase a legacy cleanup failure'
        assert result['error_code'] == 'CLEANUP_FAILED'
        assert result['residual_paths'] and not result['residual_paths'][0].startswith('/')
        assert result['residual_bytes'] is None and not result['retryable']
    assert leftover.read_bytes() == b'legacy residual'
