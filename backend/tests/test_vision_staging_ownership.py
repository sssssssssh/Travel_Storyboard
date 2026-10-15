"""TSB-318: both workers use registered, exact Job-owned visual staging."""
import os
from pathlib import Path
import uuid

import pytest
from PIL import Image

from app.database import session_scope
from app.ffmpeg import safe_open, long_path
from app.models import Job
from app.paths import project_cache_dir
from app.services import staging_cleanup as cleanup
from app.services.runner import JobCancelled, JobRunner
from app.services.vision import motion, semantic
from app.services.vision.errors import ModelError
from test_vision_semantics import project, stubbed, _make_job, _job_row  # noqa: F401
from test_vision_cache_versions import seed
from test_staging_ownership import link_directory


def visual_stage(pid, kind):
    jid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Job(id=jid, project_id=pid, job_type=kind, status='running'))
    shape = 'staging' if kind == 'vision_analyze' else 'motion'
    path = project_cache_dir(pid) / 'vision' / shape / jid
    return jid, path


@pytest.mark.parametrize('kind', ['vision_analyze', 'motion_review'])
def test_exact_visual_stage_owns_bytes_and_releases_registry(project, kind):
    pid = project['id']; jid, path = visual_stage(pid, kind)
    cleanup.create_owned_staging(pid, path, owner_job_id=jid)
    with safe_open(path / 'frame.jpg', 'wb') as f: f.write(b'owned JPEG')
    result = cleanup.cleanup_staging_or_log(pid, path, owner_job_id=jid)
    assert result.ok and result.removed_bytes == len(b'owned JPEG')
    assert path not in cleanup._OWNERS
    assert cleanup.cleanup_staging_or_log(pid, path, owner_job_id=jid).ok
    with session_scope() as s:
        assert s.get(Job, jid).stats['cleanup']['ok']


@pytest.mark.parametrize('pipeline', ['semantic', 'motion'])
@pytest.mark.parametrize('outcome', ['success', 'model_error', 'cancel', 'locked', 'stats_error'])
def test_workers_finally_report_real_cleanup(pipeline, outcome, project, stubbed, tmp_path, monkeypatch):
    pid = project['id']; data = seed(project, tmp_path, 1); sid = data['segments'][0]['segment_id']
    jid = _make_job(pid, segment_ids=[sid])
    if pipeline == 'motion':
        with session_scope() as s: s.get(Job, jid).job_type = 'motion_review'
        def sample(*args):
            staging = args[3]; staging.mkdir(parents=True, exist_ok=True)
            frames = []
            for i in range(2):
                path = staging / f'{i}.jpg'; Image.new('RGB', (16, 16), 'green').save(path)
                frames.append(motion.MotionFrame(str(path), i + 1.))
            return frames
        monkeypatch.setattr(motion, '_sample_frames', sample)
        def infer(*a, **kw):
            if outcome == 'model_error': raise ModelError('MODEL_TIMEOUT', 'timeout')
            if outcome == 'cancel':
                with session_scope() as s: s.get(Job, jid).status = 'cancelling'
                raise JobCancelled('cancelled during inference')
            return {'motion_type': 'pan', 'stability_risk': 'low', 'action_summary': 'view',
                    'evidence_timestamps_sec': [1., 2.], 'limitations': [], 'needs_human_review': False}, {}
        monkeypatch.setattr(motion, '_analyze_motion', infer)
        task = motion.motion_review_task
        stage = motion.stage_dir(pid, jid)
    else:
        task = semantic.vision_analyze_task
        stage = semantic.stage_image_dir(pid, jid)
        if outcome in ('model_error', 'cancel'):
            def infer(*a, **kw):
                if outcome == 'model_error': raise ModelError('MODEL_TIMEOUT', 'timeout')
                with session_scope() as s: s.get(Job, jid).status = 'cancelling'
                raise ModelError('CANCELLED', 'cancelled during inference')
            monkeypatch.setattr(stubbed, 'complete', infer)
    if outcome == 'locked':
        monkeypatch.setattr(cleanup, '_remove_file', lambda *a: (_ for _ in ()).throw(PermissionError('locked')))
    if outcome == 'stats_error':
        worker = semantic if pipeline == 'semantic' else motion
        monkeypatch.setattr(worker, 'merge_job_stats', lambda *a: (_ for _ in ()).throw(RuntimeError('stats unavailable')))
    JobRunner()._run(jid, task)
    row = _job_row(jid)
    assert 'cleanup' in row.stats
    if outcome == 'locked':
        assert row.status == 'partial_failed'
        assert not row.stats['cleanup']['ok']
        assert row.stats['cleanup']['residual_bytes'] > 0
        assert row.stats['cleanup']['retryable']
        assert stage.exists()
        monkeypatch.undo()
        assert cleanup.cleanup_staging_or_log(pid, stage, owner_job_id=jid).ok
    else:
        assert row.stats['cleanup']['ok'] and not stage.exists()
        assert row.status == {'success': 'succeeded', 'model_error': 'partial_failed',
                              'cancel': 'cancelled', 'stats_error': 'failed'}[outcome]


@pytest.mark.parametrize('kind', ['vision_analyze', 'motion_review'])
def test_sibling_and_restarted_visual_stages_are_not_deleted(project, kind):
    pid = project['id']
    jid, own = visual_stage(pid, kind)
    other_jid, sibling = visual_stage(pid, kind)
    for owner, path in ((jid, own), (other_jid, sibling)):
        cleanup.create_owned_staging(pid, path, owner_job_id=owner)
        with safe_open(path / 'keep', 'wb') as f: f.write(b'original bytes')
    assert cleanup.cleanup_owned_staging(pid, sibling, owner_job_id=jid).error_code == 'STAGING_NOT_OWNED'
    assert cleanup.cleanup_owned_staging(pid, own, owner_job_id=jid).ok
    with safe_open(sibling / 'keep', 'rb') as f: assert f.read() == b'original bytes'
    cleanup._OWNERS.pop(sibling)  # Simulate loss of the process-local registration.
    assert cleanup.cleanup_owned_staging(pid, sibling, owner_job_id=other_jid).error_code == 'STAGING_NOT_OWNED'
    with pytest.raises(FileExistsError):
        cleanup.create_owned_staging(pid, sibling, owner_job_id=other_jid)
    with safe_open(sibling / 'keep', 'rb') as f: assert f.read() == b'original bytes'


def test_foreign_job_wrong_kind_existing_and_restart_stages_are_never_adopted(project):
    pid = project['id']; jid, stage = visual_stage(pid, 'vision_analyze')
    stage.parent.mkdir(parents=True, exist_ok=True); stage.mkdir()
    protected = stage / 'keep'; protected.write_bytes(b'unowned remains')
    with pytest.raises(FileExistsError): cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
    assert cleanup.cleanup_staging_or_log(pid, stage, owner_job_id=jid).error_code == 'STAGING_NOT_OWNED'
    assert protected.read_bytes() == b'unowned remains'
    with session_scope() as s: s.get(Job, jid).job_type = 'detect'
    with pytest.raises(cleanup.StagingNotOwned): cleanup.create_owned_staging(pid, stage, owner_job_id=jid)


def test_swapped_visual_stage_link_does_not_touch_outside(project, tmp_path):
    pid = project['id']; jid, stage = visual_stage(pid, 'motion_review')
    cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
    moved = stage.with_name(jid + '-moved'); stage.rename(moved)
    outside = tmp_path / 'outside'; outside.mkdir(); keep = outside / 'keep'; keep.write_bytes(b'never delete')
    link_directory(stage, outside)
    result = cleanup.cleanup_staging_or_log(pid, stage, owner_job_id=jid)
    assert result.error_code == 'STAGING_NOT_OWNED'
    assert keep.read_bytes() == b'never delete'


@pytest.mark.parametrize('relative', ['vision', 'vision/staging', 'vision/motion', 'vision/other/job', 'vision/staging/job/child'])
def test_visual_shape_expansion_does_not_authorize_parents(project, relative):
    pid = project['id']; path = project_cache_dir(pid) / relative
    path.mkdir(parents=True, exist_ok=True); keep = path / 'keep'; keep.write_bytes(b'published')
    assert cleanup.cleanup_owned_staging(pid, path, owner_job_id='forged').error_code == 'STAGING_NOT_OWNED'
    assert keep.read_bytes() == b'published'


def test_completed_receipt_cannot_delete_recreated_directory(project):
    pid = project['id']; jid, stage = visual_stage(pid, 'vision_analyze')
    cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
    assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
    stage.mkdir(); keep = stage / 'foreign'; keep.write_bytes(b'new owner')
    assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).error_code == 'STAGING_NOT_OWNED'
    assert keep.read_bytes() == b'new owner'


def test_success_registry_is_bounded_and_failure_evidence_is_retained(project, monkeypatch):
    pid = project['id']; baseline = len(cleanup._OWNERS)
    monkeypatch.setattr(cleanup, '_CLEANED_LIMIT', 4)
    for _ in range(8):
        jid, stage = visual_stage(pid, 'vision_analyze')
        cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
        assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
    assert len(cleanup._OWNERS) == baseline
    assert len(cleanup._CLEANED) <= 4
    jid, stage = visual_stage(pid, 'motion_review')
    cleanup.create_owned_staging(pid, stage, owner_job_id=jid)
    with safe_open(stage / 'frame', 'wb') as f: f.write(b'retry evidence')
    with monkeypatch.context() as m:
        m.setattr(cleanup, '_remove_file', lambda *a: (_ for _ in ()).throw(PermissionError('busy')))
        assert not cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok
    assert stage in cleanup._OWNERS
    assert cleanup.cleanup_owned_staging(pid, stage, owner_job_id=jid).ok


@pytest.mark.needs_ffmpeg
def test_real_motion_in_deep_unicode_workspace_keeps_source_and_history(client, tmp_path, monkeypatch):
    from test_vision_motion import _seed, _job, _StubProvider
    from _media import require_ffmpeg
    from app.ffmpeg import run, safe_mkdir
    from app.config import get_settings
    import hashlib
    exe = require_ffmpeg()
    source = tmp_path / 'source.mp4'
    run([exe, '-y', '-f', 'lavfi', '-i', 'testsrc=size=64x36:rate=5:duration=2',
         '-pix_fmt', 'yuv420p', str(source)], expect_output=source)
    original = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns)
    deep = tmp_path / '中文素材工作区'
    while len(str(deep)) < 275: deep = deep / '旅行画面分镜暂存目录'
    safe_mkdir(deep)
    p = client.post('/api/projects', json={'name': 'deep-visual', 'output_dir': str(deep)}).json()
    pid = p['id']; _seed(pid, source, start=0.1, end=1.9, duration=2)
    settings = get_settings()
    for key, value in {'model_mode': 'on', 'model_base_url': 'http://127.0.0.1:9/v1',
                       'model_api_key': 'deep-test-only-key', 'model_name': 'stub-model'}.items():
        monkeypatch.setattr(settings, key, value)
    import app.services.vision as package
    monkeypatch.setattr(package, 'get_provider', lambda: _StubProvider())
    keep = project_cache_dir(pid) / 'roughcut' / 'history.mp4'; safe_mkdir(keep.parent)
    with safe_open(keep, 'wb') as f: f.write(b'published history')
    jid = _job(pid, frame_count=2)
    JobRunner()._run(jid, motion.motion_review_task)
    row = _job_row(jid)
    assert row.status == 'succeeded', row.error
    assert row.stats['cleanup']['ok'] and row.stats['cleanup']['removed_files'] == 2
    assert not motion.stage_dir(pid, jid).exists()
    with safe_open(keep, 'rb') as f: assert f.read() == b'published history'
    assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mtime_ns) == original
