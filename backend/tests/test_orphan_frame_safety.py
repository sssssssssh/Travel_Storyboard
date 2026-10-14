"""RT275: aged orphan JPEGs only; current references and stages are immutable."""
import os
import time
from pathlib import Path
import pytest
from app.ffmpeg import long_path, safe_open, safe_stat
from app.paths import project_cache_dir, rel_to_data
from app.database import session_scope
from app.models import Asset, Segment, Frame, Job
from app.services import analyzer
from test_staging_ownership import link_directory


@pytest.mark.parametrize('length', [270, 320])
def test_deep_chinese_old_orphan_removed_but_referenced_young_and_stage_kept(client, tmp_path, length):
    output = tmp_path
    while len(str(output)) < length: output /= '旅行缓存' + 'e' * 24
    os.makedirs(long_path(output))
    pid = client.post('/api/projects', json={'name': 'orphan-deep', 'output_dir': str(output)}).json()['id']
    root = project_cache_dir(pid); frame_root = root / 'frames' / 'asset'
    os.makedirs(long_path(frame_root))
    paths = [frame_root / name for name in ['old.jpg', 'keep.jpg', 'young.jpg']]
    stage = root / 'staging' / 'asset' / 'active' / 'stage.jpg'
    os.makedirs(long_path(stage.parent)); paths.append(stage)
    for path in paths:
        with safe_open(path, 'wb') as f: f.write(path.name.encode())
    old_time = time.time() - 7200
    for path in [paths[0], paths[1], stage]: os.utime(long_path(path), (old_time, old_time))
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path='/virtual/read-only.mp4'); s.add(asset); s.flush()
        seg = Segment(project_id=pid, asset_id=asset.id, frame_refs=[rel_to_data(paths[1])]); s.add(seg); s.flush()
        s.add(Frame(asset_id=asset.id, segment_id=seg.id, image_path=rel_to_data(paths[1])))
    assert analyzer.cleanup_orphan_frames(pid) == 1
    assert not os.path.exists(long_path(paths[0]))
    for path in paths[1:]:
        with safe_open(path, 'rb') as f: assert f.read() == path.name.encode()


@pytest.mark.parametrize('layout', ['root', 'nested'])
def test_frame_root_and_nested_directory_links_are_never_followed(client, tmp_path, caplog, layout):
    pid = client.post('/api/projects', json={'name': 'orphan-link'}).json()['id']
    root = project_cache_dir(pid); root.mkdir(parents=True)
    outside = tmp_path / 'outside'; outside.mkdir()
    precious = outside / 'old.jpg'; precious.write_bytes(b'original')
    os.utime(precious, (0, 0))
    if layout == 'root': alias = root / 'frames'
    else:
        (root / 'frames').mkdir()
        alias = root / 'frames' / 'linked-asset'
    link_directory(alias, outside)
    assert analyzer.cleanup_orphan_frames(pid) == 0
    assert precious.read_bytes() == b'original'
    assert 'ORPHAN' in caplog.text


def test_orphan_listing_error_is_observable_and_retryable(client, monkeypatch, caplog):
    pid = client.post('/api/projects', json={'name': 'orphan-unreadable'}).json()['id']
    root = project_cache_dir(pid) / 'frames'; root.mkdir(parents=True)
    old = root / 'old.jpg'; old.write_bytes(b'orphan'); os.utime(old, (0, 0))
    original = analyzer._orphan_entries
    def unavailable(*args): raise PermissionError('test directory busy')
    monkeypatch.setattr(analyzer, '_orphan_entries', unavailable)
    assert analyzer.cleanup_orphan_frames(pid) == 0
    assert 'ORPHAN_CLEANUP_FAILED' in caplog.text and old.read_bytes() == b'orphan'
    monkeypatch.setattr(analyzer, '_orphan_entries', original)
    assert analyzer.cleanup_orphan_frames(pid) == 1


def test_active_detection_defers_old_orphan_deletion(client):
    pid = client.post('/api/projects', json={'name': 'orphan-active'}).json()['id']
    root = project_cache_dir(pid) / 'frames'; root.mkdir(parents=True)
    old = root / 'in-flight.jpg'; old.write_bytes(b'publishing'); os.utime(old, (0, 0))
    with session_scope() as s:
        s.add(Job(project_id=pid, job_type='detect', status='cancelling'))
    assert analyzer.cleanup_orphan_frames(pid) == 0
    assert old.read_bytes() == b'publishing'
