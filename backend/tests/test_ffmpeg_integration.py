"""Real FFmpeg pipeline; required CI mode fails on missing binaries/codecs."""

from __future__ import annotations

import subprocess
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.ffmpeg import (
    extract_frame,
    ffmpeg_available,
    ffprobe_available,
    locate_ffprobe,
    probe_media,
    run,
)
from app.models import Asset, Frame, Segment
from app.paths import frames_dir, project_cache_dir
from app.services.analyzer import _detect_config_key, _analyze_asset, _detected_key
from app.services.indexer import index_one
from app.services.roughcut import _clamp_duration
from app.config import get_settings


def _ffmpeg_or_skip() -> str:
    """Return a *usable* ffmpeg path or skip.

    ``locate_ffmpeg()`` falls back to the bare string ``"ffmpeg"`` so callers can
    build a command line before checking; that is not proof the binary exists.
    Use the same availability probe the health endpoint uses.
    """
    exe = ffmpeg_available()
    if not exe or not ffprobe_available():
        if os.environ.get("TSB_REQUIRE_FFMPEG") == "1":
            pytest.fail("mandatory FFmpeg integration requires ffmpeg AND ffprobe")
        pytest.skip("ffmpeg/ffprobe not available")
    return exe


@pytest.fixture(scope="module")
def synthetic_clip(tmp_path_factory) -> Path:
    """A 6-second 640x360 clip with three distinct colour sections + audio.

    Built with ffmpeg's own generators so the fixture has no binary dependency.
    """
    exe = _ffmpeg_or_skip()
    out = tmp_path_factory.mktemp("media") / "sample.mp4"
    cmd = [
        exe, "-y",
        # 2s red, 2s green, 2s blue (concatenated so there are real hard cuts)
        "-f", "lavfi", "-i", "color=c=red:s=640x360:d=2:r=30",
        "-f", "lavfi", "-i", "color=c=green:s=640x360:d=2:r=30",
        "-f", "lavfi", "-i", "color=c=blue:s=640x360:d=2:r=30",
        # A quiet tone so has_audio is true.
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        pytest.fail(f"could not synthesise a clip: {proc.stderr.decode()[-1000:]}")
    return out


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "ffmpeg-it", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


# --- probing -----------------------------------------------------------------

def test_probe_reads_real_metadata(synthetic_clip: Path):
    _ffmpeg_or_skip()
    info = probe_media(synthetic_clip)
    assert info.duration_sec == pytest.approx(6.0, abs=0.5)
    assert info.has_video is True
    assert info.video.width == 640 and info.video.height == 360
    assert info.has_audio is True


def test_index_one_succeeds_on_a_real_file(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    asset_id, action = index_one(project["id"], synthetic_clip)
    assert action in ("created", "updated"), action

    with session_scope() as s:
        asset = s.get(Asset, asset_id)
        assert asset.scan_status == "succeeded"
        assert asset.duration_sec > 0
        assert asset.width == 640


def test_reindexing_does_not_duplicate(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    first, _ = index_one(project["id"], synthetic_clip)
    second, _ = index_one(project["id"], synthetic_clip)
    assert first == second

    with session_scope() as s:
        count = s.scalar(
            select(func.count()).select_from(Asset).where(Asset.project_id == project["id"])
        )
    assert count == 1


# --- frame extraction --------------------------------------------------------

def test_extract_frame_writes_a_readable_jpeg(tmp_path: Path, synthetic_clip: Path):
    _ffmpeg_or_skip()
    from PIL import Image

    out = tmp_path / "frame.jpg"
    extract_frame(synthetic_clip, 1.0, out, width=320)
    assert out.is_file() and out.stat().st_size > 0
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.size[0] == 320


def test_extract_preview_with_audio(tmp_path: Path, synthetic_clip: Path):
    _ffmpeg_or_skip()
    from app.ffmpeg import extract_preview

    out = tmp_path / "preview.mp4"
    extract_preview(synthetic_clip, 0.5, 2.0, out, width=320, keep_audio=True)
    assert out.is_file()

    probe = locate_ffprobe()
    proc = run([
        probe, "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(out),
    ])
    assert b"aac" in proc.stdout.lower() or b"mp4a" in proc.stdout.lower()


# --- detection + analysis end-to-end ----------------------------------------

def test_detect_finds_the_three_colour_sections(client, project, synthetic_clip: Path):
    """The synthetic clip has two hard cuts, so detection should find ~3 shots."""
    _ffmpeg_or_skip()
    from app.services.detector import detect_segments

    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)

    st = get_settings()
    detection = detect_segments(
        asset.source_path,
        asset.duration_sec,
        sample_fps=st.detect_sample_fps,
        threshold=st.detect_threshold,
        max_segment_sec=st.detect_max_segment_sec,
    )
    assert len(detection.boundaries) >= 2, [b.reason for b in detection.boundaries]
    # Boundaries must tile the clip without gaps.
    for a, b in zip(detection.boundaries, detection.boundaries[1:]):
        assert abs(a.end_sec - b.start_sec) < 0.01


def test_analyze_asset_creates_segments_frames_and_survives_rerun(
    client, project, synthetic_clip: Path
):
    """Full analysis, then a forced re-run: ids and annotations must persist."""
    _ffmpeg_or_skip()
    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)

    first = _analyze_asset(project["id"], asset)
    assert first["segments"] >= 2
    assert first["frames"] >= 2

    with session_scope() as s:
        segs = s.scalars(
            select(Segment).where(Segment.project_id == project["id"]).order_by(Segment.start_sec)
        ).all()
        target = segs[0]
        target_id = target.id
        detected = (target.detected_start_sec, target.detected_end_sec)
        # Simulate a user trim + favourite.
        span = target.end_sec - target.start_sec
        target.start_sec = target.start_sec + span * 0.25
        target.end_sec = target.end_sec - span * 0.25
        target.is_trimmed = True
        target.user_rating = "favorite"

    with session_scope() as s:
        asset = s.get(Asset, asset.id)
        s.expunge(asset)
    second = _analyze_asset(project["id"], asset)

    with session_scope() as s:
        again = s.get(Segment, target_id)
        assert again is not None, "trimmed segment was destroyed by re-analysis"
        assert again.user_rating == "favorite"
        assert again.is_trimmed is True
        assert (again.detected_start_sec, again.detected_end_sec) == detected
        assert _detected_key(again) == _detected_key(
            type(again)(
                asset_id=again.asset_id,
                project_id=again.project_id,
                detected_start_sec=detected[0],
                detected_end_sec=detected[1],
            )
        )

    # Frame cache must not grow across runs.
    frames_on_disk = list(frames_dir(project["id"], asset.id).glob("*.jpg"))
    with session_scope() as s:
        rows = s.scalar(
            select(func.count()).select_from(Frame).where(Frame.asset_id == asset.id)
        )
    assert len(frames_on_disk) == rows, (len(frames_on_disk), rows)
    assert second.get("frames_reclaimed", 0) >= 0


def test_detect_config_key_is_stable():
    st = get_settings()
    assert _detect_config_key(st) == _detect_config_key(st)


# --- rough cut duration clamping --------------------------------------------

def test_clamp_duration_never_exceeds_source(synthetic_clip: Path):
    _ffmpeg_or_skip()
    from app.models import EditPlanShot

    asset = Asset(
        id=str(uuid.uuid4()),
        project_id="p",
        source_path=str(synthetic_clip),
        display_name="s.mp4",
        duration_sec=6.0,
        has_audio=True,
    )
    # Planned longer than the selection -> clamped to the selection.
    shot = EditPlanShot(
        project_id="p", order=0, source_in_sec=1.0, source_out_sec=3.0,
        target_duration_sec=30.0,
    )
    assert _clamp_duration(shot, asset) == pytest.approx(2.0)

    # Selection running past the end of the file -> clamped to what exists.
    shot2 = EditPlanShot(
        project_id="p", order=1, source_in_sec=4.0, source_out_sec=10.0,
        target_duration_sec=6.0,
    )
    assert _clamp_duration(shot2, asset) == pytest.approx(2.0)

    # A normal shortened shot is honoured.
    shot3 = EditPlanShot(
        project_id="p", order=2, source_in_sec=0.0, source_out_sec=4.0,
        target_duration_sec=1.5,
    )
    assert _clamp_duration(shot3, asset) == pytest.approx(1.5)


def test_project_cache_dir_exists_after_analysis(client, project, synthetic_clip: Path):
    _ffmpeg_or_skip()
    index_one(project["id"], synthetic_clip)
    with session_scope() as s:
        asset = s.scalars(select(Asset).where(Asset.project_id == project["id"])).first()
        s.expunge(asset)
    _analyze_asset(project["id"], asset)
    root = project_cache_dir(project["id"])
    assert (root / "frames").is_dir()


def test_complete_custom_workspace_pipeline_and_source_hashes(client, synthetic_clip, tmp_path, record_property):
    """RT21/24 substitute: three generated files, real media through all stages.

    This verifies source immutability on generated fixtures, not Windows travel
    footage acceptance. The optional model response is deterministic and local.
    """
    import hashlib
    import shutil
    from app.models import Job, ReviewRequest
    from app.services.runner import JobRunner, TASK_REGISTRY
    from app.services.story_planner import _select_representative_frames, import_review_requests
    from app.services.timeline_export import build_edit_plan, export_json
    exe = _ffmpeg_or_skip()
    source = tmp_path / "旅行原片's"; source.mkdir()
    clips = [source / '横屏音轨.mp4', source / '竖屏静音.mp4', source / '4K_HEVC.mp4']
    shutil.copyfile(synthetic_clip, clips[0])
    run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=yellow:s=180x320:d=2:r=30',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(clips[1])])
    run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=3840x2160:d=1:r=3',
         '-c:v', 'libx265', '-preset', 'ultrafast', '-x265-params', 'pools=2',
         '-pix_fmt', 'yuv420p', str(clips[2])])
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in clips]
    project = client.post('/api/projects', json={
        'name': '综合链路', 'source_roots': [str(source)], 'output_dir': str(tmp_path / "工作区's")}).json()
    pid = project['id']
    def execute(kind, config=None):
        with session_scope() as s:
            job = Job(project_id=pid, job_type=kind, config_snapshot=config or {})
            s.add(job); s.flush(); jid = job.id
        JobRunner()._run(jid, TASK_REGISTRY[kind])
        row = client.get(f'/api/jobs/{jid}').json()
        assert row['status'] == 'succeeded', row
        return row
    execute('scan', {'source_roots': [str(source)]})
    detected = execute('detect')
    assert detected['stats']['done'] == 3
    repeat = execute('detect')
    assert repeat['stats']['skipped'] == 3
    execute('package')
    picked, coverage = _select_representative_frames(pid, 12, 60)
    assert coverage['assets_covered_by_images'] == 3
    assert coverage['complete'] is True
    first = picked[0]
    assert import_review_requests(pid, {'requests': [{
        'asset_id': first['asset_id'], 'segment_id': first['segment_id'],
        'start_sec': first['start_sec'], 'end_sec': first['end_sec'], 'priority': 'high'}]})['created'] == 1
    execute('proxies')
    with session_scope() as s:
        req = s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).one()
        proxy = req.proxy_ref
    assert client.get(f'/api/projects/{pid}/media', params={'path': proxy}).status_code == 200
    shots = [{'asset_id': p['asset_id'], 'segment_id': p['segment_id'],
              'source_in_sec': p['start_sec'], 'source_out_sec': p['end_sec'],
              'target_duration_sec': min(1., p['end_sec'] - p['start_sec'])} for p in picked]
    assert build_edit_plan(pid, shots)['errors'] == []
    before = export_json(pid)['shots']
    assert client.get(f'/api/projects/{pid}/edit-plan').json()['shots'] == before
    roughcut = execute('roughcut')
    preview = client.get(f'/api/projects/{pid}/roughcut')
    assert preview.status_code == 200 and preview.headers['content-type'] == 'video/mp4'
    from app.paths import resolve_in_project
    info = probe_media(resolve_in_project(pid, roughcut['resume_token']['path']))
    assert info.has_audio
    assert info.duration_sec == pytest.approx(sum(s['target_duration_sec'] for s in shots), abs=.25)
    after_hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in clips]
    assert after_hashes == hashes
    for i, value in enumerate(hashes):
        record_property(f'synthetic_source_{i}_sha256_before', value)
        record_property(f'synthetic_source_{i}_sha256_after', after_hashes[i])
    record_property('generated_input_duration_sec', 9)
    record_property('roughcut_duration_sec', info.duration_sec)
    assert export_json(pid)['shots'] == before


def test_package_replay_reuses_committed_artifact(client, project, synthetic_clip):
    from app.models import Job, ReviewPackage
    from app.services.review_package import package_task
    aid, _ = index_one(project['id'], synthetic_clip)
    with session_scope() as s:
        asset = s.get(Asset, aid); s.expunge(asset)
    _analyze_asset(project['id'], asset)
    with session_scope() as s:
        job = Job(project_id=project['id'], job_type='package')
        s.add(job); s.flush(); jid = job.id
    package_task(jid)
    with session_scope() as s:
        before = s.get(Job, jid).resume_token
    package_task(jid)
    with session_scope() as s:
        rows = s.scalars(select(ReviewPackage).where(ReviewPackage.project_id == project['id'])).all()
        assert len(rows) == 1
        assert s.get(Job, jid).resume_token == before
        s.get(Job, jid).status = 'succeeded'


def test_real_ffprobe_force_and_resume_call_counts(client, project, synthetic_clip, monkeypatch):
    from app.models import Job
    from app.services import indexer
    from app.services.runner import JobRunner
    actual_probe, calls = indexer.probe_media, []
    def count(path):
        calls.append(str(path)); return actual_probe(path)
    monkeypatch.setattr(indexer, 'probe_media', count)
    aid, action = index_one(project['id'], synthetic_clip)
    assert action == 'created' and len(calls) == 1
    assert index_one(project['id'], synthetic_clip)[1] == 'skipped'
    assert len(calls) == 1
    with session_scope() as s:
        job = Job(project_id=project['id'], job_type='scan', config_snapshot={
            'source_roots': [str(synthetic_clip.parent)], 'force': True})
        s.add(job); s.flush(); jid = job.id
    indexer.scan_task(jid)
    assert len(calls) == 2
    indexer.scan_task(jid)
    assert len(calls) == 2
    with session_scope() as s:
        assert s.scalars(select(Asset).where(Asset.project_id == project['id'])).one().id == aid
        assert s.get(Job, jid).stats['forced_reprobe'] == 1
        s.get(Job, jid).status = 'succeeded'


def test_generated_ten_minute_chinese_path_with_audio(client, tmp_path, record_property):
    """RT16 surrogate: generated long clip, not user Windows travel footage."""
    import hashlib
    from types import SimpleNamespace
    from app.ffmpeg import extract_preview
    from app.paths import rel_to_data
    exe = _ffmpeg_or_skip()
    root = tmp_path / '中文 长视频'; root.mkdir()
    path = root / '十分钟 环境音.mp4'
    run([exe, '-y', '-f', 'lavfi', '-i', 'color=c=orange:s=160x90:d=601:r=1',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=601', '-c:v', 'libx264',
         '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '32k', '-shortest', str(path)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    pid = client.post('/api/projects', json={'name': 'generated-long', 'output_dir': str(tmp_path / '中文 工作区')}).json()['id']
    aid, action = index_one(pid, path)
    assert action == 'created'
    with session_scope() as s:
        asset = s.get(Asset, aid); s.expunge(asset)
    assert asset.duration_sec >= 600 and asset.has_audio
    settings = SimpleNamespace(detect_sample_fps=1, detect_threshold=.35, detect_max_segment_sec=300, thumb_width=160)
    result = _analyze_asset(pid, asset, settings=settings)
    assert result['segments'] >= 3
    preview = project_cache_dir(pid) / 'proxies' / 'long-preview.mp4'
    preview.parent.mkdir(parents=True, exist_ok=True)
    extract_preview(path, 590, 2, preview, width=160, keep_audio=True)
    assert probe_media(preview).has_audio
    assert client.get(f'/api/projects/{pid}/media', params={'path': rel_to_data(preview)}).status_code == 200
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    record_property('generated_long_source_duration_sec', asset.duration_sec)
    record_property('generated_long_source_sha256_unchanged', before)


@pytest.mark.parametrize('custom', [False, True])
def test_real_short_review_proxy_is_exact_readonly_and_project_scoped(client, synthetic_clip, tmp_path, custom, record_property):
    """RT-10/17: inspect generated video bytes, not just the FFmpeg arguments."""
    import hashlib
    from app.models import Job, ReviewRequest
    from app.paths import resolve_in_project
    from app.services.runner import JobRunner
    from app.services.story_planner import import_review_requests, proxies_task
    pid = client.post('/api/projects', json={'name': 'short-preview',
        'output_dir': str(tmp_path / '自定义 output') if custom else ''}).json()['id']
    before = hashlib.sha256(synthetic_clip.read_bytes()).hexdigest()
    aid, _ = index_one(pid, synthetic_clip)
    with session_scope() as s:
        segment = Segment(project_id=pid, asset_id=aid, start_sec=2, end_sec=5,
                          detected_start_sec=0, detected_end_sec=6)
        s.add(segment); s.flush(); sid = segment.id
    assert import_review_requests(pid, {'requests': [
        {'asset_id': aid, 'segment_id': sid, 'start_sec': 3, 'end_sec': 3.6}]})['created'] == 1
    with session_scope() as s:
        job = Job(project_id=pid, job_type='proxies'); s.add(job); s.flush(); jid = job.id
    JobRunner()._run(jid, proxies_task)
    with session_scope() as s:
        request = s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).one()
        job = s.get(Job, jid)
        assert request.status == 'proxied' and job.status == 'succeeded'
        path, rid = resolve_in_project(pid, request.proxy_ref), request.id
        assert job.stats['files_generated'] == 1 and job.stats['bytes'] == path.stat().st_size
        assert job.stats['requested_duration_sec'] == pytest.approx(.6)
        assert job.stats['output_duration_sec'] == pytest.approx(.6, abs=.08)
    info = probe_media(path)
    assert info.has_video and info.duration_sec == pytest.approx(.6, abs=.08)
    assert info.duration_sec < .8  # A forced one-second proxy must fail.
    # Decode the actual stream, including audio, as part of mandatory media CI.
    run([_ffmpeg_or_skip(), '-v', 'error', '-i', str(path), '-f', 'null', '-'])
    response = client.get(f'/api/projects/{pid}/review-requests/{rid}/proxy', headers={'Range': 'bytes=0-127'})
    assert response.status_code == 206 and len(response.content) == 128
    other = client.post('/api/projects', json={'name': 'other-project'}).json()['id']
    assert client.get(f'/api/projects/{other}/review-requests/{rid}/proxy').status_code == 404
    assert client.get(f'/api/projects/{other}/media', params={'path': str(path)}).status_code == 400
    assert hashlib.sha256(synthetic_clip.read_bytes()).hexdigest() == before
    record_property('requested_duration_sec', .6)
    record_property('output_duration_sec', info.duration_sec)
    record_property('output_bytes', path.stat().st_size)
    record_property('source_sha256_before_after', before)
    record_property('workspace', 'custom' if custom else 'default')
