"""RT272: adversarial delete targets must be refused before disk mutation."""
from pathlib import Path

import pytest
from app.paths import project_cache_dir
from app.services import staging_cleanup as c


@pytest.mark.parametrize('relative', [
    'roughcut/history', 'roughcut/output', 'proxies/staging', 'staging/asset',
    'roughcut/staging-', 'frames/staging-evil', 'packages/staging-evil',
    'roughcut/staging-a/child', 'proxies/staging/a/child',
])
def test_non_leaf_shapes_are_rejected_without_changing_bytes(client, relative):
    pid = client.post('/api/projects', json={'name': 'shape'}).json()['id']
    target = project_cache_dir(pid) / relative
    target.mkdir(parents=True)
    protected = target / 'keep.bin'; protected.write_bytes(b'published unchanged')
    result = c.cleanup_owned_staging(pid, target, owner_job_id='invented-owner')
    assert result.error_code == 'STAGING_NOT_OWNED'
    assert protected.read_bytes() == b'published unchanged'


def test_internal_stage_alias_cannot_delete_another_stage(client):
    pid = client.post('/api/projects', json={'name': 'internal-link'}).json()['id']
    root = project_cache_dir(pid) / 'roughcut'; root.mkdir(parents=True)
    other = root / 'staging-other'; other.mkdir()
    secret = other / 'keep.bin'; secret.write_bytes(b'other job')
    alias = root / 'staging-alias'
    try: alias.symlink_to(other, target_is_directory=True)
    except OSError: pytest.skip('link creation is a platform fact')
    result = c.cleanup_owned_staging(pid, alias, owner_job_id='forged')
    assert result.error_code == 'STAGING_NOT_OWNED'
    assert secret.read_bytes() == b'other job'

from app.database import session_scope
from app.models import Asset, Job
from app.ffmpeg import long_path, safe_open
import os
import subprocess


def owned(client, shape='roughcut/staging-run', output_dir=None):
    body = {'name': 'owner'}
    if output_dir: body['output_dir'] = str(output_dir)
    pid = client.post('/api/projects', json=body).json()['id']
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path='/virtual/source.mp4')
        job = Job(project_id=pid, job_type='detect', status='running')
        s.add_all([asset, job]); s.flush(); aid, jid = asset.id, job.id
    shape = shape.replace('<asset>', aid)
    stage = c.create_owned_staging(pid, project_cache_dir(pid) / shape, owner_job_id=jid)
    with safe_open(stage / 'owned.bin', 'wb') as f: f.write(b'owned bytes')
    return pid, jid, stage


@pytest.mark.parametrize('shape', ['roughcut/staging-run', 'proxies/staging/token', 'staging/<asset>/token'])
def test_exact_owned_shapes_delete_only_their_files(client, shape):
    pid, jid, stage = owned(client, shape)
    root = project_cache_dir(pid)
    keep = root / 'frames' / 'published.jpg'; keep.parent.mkdir(); keep.write_bytes(b'published')
    result = c.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert result.ok and result.removed_bytes == len(b'owned bytes')
    assert keep.read_bytes() == b'published'
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok


def test_different_jobs_cannot_adopt_or_delete_each_others_stage(client):
    pid, jid, stage = owned(client)
    with session_scope() as s:
        other = Job(project_id=pid, job_type='detect', status='running')
        s.add(other); s.flush(); other_id = other.id
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=other_id).error_code == 'STAGING_NOT_OWNED'
    with pytest.raises(FileExistsError): c.create_owned_staging(pid, stage, owner_job_id=other_id)
    assert (stage / 'owned.bin').read_bytes() == b'owned bytes'
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok


def test_cross_project_owner_and_asset_are_rejected(client):
    pid, jid, stage = owned(client)
    other, other_jid, other_stage = owned(client)
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=other_jid).error_code == 'STAGING_NOT_OWNED'
    assert c.cleanup_owned_staging(other, stage, owner_job_id=jid).error_code == 'STAGING_NOT_OWNED'
    with pytest.raises(c.StagingNotOwned):
        c.create_owned_staging(pid, project_cache_dir(pid) / 'staging' / 'foreign-asset' / 'run', owner_job_id=jid)
    assert (stage / 'owned.bin').read_bytes() == (other_stage / 'owned.bin').read_bytes() == b'owned bytes'


@pytest.mark.parametrize('suffix', ['/../roughcut/staging-run', '/./roughcut/staging-run', '//roughcut/staging-run'])
def test_raw_path_segments_are_checked_before_normalization(client, suffix):
    pid, jid, stage = owned(client)
    result = c.cleanup_owned_staging(pid, str(project_cache_dir(pid)) + suffix, owner_job_id=jid)
    assert result.error_code == 'STAGING_NOT_OWNED'
    assert (stage / 'owned.bin').read_bytes() == b'owned bytes'


def link_directory(alias, target):
    if os.name == 'nt':
        proc = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(target)], capture_output=True)
        assert proc.returncode == 0, proc.stderr
    else: alias.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize('internal', [True, False])
def test_middle_parent_link_is_refused_without_following(client, tmp_path, internal):
    pid, jid, stage = owned(client)
    parent = stage.parent
    moved = parent.with_name('other-parent') if internal else tmp_path / 'outside-parent'
    parent.rename(moved); link_directory(parent, moved)
    result = c.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert result.error_code == 'STAGING_NOT_OWNED'
    assert (moved / stage.name / 'owned.bin').read_bytes() == b'owned bytes'


def test_link_swap_after_preflight_is_blocked_by_identity_recheck(client, monkeypatch):
    pid, jid, stage = owned(client)
    victim = stage.with_name('staging-victim'); victim.mkdir(); (victim / 'keep').write_bytes(b'victim')
    moved = stage.with_name('staging-original')
    original = c._iter_tree
    def swap(path):
        files = original(path)
        stage.rename(moved); link_directory(stage, victim)
        return files
    monkeypatch.setattr(c, '_iter_tree', swap)
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=jid).error_code == 'STAGING_NOT_OWNED'
    assert (victim / 'keep').read_bytes() == b'victim'
    assert (moved / 'owned.bin').read_bytes() == b'owned bytes'


@pytest.mark.parametrize('length', [270, 320])
def test_owned_chinese_stage_beyond_max_path_is_really_removed(client, tmp_path, length):
    output = tmp_path
    while len(str(output)) < length: output /= '旅行工作区' + 'd' * 20
    os.makedirs(long_path(output))
    pid, jid, stage = owned(client, output_dir=output)
    assert len(str(stage)) > length
    result = c.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert result.ok and result.removed_bytes == len(b'owned bytes')
    assert not os.path.exists(long_path(stage))


def test_unreadable_scan_is_failure_not_empty_success(client, monkeypatch):
    pid, jid, stage = owned(client)
    def unavailable(_): raise PermissionError('injected listing failure')
    monkeypatch.setattr(c, '_listdir', unavailable)
    result = c.cleanup_owned_staging(pid, stage, owner_job_id=jid)
    assert not result.ok and result.error_code == 'CLEANUP_FAILED' and result.retryable
    assert result.residual_paths and (stage / 'owned.bin').read_bytes() == b'owned bytes'


@pytest.mark.skipif(os.name != 'nt', reason='requires Windows')
def test_windows_open_file_reports_real_residual_bytes(client):
    import ctypes
    from ctypes import wintypes
    pid, jid, stage = owned(client)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(long_path(stage / 'owned.bin'), 0x80000000, 1, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        result = c.cleanup_owned_staging(pid, stage, owner_job_id=jid)
        assert not result.ok and result.error_code == 'CLEANUP_FAILED'
        assert result.retryable and result.residual_bytes == len(b'owned bytes')
        assert result.residual_count == 1
    finally: kernel.CloseHandle(handle)
    assert c.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
