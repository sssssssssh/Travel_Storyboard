"""TSB-222: full hashing, source mutation and interruption through analysis."""
import pytest
from sqlalchemy import select
from app.database import session_scope
from app.models import Asset, Frame, Job, Segment
from app.services import analyzer, checkpoints, indexer
from app.services.runner import JobRunner
from test_concurrent_annotations import seeded  # noqa: F401


@pytest.mark.parametrize('strict,passes', [(False, 1), (True, 2)])
def test_detect_hashes_once_unless_strict_recheck_requested(client, seeded, monkeypatch, strict, passes):
    pid, asset, sid, _, _, _ = seeded
    with session_scope() as s:
        s.get(Asset, asset.id).scan_status = 'succeeded'
        job = Job(project_id=pid, job_type='detect', config_snapshot={'force': True, 'full_hash': True, 'strict_full_hash': strict})
        s.add(job); s.flush(); jid = job.id
    reader, reads = checkpoints.read_hash_block, []
    def count(stream, size):
        data = reader(stream, size); reads.append(len(data)); return data
    monkeypatch.setattr(checkpoints, 'read_hash_block', count)
    JobRunner()._run(jid, analyzer.detect_task)
    size = len(b'original-video')
    assert sum(reads) == size * passes
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'succeeded'
        assert row.stats['bytes_read'] == size * passes
        assert row.stats['strict_extra_bytes'] == size * (passes - 1)
        assert row.stats['hash_passes'] == passes


def test_source_mutation_during_analysis_preserves_old_frames(client, seeded, monkeypatch):
    pid, asset, sid, fid, path, _ = seeded
    with session_scope() as s:
        s.get(Asset, asset.id).scan_status = 'succeeded'
        s.get(Segment, sid).user_rating = 'favorite'
        job = Job(project_id=pid, job_type='detect', config_snapshot={'force': True, 'full_hash': True})
        s.add(job); s.flush(); jid = job.id
    original = analyzer.extract_frame
    changed = []
    def mutate(*args, **kwargs):
        original(*args, **kwargs)
        if not changed:
            changed.append(True)
            with open(asset.source_path, 'ab') as stream: stream.write(b'changed externally')
    monkeypatch.setattr(analyzer, 'extract_frame', mutate)
    JobRunner()._run(jid, analyzer.detect_task)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.status == 'partial_failed'
        assert row.resume_token['items'][0]['error_code'] == 'source_changed'
        assert s.get(Frame, fid) is not None and path.is_file()
        assert s.get(Segment, sid).user_rating == 'favorite'


def test_crash_mid_hash_resumes_only_unfinished_file(client, tmp_path, monkeypatch):
    pid = client.post('/api/projects', json={'name': 'hash-restart'}).json()['id']
    for name, count in (('a.mp4', 1), ('b.mp4', 3)):
        (tmp_path / name).write_bytes(b'a' * (count * 1024 * 1024))
    with session_scope() as s:
        job = Job(project_id=pid, job_type='scan', config_snapshot={'full_hash': True, 'force': True, 'source_roots': [str(tmp_path)]})
        s.add(job); s.flush(); jid = job.id
    monkeypatch.setattr(indexer, 'index_one', lambda p, path, *, force=False: ('asset', 'created'))
    reader, reads = checkpoints.read_hash_block, []
    def crash(stream, size):
        if str(stream.name).endswith('b.mp4') and stream.tell() >= 1024 * 1024:
            raise KeyboardInterrupt('simulated process interruption')
        data = reader(stream, size); reads.append((stream.name, len(data))); return data
    monkeypatch.setattr(checkpoints, 'read_hash_block', crash)
    with pytest.raises(KeyboardInterrupt): indexer.scan_task(jid)
    with session_scope() as s:
        row = s.get(Job, jid)
        assert [i['status'] for i in row.resume_token['items']] == ['completed', 'pending']
        assert row.stats['done'] == 1
    def resumed(stream, size):
        data = reader(stream, size); reads.append((stream.name, len(data))); return data
    monkeypatch.setattr(checkpoints, 'read_hash_block', resumed)
    indexer.scan_task(jid)
    assert sum(n for p, n in reads if p.endswith('a.mp4')) == 1024 * 1024
    assert sum(n for p, n in reads if p.endswith('b.mp4')) == 4 * 1024 * 1024
    with session_scope() as s:
        row = s.get(Job, jid)
        assert row.stats['bytes_read'] == 5 * 1024 * 1024  # actual IO includes interrupted attempt
        assert row.stats['done'] == 2
        row.status = 'succeeded'
