"""TSB-205: content signatures, stable manifests and frozen config."""
import os
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from app.database import session_scope
from app.models import Asset, Job
from app.services import indexer, analyzer
from app.services.runner import JobCancelled


def project(client):
    return client.post('/api/projects', json={'name': 'checkpoints'}).json()['id']


def test_same_size_mtime_middle_change_is_reindexed(client, tmp_path, monkeypatch):
    pid = project(client)
    path = tmp_path / 'same.mp4'
    path.write_bytes(b'a' * 400000)
    monkeypatch.setattr(indexer, '_apply_probe_fields', lambda a, p, size, mtime: (
        setattr(a, 'file_size_bytes', size), setattr(a, 'mtime_ns', mtime),
        setattr(a, 'quick_fingerprint', indexer.quick_fingerprint(p, size)),
        setattr(a, 'scan_status', 'succeeded')))
    aid, _ = indexer.index_one(pid, path)
    stat = path.stat()
    with path.open('r+b') as f:
        f.seek(200000); f.write(b'changed')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    again, action = indexer.index_one(pid, path)
    assert again == aid and action == 'updated'


def test_scan_resume_tracks_paths_instead_of_list_positions(client, tmp_path, monkeypatch):
    pid = project(client)
    a, b, c = [tmp_path / f'{n}.mp4' for n in ('b', 'c', 'a')]
    a.write_bytes(b'a'); b.write_bytes(b'b')
    with session_scope() as s:
        from app.models import Project
        s.get(Project, pid).source_roots = [str(tmp_path)]
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)]})
        s.add(job); s.flush(); jid = job.id
    visited = []
    def process(_, path):
        if path == b and not c.exists():
            raise JobCancelled('crash between items')
        visited.append(path.name)
        return 'asset', 'created'
    monkeypatch.setattr(indexer, 'index_one', process)
    import pytest
    with pytest.raises(JobCancelled):
        indexer.scan_task(jid)
    assert visited == ['b.mp4']
    c.write_bytes(b'new')
    indexer.scan_task(jid)
    assert visited == ['b.mp4', 'a.mp4', 'c.mp4']
    with session_scope() as s:
        assert s.get(Job, jid).stats['done'] == 3


def test_detect_uses_snapshot_not_live_settings(client, tmp_path, monkeypatch):
    pid = project(client)
    source = tmp_path / 'video.mp4'; source.write_bytes(b'source')
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path=str(source), display_name='video.mp4', scan_status='succeeded')
        s.add(asset); s.flush()
        job = Job(project_id=pid, job_type='detect', config_snapshot={
            'sample_fps': 2, 'threshold': .7, 'max_segment_sec': 11, 'thumb_width': 123, 'force': True})
        s.add(job); s.flush(); jid = job.id
    used = []
    monkeypatch.setattr(analyzer, '_analyze_asset', lambda p, a, settings=None, job_id=None: used.append(settings))
    analyzer.detect_task(jid)
    assert used[0].detect_threshold == .7
    assert used[0].detect_sample_fps == 2
    assert used[0].thumb_width == 123


def test_resume_preserves_failures_then_retry_resolves_them(client, tmp_path, monkeypatch):
    pid = project(client)
    root = tmp_path / 'src'; root.mkdir()
    first, second = root / 'a.mp4', root / 'b.mp4'
    first.write_bytes(b'a'); second.write_bytes(b'b')
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(root)]})
        s.add(job); s.flush(); jid = job.id
    def fail(_, path):
        if path.name == 'b.mp4':
            raise RuntimeError('broken')
        return 'a', 'created'
    monkeypatch.setattr(indexer, 'index_one', fail)
    indexer.scan_task(jid)
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.stats['failed'] == 1 and job.failures['count'] == 1
    indexer.scan_task(jid)
    with session_scope() as s:
        assert s.get(Job, jid).failures['count'] == 1
    calls = []
    monkeypatch.setattr(indexer, 'index_one', lambda _, path: (calls.append(path.name) or 'b', 'created'))
    indexer.scan_task(jid)
    assert calls == ['b.mp4']
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.stats == {'total': 2, 'done': 2, 'failed': 0, 'skipped': 0, 'source_changed': 0}
        assert job.failures['count'] == 0
        job.status = 'succeeded'


def test_completed_item_removed_is_invalidated_not_silently_skipped(client, tmp_path, monkeypatch):
    pid = project(client)
    path = tmp_path / 'a.mp4'; path.write_bytes(b'a')
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)]})
        s.add(job); s.flush(); jid = job.id
    monkeypatch.setattr(indexer, 'index_one', lambda *_: ('a', 'created'))
    indexer.scan_task(jid)
    path.unlink()
    indexer.scan_task(jid)
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.stats['failed'] == 1
        assert job.resume_token['items'][0]['status'] == 'failed'
        assert '源文件已变化' in job.resume_token['warning']
        job.status = 'partial_failed'


def test_full_hash_detects_changes_outside_fast_samples(tmp_path):
    from app.services.checkpoints import source_signature
    path = tmp_path / 'large.mp4'; path.write_bytes(b'a' * 1000000)
    stat = path.stat()
    quick = source_signature(path)
    full = source_signature(path, full_hash=True)
    with path.open('r+b') as stream:
        stream.seek(200000); stream.write(b'changed')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert source_signature(path) == quick  # documented limitation of sampling
    assert source_signature(path, full_hash=True) != full
