"""TSB-207 fault injection against actual extracted JPEGs and DB transactions."""
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import event, select
from app.database import session_scope
from app.models import Asset, Frame
from app.paths import frames_dir, project_cache_dir, rel_to_data, resolve_in_project
from app.services import analyzer
from app.services.indexer import index_one
from test_ffmpeg_integration import synthetic_clip  # noqa: F401


def seed(client, synthetic_clip):
    pid = client.post('/api/projects', json={'name': 'cache-recovery'}).json()['id']
    aid, _ = index_one(pid, synthetic_clip)
    with session_scope() as s:
        asset = s.get(Asset, aid); s.expunge(asset)
    analyzer._analyze_asset(pid, asset)
    with session_scope() as s:
        before = [(f.id, f.image_path) for f in s.scalars(select(Frame).where(Frame.asset_id == aid))]
    return pid, asset, before


@pytest.mark.parametrize('fault', ['extract', 'publish', 'commit'])
def test_failed_replacement_preserves_old_frames(client, synthetic_clip, monkeypatch, fault):
    pid, asset, before = seed(client, synthetic_clip)
    if fault == 'commit':
        def fail(*_):
            raise RuntimeError('injected DB failure')
        event.listen(Frame, 'before_insert', fail)
    elif fault == 'extract':
        original = analyzer.extract_frame
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('injected extract failure')
        monkeypatch.setattr(analyzer, 'extract_frame', fail)
    else:
        original = Path.replace
        calls = []
        def fail(path, target):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError('injected publish failure')
            return original(path, target)
        monkeypatch.setattr(Path, 'replace', fail)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            analyzer._analyze_asset(pid, asset)
    finally:
        if fault == 'commit':
            event.remove(Frame, 'before_insert', fail)
    with session_scope() as s:
        after = [(f.id, f.image_path) for f in s.scalars(select(Frame).where(Frame.asset_id == asset.id))]
    assert after == before
    assert all(resolve_in_project(pid, path).is_file() for _, path in before)


def test_cleanup_failure_does_not_rollback_new_version(client, synthetic_clip, monkeypatch):
    pid, asset, before = seed(client, synthetic_clip)
    old_paths = {resolve_in_project(pid, path) for _, path in before}
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path in old_paths:
            raise OSError('injected cleanup failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail)
    analyzer._analyze_asset(pid, asset)
    with session_scope() as s:
        after = [(f.id, f.image_path) for f in s.scalars(select(Frame).where(Frame.asset_id == asset.id))]
    assert after != before
    assert all(resolve_in_project(pid, path).is_file() for _, path in after)
    assert Path(asset.source_path).is_file()


def test_orphan_cleanup_only_old_unreferenced_inside_project(client, synthetic_clip, tmp_path):
    pid, asset, before = seed(client, synthetic_clip)
    old = frames_dir(pid, asset.id) / 'crash-orphan.jpg'
    young = old.with_name('young.jpg')
    old.write_bytes(b'orphan'); young.write_bytes(b'young')
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    outside = tmp_path / 'outside.jpg'; outside.write_bytes(b'original')
    link = old.with_name('link.jpg')
    try:
        link.symlink_to(outside)
    except OSError:
        pass  # Windows symlink privilege is optional; outside still checked.
    analyzer.cleanup_orphan_frames(pid)
    assert not old.exists() and young.exists()
    assert outside.read_bytes() == b'original'
    assert all(resolve_in_project(pid, path).is_file() for _, path in before)
