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
    def process(_, path, *, force=False):
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
    def fail(_, path, *, force=False):
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
    monkeypatch.setattr(indexer, 'index_one', lambda _, path, *, force=False: (calls.append(path.name) or 'b', 'created'))
    indexer.scan_task(jid)
    assert calls == ['b.mp4']
    with session_scope() as s:
        job = s.get(Job, jid)
        assert {k: job.stats[k] for k in ('total', 'done', 'failed', 'skipped', 'source_changed')} == {'total': 2, 'done': 2, 'failed': 0, 'skipped': 0, 'source_changed': 0}
        assert job.failures['count'] == 0
        job.status = 'succeeded'


def test_completed_item_removed_is_invalidated_not_silently_skipped(client, tmp_path, monkeypatch):
    pid = project(client)
    path = tmp_path / 'a.mp4'; path.write_bytes(b'a')
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)]})
        s.add(job); s.flush(); jid = job.id
    monkeypatch.setattr(indexer, 'index_one', lambda *a, **kw: ('a', 'created'))
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


def test_force_scan_reprobes_once_and_resume_keeps_completed(client, tmp_path, monkeypatch):
    pid = project(client)
    source = tmp_path / 'force.mp4'; source.write_bytes(b'unchanged')
    probed = []
    def probe(asset, path, size, mtime):
        probed.append(str(path))
        asset.file_size_bytes, asset.mtime_ns = size, mtime
        asset.quick_fingerprint = indexer.quick_fingerprint(path, size)
        asset.scan_status = 'succeeded'
    monkeypatch.setattr(indexer, '_apply_probe_fields', probe)
    first, _ = indexer.index_one(pid, source)
    def run(force):
        with session_scope() as s:
            job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)], 'force': force})
            s.add(job); s.flush(); jid = job.id
        indexer.scan_task(jid)
        return jid
    regular = run(False)
    assert len(probed) == 1
    forced = run(True)
    assert len(probed) == 2
    indexer.scan_task(forced)
    assert len(probed) == 2
    with session_scope() as s:
        assert s.get(Job, forced).stats['forced_reprobe'] == 1
        assert s.get(Job, regular).stats['skipped_unchanged'] == 1
        assert s.scalars(select(Asset).where(Asset.project_id == pid)).one().id == first
        s.get(Job, regular).status = 'succeeded'; s.get(Job, forced).status = 'succeeded'


def test_full_hash_once_per_scan_and_cached_on_same_job_resume(client, tmp_path, monkeypatch):
    from app.services import checkpoints
    pid = project(client)
    source = tmp_path / 'large.mp4'; source.write_bytes(b'a' * (3 * 1024 * 1024))
    reads = []
    original = checkpoints.hashlib.sha256
    # Count actual full-file block reads, independent of quick signature hashing.
    reader = getattr(checkpoints, 'read_hash_block', None)
    assert reader is not None, 'full hashing needs an observable cancellable block reader'
    def count(stream, size):
        block = reader(stream, size); reads.append(len(block)); return block
    monkeypatch.setattr(checkpoints, 'read_hash_block', count)
    monkeypatch.setattr(indexer, 'index_one', lambda p, path, *, force=False: ('a', 'created'))
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)], 'full_hash': True})
        s.add(job); s.flush(); jid = job.id
    indexer.scan_task(jid)
    assert sum(reads) == source.stat().st_size
    indexer.scan_task(jid)
    assert sum(reads) == source.stat().st_size
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.stats['bytes_read'] == source.stat().st_size
        assert row.stats['signature_mode'] == 'full'
        assert row.stats['processed_files'] == row.stats['total_files'] == 1
        row.status = 'succeeded'


def test_hash_cancellation_stops_before_reading_rest_and_preserves_checkpoint(client, tmp_path, monkeypatch):
    from app.services import checkpoints
    from app.services.runner import update_job
    import pytest
    pid = project(client)
    source = tmp_path / 'large.mp4'; source.write_bytes(b'a' * (4 * 1024 * 1024))
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', status='running', config_snapshot={'source_roots': [str(tmp_path)], 'full_hash': True})
        s.add(job); s.flush(); jid = job.id
    reader = getattr(checkpoints, 'read_hash_block', None)
    assert reader is not None
    read = []
    def cancel(stream, size):
        data = reader(stream, size)
        read.append(len(data))
        update_job(jid, status='cancelled')
        return data
    monkeypatch.setattr(checkpoints, 'read_hash_block', cancel)
    with pytest.raises(JobCancelled): indexer.scan_task(jid)
    assert 0 < sum(read) < source.stat().st_size
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'cancelled'
        assert row.resume_token['items'][0]['status'] == 'pending'
        assert row.stats['bytes_read'] == sum(read)


def test_source_changed_during_hash_is_failed_before_probe(client, tmp_path, monkeypatch):
    from app.services import checkpoints
    pid = project(client)
    source = tmp_path / 'large.mp4'; source.write_bytes(b'a' * (3 * 1024 * 1024))
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path)], 'full_hash': True})
        s.add(job); s.flush(); jid = job.id
    reader = getattr(checkpoints, 'read_hash_block', None)
    assert reader is not None
    modified = []
    def change(stream, size):
        data = reader(stream, size)
        if not modified:
            modified.append(True)
            with source.open('r+b') as out: out.seek(1000000); out.write(b'changed')
        return data
    monkeypatch.setattr(checkpoints, 'read_hash_block', change)
    probed = []
    monkeypatch.setattr(indexer, 'index_one', lambda *a, **kw: probed.append(True))
    indexer.scan_task(jid)
    assert probed == []
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.stats['failed'] == 1
        assert row.resume_token['items'][0]['error_code'] == 'source_changed'
        row.status = 'partial_failed'


def test_source_change_during_probe_rolls_back_derived_metadata(client, tmp_path, monkeypatch):
    from app.models import Frame, Segment
    pid = project(client)
    source = tmp_path / 'changing.mp4'; source.write_bytes(b'original')
    def probe(asset, path, size, mtime):
        asset.file_size_bytes, asset.mtime_ns = size, mtime
        asset.quick_fingerprint = indexer.quick_fingerprint(path, size)
        asset.duration_sec = 10
        asset.scan_status = 'succeeded'
    monkeypatch.setattr(indexer, '_apply_probe_fields', probe)
    aid, _ = indexer.index_one(pid, source)
    def changed_probe(asset, path, size, mtime):
        probe(asset, path, size, mtime)
        asset.duration_sec = 100
        path.write_bytes(b'changed outside')
    monkeypatch.setattr(indexer, '_apply_probe_fields', changed_probe)
    again, action = indexer.index_one(pid, source, force=True)
    assert again == aid and action == 'failed'
    with session_scope() as s:
        row = s.get(Asset, aid)
        assert row.duration_sec == 10
        assert row.error_code == 'source_changed'


def test_missing_source_root_is_not_reported_as_empty_success(client, tmp_path):
    pid = project(client)
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(tmp_path / 'missing')]})
        s.add(job); s.flush(); jid = job.id
    from app.services.runner import JobRunner
    JobRunner()._run(jid, indexer.scan_task)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'partial_failed'
        assert row.stats['failed'] == 1


def test_unreadable_source_root_reports_failure(client, tmp_path, monkeypatch):
    pid = project(client)
    root = tmp_path / 'locked'; root.mkdir()
    original = indexer.iter_video_files
    def denied(roots, check_cancel=None):
        if roots == [str(root)]: raise PermissionError('source directory denied')
        return original(roots, check_cancel=check_cancel)
    monkeypatch.setattr(indexer, 'iter_video_files', denied)
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'source_roots': [str(root)]})
        s.add(job); s.flush(); jid = job.id
    from app.services.runner import JobRunner
    JobRunner()._run(jid, indexer.scan_task)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'partial_failed'
        assert row.resume_token['items'][0]['error_code'] == 'source_unavailable'


def test_windows_stat_and_fstat_creation_identity_are_canonical():
    from app.services.checkpoints import file_identity
    from types import SimpleNamespace
    common = dict(st_size=100, st_mtime_ns=200, st_dev=3, st_ino=4, st_birthtime_ns=50)
    path_stat = SimpleNamespace(**common, st_ctime_ns=50)
    handle_stat = SimpleNamespace(**common, st_ctime_ns=999)
    assert file_identity(path_stat, windows=True) == file_identity(handle_stat, windows=True)
    assert file_identity(path_stat, windows=False) != file_identity(handle_stat, windows=False)
    for field in ('st_size', 'st_mtime_ns', 'st_dev', 'st_ino', 'st_birthtime_ns'):
        changed = {**common, field: common[field] + 1}
        assert file_identity(path_stat, windows=True) != file_identity(SimpleNamespace(**changed, st_ctime_ns=999), windows=True)
