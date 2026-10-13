"""TSB-231/232/234: current references, package selection and human lineage."""
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, EditPlanShot, Frame, ReviewRequest, Segment, UserAnnotation
from app.paths import frames_dir, project_cache_dir
from app.services.review_package import _build_package
from app.services.story_planner import import_review_requests, _persist_requests


@pytest.fixture()
def library(client, tmp_path):
    pid = client.post('/api/projects', json={'name': 'review-closure'}).json()['id']
    source = tmp_path / '中文 source.mp4'; source.write_bytes(b'unchanged-source')
    with session_scope() as s:
        asset = Asset(project_id=pid, source_path=str(source), display_name=source.name, duration_sec=10)
        s.add(asset); s.flush()
        seg = Segment(project_id=pid, asset_id=asset.id, start_sec=2, end_sec=8,
                      detected_start_sec=0, detected_end_sec=10, user_rating='favorite', story_role='高潮')
        s.add(seg); s.flush()
        jpg = frames_dir(pid, asset.id) / 'current.jpg'; jpg.parent.mkdir(parents=True)
        Image.new('RGB', (60, 40), 'red').save(jpg)
        seg.frame_refs = [str(jpg)]
        s.add(Frame(asset_id=asset.id, segment_id=seg.id, image_path=str(jpg), time_sec=4))
        s.add(UserAnnotation(segment_id=seg.id, rating='favorite', note='retain my note', in_sec=2, out_sec=8))
        return pid, asset.id, seg.id, source, jpg


def item(library, **extra):
    _, aid, sid, *_ = library
    return {'asset_id': aid, 'segment_id': sid, 'start_sec': 3, 'end_sec': 3.6, **extra}


@pytest.mark.parametrize('start,end,code', [
    ('NaN', 4, 'INVALID_TIME_RANGE'), (3, 'Infinity', 'INVALID_TIME_RANGE'),
    (-1, 4, 'INVALID_TIME_RANGE'), (4, 3, 'INVALID_TIME_RANGE'),
    (3, 3.1, 'INVALID_TIME_RANGE'), (1, 4, 'RANGE_OUTSIDE_TRIM'),
    (3, 9, 'RANGE_OUTSIDE_TRIM'), (3, 11, 'RANGE_OUTSIDE_ASSET'),
])
def test_batch_strict_shared_validation(client, library, start, end, code):
    pid, _, _, *_ = library
    result = client.post(f'/api/projects/{pid}/review-requests/import', json={
        'requests': [item(library, start_sec=start, end_sec=end), item(library)]})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['created'] == 1
    assert data['errors'] == [{'index': 0, 'request_index': 0, 'segment_id': library[2],
                               'error_code': code, 'message': data['errors'][0]['message']}]
    assert _persist_requests(pid, [item(library, start_sec=start, end_sec=end)]) == 0


def test_forged_cross_project_and_mismatched_asset(client, library):
    pid, aid, sid, *_ = library
    other = client.post('/api/projects', json={'name': 'other'}).json()['id']
    with session_scope() as s:
        asset = Asset(project_id=other, source_path='/other.mp4', duration_sec=10)
        s.add(asset); s.flush()
        seg = Segment(project_id=other, asset_id=asset.id, start_sec=0, end_sec=10)
        s.add(seg); s.flush(); foreign_aid, foreign_sid = asset.id, seg.id
        own = Asset(project_id=pid, source_path='/own.mp4', duration_sec=10)
        s.add(own); s.flush(); own_aid = own.id
    cases = [(item(library, segment_id='forged'), 'SEGMENT_NOT_FOUND'),
             (item(library, asset_id=foreign_aid, segment_id=foreign_sid), 'PROJECT_MISMATCH'),
             (item(library, asset_id=own_aid), 'SEGMENT_ASSET_MISMATCH'),
             (item(library, asset_id=[]), 'INVALID_REQUEST'), ('malformed', 'INVALID_REQUEST')]
    result = import_review_requests(pid, {'requests': [x for x, _ in cases]})
    assert result['created'] == 0
    assert [e['error_code'] for e in result['errors']] == [code for _, code in cases]


def test_repeat_import_normalizes_and_reports_duplicate(library):
    pid, _, _, *_ = library
    first = import_review_requests(pid, {'requests': [item(library)]})
    again = import_review_requests(pid, {'requests': [item(library, start_sec=3.00000001)]})
    assert first['created'] == 1 and again['created'] == 0 and not again['errors']
    assert again['duplicates'][0]['request_index'] == 0
    with session_scope() as s:
        rows = s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).all()
        assert len(rows) == 1 and rows[0].id == again['duplicates'][0]['request_id']


@pytest.mark.parametrize('rating', ['', 'favorite'])
def test_package_excludes_favorite_stale_in_all_filters(library, rating):
    pid, aid, sid, *_ = library
    with session_scope() as s:
        stale = Segment(project_id=pid, asset_id=aid, start_sec=0, end_sec=10,
                        user_rating='favorite', status='needs_review')
        s.add(stale); s.flush(); stale_id = stale.id
    result = _build_package(pid, {'rating': rating})
    root = project_cache_dir(pid) / 'packages' / result['version']
    manifest = json.loads((root / 'manifest.json').read_text())
    segments = json.loads((root / 'segments.json').read_text())
    assert [row['segment_id'] for row in segments] == [sid]
    assert manifest['selection']['total_candidates'] == 2
    assert manifest['selection']['included_segments'] == 1
    assert manifest['selection']['excluded_needs_review'] == 1
    assert stale_id not in ''.join(p.read_text() for p in root.glob('*.json'))
    assert len(list((root / 'frames').glob('*.jpg'))) == manifest['counts']['frames'] == 1
    assert len(list((root / 'contact_sheets').glob('*.jpg'))) == 1
    assert {a['asset_id'] for a in json.loads((root / 'assets.json').read_text())} == {aid}


@pytest.mark.parametrize('invalid', ['needs_review', 'missing_frames', 'source_missing'])
def test_package_all_invalid_fails_actionably(library, invalid):
    pid, _, sid, source, jpg = library
    if invalid == 'needs_review':
        with session_scope() as s: s.get(Segment, sid).status = invalid
    if invalid == 'missing_frames': jpg.unlink()
    if invalid == 'source_missing': source.unlink()
    with pytest.raises(ValueError, match='无当前有效镜头'):
        _build_package(pid, {})
    assert not list((project_cache_dir(pid) / 'packages').glob('*.zip'))


def test_package_missing_frame_statistics_do_not_enumerate_excluded_asset(library):
    pid, aid, sid, source, _ = library
    with session_scope() as s:
        other = Asset(project_id=pid, source_path=str(source.parent / 'second.mp4'), duration_sec=10)
        Path(other.source_path).write_bytes(b'second')
        s.add(other); s.flush(); other_id = other.id
        missing = Segment(project_id=pid, asset_id=other.id, start_sec=0, end_sec=10)
        excluded = Segment(project_id=pid, asset_id=aid, start_sec=0, end_sec=10, user_rating='exclude')
        s.add_all([missing, excluded])
    result = _build_package(pid, {})
    root = project_cache_dir(pid) / 'packages' / result['version']
    manifest = json.loads((root / 'manifest.json').read_text())
    assert manifest['selection']['excluded_missing_frames'] == 1
    assert manifest['selection']['excluded_by_rating'] == 1
    assert other_id not in (root / 'assets.json').read_text()


def test_human_replacement_preserves_history_annotations_plan(client, library):
    pid, aid, sid, *_ = library
    import_review_requests(pid, {'requests': [item(library)]})
    with session_scope() as s:
        old = s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).one()
        old.status, old.proxy_ref = 'needs_review', 'historic-preview.mp4'; rid = old.id
        s.get(Segment, sid).status = 'needs_review'
        newseg = Segment(project_id=pid, asset_id=aid, start_sec=0, end_sec=6,
                         detected_start_sec=0, detected_end_sec=6)
        s.add(newseg); s.flush(); new_sid = newseg.id
        shot = EditPlanShot(project_id=pid, asset_id=aid, segment_id=sid, order=0,
                            source_in_sec=3, source_out_sec=3.6, target_duration_sec=.6)
        s.add(shot); s.flush(); shot_id = shot.id
    before = client.get(f'/api/projects/{pid}/review-requests').json()[0]
    assert before['validation_error']['error_code'] == 'SEGMENT_NEEDS_REVIEW'
    assert before['plan_shot_ids'] == [shot_id]
    assert before['segment_context']['note'] == 'retain my note'
    assert [g['id'] for g in before['current_segments']] == [new_sid]
    response = client.post(f'/api/projects/{pid}/review-requests/{rid}/disposition', json={
        'action': 'replace', 'asset_id': aid, 'segment_id': new_sid, 'start_sec': 3, 'end_sec': 3.6})
    assert response.status_code == 200, response.text
    new = response.json()
    assert new['id'] != rid and new['supersedes_request_id'] == rid and new['status'] == 'pending'
    with session_scope() as s:
        old = s.get(ReviewRequest, rid)
        assert old.status == 'superseded' and old.superseded_by_request_id == new['id']
        assert old.proxy_ref == 'historic-preview.mp4' and old.segment_id == sid
        assert s.get(EditPlanShot, shot_id).segment_id == sid
        seg = s.get(Segment, sid)
        assert seg.status == 'needs_review' and seg.user_rating == 'favorite' and seg.story_role == '高潮'
        assert s.scalar(select(UserAnnotation.note).where(UserAnnotation.segment_id == sid)) == 'retain my note'
    # Confirmation of a request does not silently rewrite the user's edit plan.
    retry = client.post(f'/api/projects/{pid}/review-requests/{rid}/disposition', json={'action': 'retry'})
    assert retry.status_code == 409


def test_defer_dismiss_and_failed_replacement_are_non_destructive(client, library):
    pid, _, sid, *_ = library
    import_review_requests(pid, {'requests': [item(library)]})
    with session_scope() as s:
        old = s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).one()
        old.proxy_ref = 'old.mp4'; rid = old.id
    path = f'/api/projects/{pid}/review-requests/{rid}/disposition'
    assert client.post(path, json={'action': 'replace', **item(library, segment_id='missing')}).status_code == 409
    assert client.post(path, json={'action': 'defer'}).json()['status'] == 'needs_review'
    assert client.post(path, json={'action': 'dismiss'}).json()['status'] == 'dismissed'
    with session_scope() as s:
        assert s.get(ReviewRequest, rid).proxy_ref == 'old.mp4'
        assert s.get(Segment, sid).user_rating == 'favorite'
        assert len(s.scalars(select(ReviewRequest).where(ReviewRequest.project_id == pid)).all()) == 1


def test_package_failed_or_repeated_build_preserves_previous_package(library):
    pid, _, _, _, jpg = library
    first = _build_package(pid, {})
    zip_path = project_cache_dir(pid) / 'packages' / f"{first['version']}.zip"
    original = zip_path.read_bytes()
    second = _build_package(pid, {})
    assert second['version'] != first['version'] and zip_path.read_bytes() == original
    jpg.unlink()
    with pytest.raises(ValueError, match='无当前有效镜头'):
        _build_package(pid, {})
    assert zip_path.read_bytes() == original


def test_analyzed_source_signature_survives_rescan_and_rejects_changed_source(library):
    from app.services.review_validation import verified_source, ReviewValidationError
    from app.services.checkpoints import source_info
    pid, aid, _, source, _ = library
    before = source_info(source)
    source.write_bytes(b'a replacement source video')
    after = source_info(source)
    with session_scope() as s:
        a = s.get(Asset, aid)
        a.analyzed_source_signature = before['quick_signature']
        a.analyzed_fingerprint = 'full-hash-mode-digest'
        # A new scan does not bless existing detection of a different source.
        a.file_size_bytes, a.mtime_ns, a.quick_fingerprint = after['size'], after['mtime_ns'], after['quick_fingerprint']
    with session_scope() as s:
        with pytest.raises(ReviewValidationError, match='扫描/检测记录'):
            verified_source(s.get(Asset, aid))
    with pytest.raises(ValueError, match='无当前有效镜头'):
        _build_package(pid, {})


def test_full_analysis_signature_is_not_mistaken_for_quick_fingerprint(library):
    from app.services.review_validation import verified_source
    from app.services.checkpoints import source_info
    _, aid, _, source, _ = library
    info = source_info(source)
    with session_scope() as s:
        a = s.get(Asset, aid)
        a.analyzed_source_signature = info['quick_signature']
        a.analyzed_fingerprint = 'full-content-digest-is-different'
        a.quick_fingerprint = info['quick_fingerprint']
        assert verified_source(a) == info


def test_package_concurrent_invalidation_rejects_before_commit(library, monkeypatch):
    from app.services import review_package
    pid, _, sid, *_ = library
    original = review_package._sanitized_copy
    def invalidate(*a, **kw):
        original(*a, **kw)
        with session_scope() as s: s.get(Segment, sid).status = 'needs_review'
    monkeypatch.setattr(review_package, '_sanitized_copy', invalidate)
    with pytest.raises(ValueError, match='需要复核'):
        _build_package(pid, {})
    assert not list((project_cache_dir(pid) / 'packages').glob('*.zip'))


def test_package_frames_are_limited_to_current_trim(library):
    pid, aid, sid, _, jpg = library
    with session_scope() as s:
        s.add(Frame(asset_id=aid, segment_id=sid, image_path=str(jpg), time_sec=1))
    result = _build_package(pid, {})
    root = project_cache_dir(pid) / 'packages' / result['version']
    frames = json.loads((root / 'segments.json').read_text())[0]['frames']
    assert [f['time_sec'] for f in frames] == [4]



def test_stale_request_plan_is_preserved_and_roughcut_rejects_it(library, monkeypatch):
    from app.models import Job
    from app.services import roughcut
    from app.services.runner import JobRunner
    pid, aid, sid, *_ = library
    root = project_cache_dir(pid) / 'roughcut'; root.mkdir()
    old = root / 'roughcut.mp4'; old.write_bytes(b'historic-roughcut')
    with session_scope() as s:
        s.get(Segment, sid).status = 'needs_review'
        shot = EditPlanShot(project_id=pid, asset_id=aid, segment_id=sid, order=0,
                            source_in_sec=3, source_out_sec=3.6, target_duration_sec=.6)
        job = Job(project_id=pid, job_type='roughcut')
        s.add_all([shot, job]); s.flush(); shot_id, jid = shot.id, job.id
    encoded = []
    monkeypatch.setattr(roughcut, '_extract_normalized', lambda *a, **kw: encoded.append(True))
    JobRunner()._run(jid, roughcut.roughcut_task)
    assert encoded == [] and old.read_bytes() == b'historic-roughcut'
    with session_scope() as s:
        assert s.get(EditPlanShot, shot_id).segment_id == sid
        assert s.get(Job, jid).status in ('partial_failed', 'failed')
        assert s.get(Job, jid).failures['count'] == 1


def test_nonfinite_json_and_nonobject_rows_do_not_abort_valid_batch(client, library):
    pid, aid, sid, *_ = library
    payload = ('{"requests": ['
               f'{{"asset_id":"{aid}","segment_id":"{sid}","start_sec":NaN,"end_sec":4}},'
               f'{{"asset_id":"{aid}","segment_id":"{sid}","start_sec":3,"end_sec":Infinity}},'
               '[], false,' + json.dumps(item(library)) + ']}')
    response = client.post(f'/api/projects/{pid}/review-requests/import', content=payload,
                           headers={'Content-Type': 'application/json'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['created'] == 1
    assert [e['error_code'] for e in result['errors']] == [
        'INVALID_TIME_RANGE', 'INVALID_TIME_RANGE', 'INVALID_REQUEST', 'INVALID_REQUEST']


def test_job_progress_distinguishes_source_changes_and_review_counts(library):
    from app.models import Job
    from app.services.checkpoints import persist
    pid = library[0]
    with session_scope() as s:
        job = Job(project_id=pid, job_type='detect'); s.add(job); s.flush(); jid = job.id
    persist(jid, [{'key': 'changed', 'status': 'failed', 'error_code': 'source_changed'},
                  {'key': 'retained', 'status': 'completed', 'needs_review': 2}],
            {'phase': 'processing', 'source_changed_items': ['changed']})
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.stats['source_changed'] == 1 and job.stats['needs_review'] == 2
        assert job.stats['phase'] == 'complete' and job.stats['failed'] == 1
        job.status = 'partial_failed'
