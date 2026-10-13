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


def test_disjoint_latest_trim_is_retained_for_review_with_audit():
    result = Segment(detected_start_sec=0, detected_end_sec=5, quality_metrics={})
    row = Segment(start_sec=8, end_sec=10, is_trimmed=True, user_rating='favorite',
                  story_role='高潮', status='confirmed', quality_metrics={})
    assert analyzer._apply_algorithm_result(row, result)
    assert (row.start_sec, row.end_sec, row.is_trimmed) == (8, 10, True)
    assert row.user_rating == 'favorite' and row.story_role == '高潮'
    assert row.status == 'needs_review'
    assert row.quality_metrics['manual_trim_audit'][-1]['action'] == 'preserved_for_review'


@pytest.mark.parametrize('fault', ['extract', 'publish', 'commit', 'cancel'])
def test_failed_background_preserves_concurrently_saved_annotation(client, seeded, monkeypatch, fault):
    from sqlalchemy import event
    from app.models import Job
    from app.services.runner import JobCancelled
    pid, asset, sid, fid, path, _ = seeded
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect', status='running')
        s.add(job); s.flush(); jid = job.id
    entered, release = Event(), Event()
    original = analyzer.extract_frame
    def paused(*args, **kwargs):
        entered.set(); assert release.wait(5)
        if fault == 'extract': raise RuntimeError('injected extract error')
        original(*args, **kwargs)
    monkeypatch.setattr(analyzer, 'extract_frame', paused)
    def fail(*a, **kw): raise RuntimeError('injected commit error')
    if fault == 'commit': event.listen(Frame, 'before_insert', fail)
    if fault == 'publish': monkeypatch.setattr(Path, 'replace', lambda *a, **kw: fail())
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(analyzer._analyze_asset, pid, asset, None, jid)
            assert entered.wait(5)
            try:
                assert client.post(f'/api/segments/{sid}/annotate', json={
                    'rating': 'favorite', 'note': 'saved during analysis', 'in_sec': 2, 'out_sec': 8, 'story_role': '开场'}).status_code == 200
                if fault == 'cancel': assert client.post(f'/api/jobs/{jid}/cancel').status_code == 200
            finally:
                release.set()
            with pytest.raises(JobCancelled if fault == 'cancel' else RuntimeError): future.result(5)
    finally:
        if fault == 'commit': event.remove(Frame, 'before_insert', fail)
        with session_scope() as s: s.get(Job, jid).status = 'cancelled' if fault == 'cancel' else 'failed'
    with session_scope() as s:
        row = s.get(Segment, sid)
        assert (row.start_sec, row.end_sec, row.is_trimmed) == (2, 8, True)
        assert row.user_rating == 'favorite' and row.story_role == '开场'
        assert s.scalars(select(UserAnnotation).where(UserAnnotation.segment_id == sid)).one().note == 'saved during analysis'
        assert s.get(Frame, fid) is not None and path.is_file()


def test_stale_plan_reference_requires_review_before_new_outputs(client, seeded, monkeypatch):
    from app.services.timeline_export import build_edit_plan
    from app.services.story_planner import _select_representative_frames
    pid, asset, sid, _, _, detection = seeded
    shot = {'asset_id': asset.id, 'segment_id': sid, 'source_in_sec': 1, 'source_out_sec': 4}
    assert build_edit_plan(pid, [shot])['errors'] == []
    monkeypatch.setattr(analyzer, 'detect_segments', lambda *a, **kw: detection(0, 6))
    analyzer._analyze_asset(pid, asset)
    assert build_edit_plan(pid, [shot])['errors']
    sampled, _ = _select_representative_frames(pid, 12, 24)
    assert sid not in {p['segment_id'] for p in sampled}
    with session_scope() as s:
        assert s.scalars(select(EditPlanShot).where(EditPlanShot.project_id == pid)).one().segment_id == sid


def test_plan_created_during_extraction_protects_old_id_at_commit(client, seeded, monkeypatch):
    pid, asset, sid, fid, old, detection = seeded
    with session_scope() as s: s.get(Segment, sid).status = 'candidate'
    entered, release = Event(), Event()
    original = analyzer.extract_frame
    def paused(*a, **kw):
        entered.set(); assert release.wait(5); original(*a, **kw)
    monkeypatch.setattr(analyzer, 'extract_frame', paused)
    monkeypatch.setattr(analyzer, 'detect_segments', lambda *a, **kw: detection(0, 6))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(analyzer._analyze_asset, pid, asset)
        assert entered.wait(5)
        try:
            result = client.post(f'/api/projects/{pid}/edit-plan', json={'shots': [{
                'asset_id': asset.id, 'segment_id': sid, 'source_in_sec': 1, 'source_out_sec': 5, 'target_duration_sec': 4}]})
            assert result.status_code == 200 and not result.json()['errors']
        finally:
            release.set()
        future.result(5)
    with session_scope() as s:
        assert s.get(Segment, sid).status == 'needs_review'
        assert s.get(Frame, fid) is not None and old.is_file()
        assert s.scalars(select(EditPlanShot).where(EditPlanShot.project_id == pid)).one().segment_id == sid


def test_plan_validation_reserves_sqlite_writer_before_reading_references(client, seeded, monkeypatch):
    import sqlite3
    from app.database import engine
    from app.services import timeline_export
    pid, asset, sid, _, _, _ = seeded
    validate = timeline_export._validate_shot
    def probe_lock(*args, **kwargs):
        connection = sqlite3.connect(engine.url.database, timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                connection.execute('BEGIN IMMEDIATE')
        finally:
            connection.rollback(); connection.close()
        return validate(*args, **kwargs)
    monkeypatch.setattr(timeline_export, '_validate_shot', probe_lock)
    result = timeline_export.build_edit_plan(pid, [{'asset_id': asset.id, 'segment_id': sid,
                                                    'source_in_sec': 1, 'source_out_sec': 5}])
    assert result['errors'] == []
