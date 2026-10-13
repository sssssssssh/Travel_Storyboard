"""TSB-221: deterministic user writes interleaved with frame extraction.

Pure generated JPEGs keep these tests in every Python/OS unit matrix. Actual
FFmpeg failures are additionally covered by the required media matrix.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select
from app.database import session_scope
from app.models import Asset, EditPlanShot, Frame, ReviewRequest, Segment, UserAnnotation
from app.paths import frames_dir, rel_to_data, resolve_in_project
from app.services import analyzer


@pytest.fixture()
def seeded(client, tmp_path, monkeypatch):
    pid = client.post('/api/projects', json={'name': 'concurrent-edit'}).json()['id']
    source = tmp_path / 'source.mp4'; source.write_bytes(b'original-video')
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path=str(source), duration_sec=10)
        s.add(asset); s.flush()
        seg = Segment(project_id=pid, asset_id=asset.id, start_sec=0, end_sec=10,
                      detected_start_sec=0, detected_end_sec=10, status='confirmed')
        s.add(seg); s.flush()
        path = frames_dir(pid, asset.id) / 'old.jpg'; path.parent.mkdir(parents=True)
        Image.new('RGB', (40, 30), 'red').save(path)
        seg.frame_refs = [rel_to_data(path)]
        frame = Frame(asset_id=asset.id, segment_id=seg.id, image_path=rel_to_data(path), time_sec=5)
        s.add(frame); s.flush()
        sid, fid = seg.id, frame.id
        s.expunge(asset)
    def detection(start=0, end=10):
        return SimpleNamespace(boundaries=[SimpleNamespace(start_sec=start, end_sec=end, reason='cut')],
                               sample_times=[], sample_diffs=[])
    monkeypatch.setattr(analyzer, 'detect_segments', lambda *a, **kw: detection())
    monkeypatch.setattr(analyzer, 'extract_frame', lambda source, time, out, **kw: Image.new('RGB', (40, 30), 'blue').save(out))
    return pid, asset, sid, fid, path, detection


@pytest.mark.parametrize('kind', ['rating', 'note', 'trim', 'role'])
def test_latest_manual_edit_survives_background_commit(client, seeded, monkeypatch, kind):
    pid, asset, sid, _, _, _ = seeded
    entered, release = Event(), Event()
    original = analyzer.extract_frame
    def paused(*a, **kw):
        entered.set(); assert release.wait(5)
        original(*a, **kw)
    monkeypatch.setattr(analyzer, 'extract_frame', paused)
    payload = {'rating': 'favorite', 'note': 'latest note', 'in_sec': 2, 'out_sec': 8, 'story_role': '高潮'}
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(analyzer._analyze_asset, pid, asset)
        assert entered.wait(5)
        try:
            response = client.post(f'/api/segments/{sid}/annotate', json=payload)
            assert response.status_code == 200
        finally:
            release.set()
        future.result(5)
    with session_scope() as s:
        row = s.get(Segment, sid)
        annotation = s.scalars(select(UserAnnotation).where(UserAnnotation.segment_id == sid)).one()
        assert row.status == 'confirmed'
        if kind == 'rating': assert row.user_rating == 'favorite'
        if kind == 'note': assert annotation.note == 'latest note'
        if kind == 'trim': assert (row.start_sec, row.end_sec, row.is_trimmed) == (2, 8, True)
        if kind == 'role': assert row.story_role == '高潮'
        assert all(resolve_in_project(pid, path).is_file() for path in row.frame_refs)


@pytest.mark.parametrize('protection', ['plan', 'review', 'annotation', 'rating', 'role', 'trim', 'confirmed'])
def test_changed_boundaries_retain_protected_segment_and_its_frames(client, seeded, monkeypatch, protection):
    pid, asset, sid, fid, old_path, detection = seeded
    with session_scope() as s:
        row = s.get(Segment, sid); row.status = 'candidate'
        if protection == 'plan': s.add(EditPlanShot(project_id=pid, asset_id=asset.id, segment_id=sid, order=0, source_in_sec=1, source_out_sec=5, target_duration_sec=4))
        if protection == 'review': s.add(ReviewRequest(project_id=pid, asset_id=asset.id, segment_id=sid))
        if protection == 'annotation': s.add(UserAnnotation(segment_id=sid, note='never lose'))
        if protection == 'rating': row.user_rating = 'favorite'
        if protection == 'role': row.story_role = '开场'
        if protection == 'trim': row.is_trimmed = True; row.start_sec = 2; row.end_sec = 8
        if protection == 'confirmed': row.status = 'confirmed'
    monkeypatch.setattr(analyzer, 'detect_segments', lambda *a, **kw: detection(0, 6))
    result = analyzer._analyze_asset(pid, asset)
    with session_scope() as s:
        row = s.get(Segment, sid)
        assert row is not None
        assert row.status == 'needs_review'
        assert s.get(Frame, fid) is not None and old_path.is_file()
        assert 'analysis_warning' in row.quality_metrics
    assert result['needs_review'] >= 1


def test_small_boundary_shift_clamps_latest_trim_with_audit(client, seeded, monkeypatch):
    pid, asset, sid, _, _, detection = seeded
    with session_scope() as s:
        row = s.get(Segment, sid); row.is_trimmed = True; row.start_sec = 2; row.end_sec = 10
    # Same rounded detector identity, but latest trim needs a 10ms clamp.
    monkeypatch.setattr(analyzer, 'detect_segments', lambda *a, **kw: detection(0, 9.99))
    analyzer._analyze_asset(pid, asset)
    with session_scope() as s:
        row = s.get(Segment, sid)
        assert (row.start_sec, row.end_sec, row.is_trimmed) == (2, 9.99, True)
        audit = row.quality_metrics['manual_trim_audit'][-1]
        assert audit['original_in_sec'] == 2 and audit['original_out_sec'] == 10


def test_patch_note_is_persisted_and_readable(client, seeded):
    _, _, sid, _, _, _ = seeded
    assert client.patch(f'/api/segments/{sid}', json={'note': 'from PATCH'}).status_code == 200
    assert client.get(f'/api/segments/{sid}').json()['note'] == 'from PATCH'
