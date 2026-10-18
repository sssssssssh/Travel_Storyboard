"""TSB-357/358: real API -> real parent/child Runner -> FFmpeg -> Mock HTTP -> DB.

Only the HTTP transport and native capability result are mocked. Never directly
invoke a registered task, assign a successful Job status, or use user footage.
"""
from __future__ import annotations
import base64
import hashlib
import io
import json
import re
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse
import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from app.database import session_scope
from app.models import Asset, Job, Segment, SegmentMotionReview, SegmentVideoObservation, SegmentMotionVerdict, UserAnnotation
from app.services.runner import JobRunner, TERMINAL_STATUSES, project_lock
from tests._media import needs_ffmpeg, require_ffmpeg
from conftest import apply_mock_model_contract
pytestmark = needs_ffmpeg

def wait_for(condition, *, description, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail(f'timed out waiting for {description}')

@pytest.fixture()
def fallback_env(client, monkeypatch, tmp_path):
    from app.ffmpeg import run
    from app.services import job_service
    from app.services.vision import video_job as video, motion
    from app.services.vision.video_capabilities import VideoCapabilities, UNSUPPORTED
    settings = apply_mock_model_contract(monkeypatch)
    monkeypatch.setattr(settings, 'model_allow_insecure_http', False)
    monkeypatch.setattr(settings, 'model_max_retries', 0)
    monkeypatch.setattr(settings, 'motion_frame_count', 3)
    monkeypatch.setattr(settings, 'vision_target_width', 96)
    monkeypatch.setattr(video, 'current_capabilities', lambda : VideoCapabilities(state=UNSUPPORTED))
    source = tmp_path / 'synthetic-source.mp4'
    run([require_ffmpeg(), '-y', '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=10:duration=3', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], expect_output=source)
    source_before = (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size, source.stat().st_mtime_ns)
    response = client.post('/api/projects', json={'name': 'r3.2-synthetic', 'source_roots': [], 'output_dir': str(tmp_path / 'workspace')})
    assert response.status_code == 201, response.text
    pid = response.json()['id']
    aid = str(uuid.uuid4())
    ids = [str(uuid.uuid4()) for _ in range(3)]
    with session_scope() as s:
        s.add(Asset(id=aid, project_id=pid, source_path=str(source), display_name=source.name, duration_sec=3, file_size_bytes=source.stat().st_size, mtime_ns=source.stat().st_mtime_ns, width=96, height=64, fps_num=10, fps_den=1, scan_status='succeeded'))
        s.flush()
        for (index, sid) in enumerate(ids):
            (start, end) = [(0.2, 1.8), (2, 2.8), (0, 0.15)][index]
            s.add(Segment(id=sid, project_id=pid, asset_id=aid, start_sec=start, end_sec=end, detected_start_sec=0 if index == 0 else start, detected_end_sec=2 if index == 0 else end, is_trimmed=index == 0, user_rating='favorite', story_role='开场', status='ready'))
        s.flush()
        s.add(UserAnnotation(segment_id=ids[0], note='human note survives', rating='favorite', in_sec=0.2, out_sec=1.8))
        old = SegmentMotionReview(project_id=pid, asset_id=aid, segment_id=ids[0], method='multiframe', motion_type='unknown', action_summary='historical reading', is_current=True, human_verdict='unclear', human_note='historical human judgement')
        s.add(old)
        s.flush()
        old_id = old.id
    own = JobRunner()
    monkeypatch.setattr(job_service, 'runner', own)
    gate = threading.Event()
    gate.set()
    in_provider = threading.Event()
    reply = threading.Event()
    reply.set()
    extracted = []
    extract = motion.extract_frame

    def track_extract(*args, **kwargs):
        extracted.append((args, kwargs))
        return extract(*args, **kwargs)
    monkeypatch.setattr(motion, 'extract_frame', track_extract)
    seen = []
    entries = []
    behaviour = {'mode': 'ok'}
    parent_id = []
    child_ids = []
    real_run = own._run

    def traced_run(jid, task):
        with session_scope() as s:
            row = s.get(Job, jid)
            kind = row.job_type
            status = row.status
        entries.append((jid, kind, status))
        if kind == 'motion_review':
            child_ids.append(jid)
            assert gate.wait(15), 'test child release gate timed out'
        return real_run(jid, task)
    monkeypatch.setattr(own, '_run', traced_run)

    def post(url, **kwargs):
        assert urlparse(url).hostname == '127.0.0.1' and urlparse(url).port == 9, 'non-Mock request forbidden'
        raw = kwargs['content']
        body = json.loads(raw)
        parts = body['messages'][-1]['content']
        images = [p['image_url']['url'] for p in parts if p.get('type') == 'image_url']
        prompt = ' '.join((p.get('text', '') for p in parts))
        assert len(images) >= 2
        for uri in images:
            assert uri.startswith('data:image/jpeg;base64,')
            with Image.open(io.BytesIO(base64.b64decode(uri.split(',', 1)[1]))) as im:
                im.load()
                assert not im.getexif()
        assert str(source).encode() not in raw
        seen.append({'images': len(images), 'prompt': prompt, 'host': '127.0.0.1', 'body_size': len(raw)})
        in_provider.set()
        assert reply.wait(15), 'test model response gate timed out'
        if behaviour['mode'] == 'error' or (behaviour['mode'] == 'partial' and len(seen) == 2):
            return httpx.Response(400, json={'error': {'message': 'mock rejection'}}, request=httpx.Request('POST', url))
        times = [float(t) for t in re.findall('t=([\\d.]+)s', prompt)]
        content = json.dumps({'motion_type': 'pan', 'stability_risk': 'low', 'action_summary': 'synthetic motion reading', 'evidence_timestamps_sec': times, 'limitations': ['Mock only'], 'needs_human_review': True})
        if behaviour['mode'] == 'invalid':
            content = 'not a JSON object'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}], 'usage': {'total_tokens': 9}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(httpx, 'post', post)

    def read(jid):
        with session_scope() as s:
            return s.get(Job, jid)

    def start(*, ack=True, selected=None, limit=1):
        r = client.post(f'/api/projects/{pid}/jobs/video_analyze', json={'mode': 'video_native', 'allow_fallback': True, 'upload_acknowledged': ack, 'segment_ids': selected or [ids[0]], 'limit': limit, 'force': True})
        assert r.status_code == 202, r.text
        parent_id.append(r.json()['id'])
        return r.json()['id']

    def terminal(jid):

        def ready():
            j = read(jid)
            return j if j.status in TERMINAL_STATUSES else None
        return wait_for(ready, description=f'Job {jid} terminal; entries={entries}')

    def finish(jid):
        parent = terminal(jid)
        sid = (parent.stats or {}).get('fallback_job_id')
        assert sid, parent.stats
        child = terminal(sid)
        wait_for(lambda : all((f.done() for f in list(own._futures.values()))), description='own Runner futures done')
        return (parent, child)

    def results():
        with session_scope() as s:
            return s.scalars(select(SegmentMotionReview).where(SegmentMotionReview.project_id == pid, SegmentMotionReview.id != old_id)).all()
    env = SimpleNamespace(pid=pid, aid=aid, ids=ids, source=source, source_before=source_before, old_id=old_id, runner=own, extracted=extracted, gate=gate, reply=reply, in_provider=in_provider, seen=seen, entries=entries, behaviour=behaviour, read=read, start=start, finish=finish, terminal=terminal, results=results, child_ids=child_ids, client=client)
    try:
        yield env
    finally:
        gate.set()
        reply.set()
        for future in list(own._futures.values()):
            future.result(timeout=30)
        if own._executor is not None:
            own._executor.shutdown(wait=True)

def assert_complete(env, parent, child):
    assert parent.status == 'succeeded', parent.failures
    assert child.status == 'succeeded', child.failures
    assert parent.started_at and parent.finished_at and child.started_at and child.finished_at
    assert child.started_at >= parent.finished_at
    assert child.stats['analyzed'] == 1
    rows = env.results()
    assert len(rows) == 1
    row = rows[0]
    assert row.segment_id == env.ids[0] and row.asset_id == env.aid
    assert row.method == 'multiframe' and row.action_summary and row.motion_type
    assert row.evidence_json['frames'] and all((0.2 < f['time_sec'] < 1.8 for f in row.evidence_json['frames']))
    fb = env.client.get(f'/api/jobs/{parent.id}').json()['fallback']
    assert fb['analysed'] is True and fb['result_count'] == 1
    public = env.client.get(f'/api/projects/{env.pid}/motion-reviews').json()
    assert any((r['id'] == row.id and r['segment_id'] == env.ids[0] for r in public))
    with session_scope() as s:
        assert s.scalars(select(SegmentVideoObservation).where(SegmentVideoObservation.project_id == env.pid)).all() == []
    return row

def test_b01_no_upload_consent_means_no_frames_and_no_model_request(fallback_env):
    e = fallback_env
    (parent, child) = e.finish(e.start(ack=False))
    assert child.status == 'failed'
    assert 'MODEL_UPLOAD_DENIED' in child.error
    assert e.seen == [] and e.results() == []
    assert e.extracted == []

def test_b02_api_runs_parent_and_exactly_one_child(fallback_env):
    e = fallback_env
    (parent, child) = e.finish(e.start())
    assert_complete(e, parent, child)
    with session_scope() as s:
        assert len(s.scalars(select(Job).where(Job.project_id == e.pid, Job.job_type == 'motion_review')).all()) == 1

def test_b03_successor_pending_running_and_terminal_are_real_runner_states(fallback_env):
    e = fallback_env
    e.gate.clear()
    e.reply.clear()
    jid = e.start()
    parent = e.terminal(jid)
    sid = parent.stats['fallback_job_id']
    assert e.read(sid).status == 'pending'
    assert e.client.post(f'/api/projects/{e.pid}/jobs/scan').status_code == 409
    e.gate.set()
    assert e.in_provider.wait(15)
    assert e.read(sid).status == 'running'
    assert e.read(sid).started_at is not None
    e.reply.set()
    (parent, child) = e.finish(jid)
    assert_complete(e, parent, child)
    assert e.runner._futures[jid].done() and e.runner._futures[sid].done()

def test_b04_actual_provider_receives_multiple_authorized_derived_jpegs(fallback_env):
    e = fallback_env
    (parent, child) = e.finish(e.start())
    assert e.seen and e.seen[0]['images'] == 3
    assert child.config_snapshot['upload_acknowledged']
    assert e.seen[0]['host'] == '127.0.0.1'
    assert '0.20-1.80s' in e.seen[0]['prompt']

def test_b05_same_selected_segment_is_persisted_and_publicly_queryable(fallback_env):
    e = fallback_env
    (parent, child) = e.finish(e.start())
    assert_complete(e, parent, child)

def test_b06_selection_limit_force_and_consent_reach_the_successor(fallback_env):
    e = fallback_env
    (parent, child) = e.finish(e.start())
    assert parent.config_snapshot['segment_ids'] == child.config_snapshot['segment_ids'] == [e.ids[0]]
    assert child.config_snapshot['limit'] == 1
    assert child.config_snapshot['upload_acknowledged'] and child.config_snapshot.get('force') is True
    assert {r.segment_id for r in e.results()} == {e.ids[0]}

def test_b07_source_human_fields_and_historical_judgement_survive(fallback_env):
    e = fallback_env
    e.finish(e.start())
    reading = e.results()[0]
    response = e.client.post(f'/api/segments/{e.ids[0]}/motion-review/verdict', json={'verdict': 'unclear', 'note': 'append-only human verdict survives', 'motion_review_id': reading.id, 'input_signature': reading.input_signature})
    assert response.status_code == 200, response.text
    with session_scope() as s:
        verdict = s.scalar(select(SegmentMotionVerdict).where(SegmentMotionVerdict.segment_id == e.ids[0]))
        preserved = (verdict.id, verdict.motion_review_id, verdict.input_signature, verdict.note, verdict.verdict)
    e.finish(e.start())
    with session_scope() as s:
        verdicts = s.scalars(select(SegmentMotionVerdict).where(SegmentMotionVerdict.segment_id == e.ids[0])).all()
        assert len(verdicts) == 1
        v = verdicts[0]
        assert preserved == (v.id, v.motion_review_id, v.input_signature, v.note, v.verdict)
    assert (hashlib.sha256(e.source.read_bytes()).hexdigest(), e.source.stat().st_size, e.source.stat().st_mtime_ns) == e.source_before
    with session_scope() as s:
        shot = s.get(Segment, e.ids[0])
        assert (shot.start_sec, shot.end_sec, shot.detected_start_sec, shot.detected_end_sec, shot.user_rating, shot.story_role) == (0.2, 1.8, 0, 2, 'favorite', '开场')
        note = s.scalar(select(UserAnnotation).where(UserAnnotation.segment_id == e.ids[0]))
        assert note.note == 'human note survives'
        old = s.get(SegmentMotionReview, e.old_id)
        assert (old.human_verdict, old.human_note) == ('unclear', 'historical human judgement')

def test_b08_completed_child_releases_lock_and_cleans_only_owned_stage(fallback_env):
    from app.paths import project_cache_dir
    from app.services.vision.motion import stage_dir
    e = fallback_env
    outside = project_cache_dir(e.pid) / 'vision' / 'motion' / 'foreign-job'
    outside.mkdir(parents=True)
    (outside / 'keep.txt').write_text('keep', encoding='utf-8')
    (parent, child) = e.finish(e.start())
    assert child.stats['cleanup']['ok']
    assert not stage_dir(e.pid, child.id).exists()
    assert (outside / 'keep.txt').read_text(encoding='utf-8') == 'keep'
    lock = project_lock(e.pid)
    assert lock.acquire(blocking=False)
    lock.release()

@pytest.mark.parametrize('failure', ['model', 'invalid-output', 'ffmpeg', 'schedule'])
def test_b09_failures_are_visible_and_never_claim_stored_results(fallback_env, monkeypatch, failure):
    from app.services.vision import motion, video_job
    from app.ffmpeg import FFmpegError
    e = fallback_env
    if failure == 'model':
        e.behaviour['mode'] = 'error'
    elif failure == 'invalid-output':
        e.behaviour['mode'] = 'invalid'
    elif failure == 'ffmpeg':
        monkeypatch.setattr(motion, 'extract_frame', lambda *a, **kw: (_ for _ in ()).throw(FFmpegError('synthetic decode failure', code='MEDIA_UNREADABLE')))
    else:
        from app.services import job_service
        original = job_service._create_locked

        def fail_child(db, pid, request, builder, **kwargs):
            if request.job_type == 'motion_review':
                raise RuntimeError('private URL must not escape')
            return original(db, pid, request, builder, **kwargs)
        monkeypatch.setattr(job_service, '_create_locked', fail_child)
        jid = e.start()
        parent = e.terminal(jid)
        fb = e.client.get(f'/api/jobs/{jid}').json()['fallback']
        assert parent.status == 'partial_failed' and (not fb['analysed'])
        assert fb['state'] == 'failed' and fb['error_code'] == 'FALLBACK_SCHEDULE_FAILED'
        assert 'private URL' not in e.client.get(f'/api/jobs/{jid}').text
        assert e.seen == [] and e.results() == []
        return
    (parent, child) = e.finish(e.start())
    assert child.status in ('failed', 'partial_failed')
    fb = e.client.get(f'/api/jobs/{parent.id}').json()['fallback']
    assert not fb['analysed']
    assert fb['error_code']
    assert e.results() == []

def test_b10_noop_store_cannot_turn_a_successful_job_into_a_reading(fallback_env, monkeypatch):
    from app.services.vision import motion
    e = fallback_env
    monkeypatch.setattr(motion, '_store', lambda *a, **kw: None)
    (parent, child) = e.finish(e.start())
    assert child.status == 'succeeded'
    assert child.stats['analyzed'] == 1 and e.results() == []
    fb = e.client.get(f'/api/jobs/{parent.id}').json()['fallback']
    assert fb['analysed'] is False
    assert fb['result_count'] == 0
    assert fb['error_code'] == 'FALLBACK_NO_RESULTS'

def test_b08_queued_child_cancelled_before_any_media_or_upload(fallback_env):
    e = fallback_env
    e.gate.clear()
    jid = e.start()
    parent = e.terminal(jid)
    sid = parent.stats['fallback_job_id']
    assert e.read(sid).status == 'pending'
    assert e.client.post(f'/api/jobs/{sid}/cancel').json()['status'] == 'cancelling'
    e.gate.set()
    (parent, child) = e.finish(jid)
    assert child.status == 'cancelled' and child.started_at is None
    assert not e.seen and (not e.results())
    assert e.extracted == []

def test_b08_running_child_cancelled_and_cleans_its_stage(fallback_env):
    e = fallback_env
    e.reply.clear()
    jid = e.start()
    assert e.in_provider.wait(15)
    sid = e.child_ids[0]
    assert e.read(sid).status == 'running'
    assert e.client.post(f'/api/jobs/{jid}/cancel').status_code == 409
    assert e.read(sid).status == 'running'
    assert e.client.post(f'/api/jobs/{sid}/cancel').json()['status'] == 'cancelling'
    e.reply.set()
    (parent, child) = e.finish(jid)
    assert child.status == 'cancelled'
    assert child.stats['cleanup']['ok']
    assert e.results() == []
    assert e.client.get(f'/api/jobs/{jid}').json()['fallback']['analysed'] is False

def test_s04_partial_success_reports_only_the_persisted_subset(fallback_env):
    e = fallback_env
    e.behaviour['mode'] = 'partial'
    (parent, child) = e.finish(e.start(selected=e.ids[:2], limit=2))
    assert child.status == 'partial_failed'
    assert len(e.results()) == 1
    fb = e.client.get(f'/api/jobs/{parent.id}').json()['fallback']
    assert fb['analysed'] is False
    assert fb['result_count'] == 1
    assert fb['error_code']

def test_b10_media_suite_is_explicitly_forced_in_ci():
    from pathlib import Path
    import shlex
    text = (Path(__file__).resolve().parents[2] / '.github/workflows/ci.yml').read_text(encoding='utf-8')
    block = re.search(r'        run: >\n((?:          .*\n)+)', text)
    assert block, 'mandatory media must have an identifiable folded command'
    # YAML folds these equally indented lines into ONE shell command. An inline
    # comment therefore eats every argument after it on both bash and PowerShell.
    command = ' '.join(line.strip() for line in block[1].splitlines())
    arguments = shlex.split(command, comments=True)
    registered = set(re.findall(r'tests/test_\w+\.py', command))
    assert {'tests/test_video_fallback_runner_e2e.py', 'tests/test_fallback_result_contract.py'} <= registered
    assert registered <= set(arguments), f'mandatory suites hidden by shell comments: {sorted(registered - set(arguments))}'
