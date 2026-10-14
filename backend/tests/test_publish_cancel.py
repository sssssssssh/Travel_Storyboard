"""Evidence from RT273 audit: roughcut's last poll did not guard publication."""
from pathlib import Path
import pytest
from app.services import roughcut as c
from app.services.runner import JobCancelled
from app.database import session_scope
from app.models import Job
from app.paths import project_cache_dir
from test_cancel_cleanup import _make_project, _add_asset, _add_segment, _add_shot, _fake_job_id, _write


def setup_cut(client, monkeypatch):
    pid = _make_project(client)['id']
    aid = _add_asset(pid); sid = _add_segment(pid, aid); _add_shot(pid, aid, sid)
    jid = _fake_job_id(pid)
    monkeypatch.setattr(c, '_extract_normalized', lambda *a, **kw: _write(Path(a[3]), b'clip'))
    monkeypatch.setattr(c, 'run', lambda *a, **kw: _write(Path(kw['expect_output']), b'new-cut'))
    return pid, jid


def test_cancel_after_last_roughcut_poll_does_not_publish_or_touch_history(client, monkeypatch):
    pid, jid = setup_cut(client, monkeypatch)
    old = project_cache_dir(pid) / 'roughcut' / 'roughcut.mp4'; _write(old, b'old-cut')
    original = c.raise_if_cancelled
    calls = []
    def cancel_after_poll(job):
        original(job); calls.append(job)
        if len(calls) == 3: assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
    monkeypatch.setattr(c, 'raise_if_cancelled', cancel_after_poll)
    with pytest.raises(JobCancelled): c.roughcut_task(jid)
    assert old.read_bytes() == b'old-cut'
    assert list(old.parent.glob('roughcut*.mp4')) == [old]
    with session_scope() as s: assert not (s.get(Job, jid).resume_token or {}).get('path')


def test_each_committed_roughcut_preserves_previous_bytes_and_serves_latest(client, monkeypatch):
    pid, jid = setup_cut(client, monkeypatch)
    old = project_cache_dir(pid) / 'roughcut' / 'roughcut.mp4'; _write(old, b'old-cut')
    c.roughcut_task(jid)
    with session_scope() as s: first = s.get(Job, jid).resume_token['path']
    jid2 = _fake_job_id(pid)
    monkeypatch.setattr(c, 'run', lambda *a, **kw: _write(Path(kw['expect_output']), b'newer-cut'))
    c.roughcut_task(jid2)
    assert old.read_bytes() == b'old-cut'
    from app.paths import resolve_in_project
    assert resolve_in_project(pid, first).read_bytes() == b'new-cut'
    assert client.get(f'/api/projects/{pid}/roughcut').content == b'newer-cut'
