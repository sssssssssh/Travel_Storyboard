"""TSB-233: interleaved edits and publication faults preserve old artifacts."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, ReviewRequest, Segment
from app.paths import project_cache_dir, rel_to_data, resolve_in_project
from app.services import story_planner
from app.services.runner import JobRunner
from test_review_validation import library, item  # fixtures shared with admission tests


@pytest.fixture()
def proxy_case(library, monkeypatch):
    pid, aid, sid, source, _ = library
    root = project_cache_dir(pid) / 'proxies'; root.mkdir()
    old = root / 'historic.mp4'; old.write_bytes(b'old-playable-preview')
    with session_scope() as s:
        rr = ReviewRequest(project_id=pid, asset_id=aid, segment_id=sid, start_sec=3, end_sec=3.6,
                           proxy_ref=rel_to_data(old))
        job = Job(project_id=pid, job_type='proxies')
        s.add_all([rr, job]); s.flush(); rid, jid = rr.id, job.id
    encoded = []
    def encode(source, start, duration, out, **kw):
        encoded.append((str(source), start, duration, out))
        out.write_bytes(b'new-preview')
    monkeypatch.setattr(story_planner, 'extract_preview', encode)
    monkeypatch.setattr(story_planner, 'probe_media', lambda _: SimpleNamespace(
        video=SimpleNamespace(width=640), duration_sec=.6))
    return library, rid, jid, old, encoded, encode


def run_case(case):
    _, _, jid, *_ = case
    JobRunner()._run(jid, story_planner.proxies_task)


def assert_preserved(case, status='needs_review', code=None):
    library, rid, jid, old, _, _ = case
    pid = library[0]
    assert old.read_bytes() == b'old-playable-preview'
    assert list((project_cache_dir(pid) / 'proxies').glob('*.mp4')) == [old]
    assert not list((project_cache_dir(pid) / 'proxies' / 'staging').rglob('*.mp4'))
    with session_scope() as s:
        rr = s.get(ReviewRequest, rid)
        assert rr.proxy_ref == rel_to_data(old) and rr.status == status
        job = s.get(Job, jid)
        if code:
            assert job.status == 'partial_failed'
            assert f'[{code}]' in job.failures['items'][0]['message']


def test_proxy_uses_exact_requested_duration_and_unique_publication(proxy_case):
    run_case(proxy_case)
    library, rid, jid, old, encoded, _ = proxy_case
    assert len(encoded) == 1 and encoded[0][1:3] == pytest.approx((3, .6))
    assert 'staging' in encoded[0][3].parts
    assert old.read_bytes() == b'old-playable-preview'
    with session_scope() as s:
        rr, job = s.get(ReviewRequest, rid), s.get(Job, jid)
        assert rr.status == 'proxied' and rr.proxy_ref != rel_to_data(old)
        assert resolve_in_project(library[0], rr.proxy_ref).read_bytes() == b'new-preview'
        assert job.stats['files_generated'] == 1 and job.stats['bytes'] == len(b'new-preview')
        assert job.stats['requested_duration_sec'] == pytest.approx(.6)


@pytest.mark.parametrize('change,code', [('trim', 'RANGE_OUTSIDE_TRIM'), ('stale', 'SEGMENT_NEEDS_REVIEW'),
                                        ('missing', 'SEGMENT_NOT_FOUND'), ('source', 'SOURCE_CHANGED')])
def test_before_encoding_rechecks_latest_state(proxy_case, change, code):
    library, _, _, _, encoded, _ = proxy_case
    pid, aid, sid, source, _ = library
    with session_scope() as s:
        if change == 'trim': s.get(Segment, sid).end_sec = 3.4
        if change == 'stale': s.get(Segment, sid).status = 'needs_review'
        if change == 'missing': s.delete(s.get(Segment, sid))
        if change == 'source': s.get(Asset, aid).file_size_bytes = 999
    run_case(proxy_case)
    assert encoded == []
    assert_preserved(proxy_case, code=code)


@pytest.mark.parametrize('change,code,status', [
    ('trim', 'RANGE_OUTSIDE_TRIM', 'needs_review'),
    ('trim_still_contains_request', 'REQUEST_CHANGED', 'needs_review'),
    ('source', 'SOURCE_CHANGED', 'needs_review'),
    ('defer', 'REQUEST_CHANGED', 'needs_review'),
    ('dismiss', 'REQUEST_CHANGED', 'dismissed'),
    ('stale', 'SEGMENT_NEEDS_REVIEW', 'needs_review'),
])
def test_during_encoding_rechecks_commit_without_writer_lock(proxy_case, monkeypatch, change, code, status):
    library, rid, _, _, _, encode = proxy_case
    _, _, sid, source, _ = library
    def interleave(*a, **kw):
        # This write can complete inside the encoding callback: encoding holds
        # no SQLite writer reservation.
        with session_scope() as s:
            if change == 'trim': s.get(Segment, sid).end_sec = 3.4
            if change == 'trim_still_contains_request': s.get(Segment, sid).end_sec = 7
            if change == 'stale': s.get(Segment, sid).status = 'needs_review'
            if change in ('defer', 'dismiss'):
                s.get(ReviewRequest, rid).status = 'needs_review' if change == 'defer' else 'dismissed'
        if change == 'source':
            replacement = source.with_suffix('.replacement'); replacement.write_bytes(b'new-video-content')
            replacement.replace(source)
        encode(*a, **kw)
    monkeypatch.setattr(story_planner, 'extract_preview', interleave)
    run_case(proxy_case)
    assert_preserved(proxy_case, status=status, code=code)


def test_cancel_during_encode_does_not_publish(proxy_case, monkeypatch):
    _, _, jid, _, _, encode = proxy_case
    def cancel(*a, **kw):
        encode(*a, **kw)
        with session_scope() as s: s.get(Job, jid).status = 'cancelled'
    monkeypatch.setattr(story_planner, 'extract_preview', cancel)
    run_case(proxy_case)
    assert_preserved(proxy_case, status='pending')
    with session_scope() as s: assert s.get(Job, jid).status == 'cancelled'


@pytest.mark.parametrize('failure', ['disk_full', 'ffmpeg', 'probe', 'database'])
def test_publish_faults_preserve_old_file_and_database(proxy_case, monkeypatch, failure):
    if failure in ('disk_full', 'ffmpeg'):
        def fail(source, start, duration, out, **kw):
            out.write_bytes(b'partial')
            raise OSError('ENOSPC: disk full' if failure == 'disk_full' else 'FFmpeg exited 1')
        monkeypatch.setattr(story_planner, 'extract_preview', fail)
    elif failure == 'probe':
        monkeypatch.setattr(story_planner, 'probe_media', lambda _: SimpleNamespace(
            video=SimpleNamespace(width=0), duration_sec=0))
    else:
        original = story_planner.session_scope
        @contextmanager
        def broken_commit():
            with original() as s:
                yield s
                # Fail after rename, before SQLite commit, exactly once.
                if any(isinstance(r, ReviewRequest) and r.status == 'proxied' for r in s.dirty):
                    raise OSError('injected database commit failure')
        monkeypatch.setattr(story_planner, 'session_scope', broken_commit)
    run_case(proxy_case)
    assert_preserved(proxy_case, status='failed', code='PROXY_FAILED')
    assert proxy_case[0][3].read_bytes() == b'unchanged-source'


def test_actual_byte_budget_leaves_oversized_request_pending(proxy_case):
    _, _, jid, *_ = proxy_case
    with session_scope() as s: s.get(Job, jid).config_snapshot = {'max_total_mb': 1 / (1024 * 1024)}
    run_case(proxy_case)
    assert_preserved(proxy_case, status='pending')
    with session_scope() as s:
        stats = s.get(Job, jid).stats
        assert stats['skipped_budget'] == 1 and stats['bytes'] == 0 and stats['files_generated'] == 0


def test_each_request_reads_after_previous_encode(proxy_case, monkeypatch):
    library, _, _, _, encoded, encode = proxy_case
    pid, aid, sid, *_ = library
    with session_scope() as s:
        second = ReviewRequest(project_id=pid, asset_id=aid, segment_id=sid, start_sec=4, end_sec=5, priority='low')
        s.add(second); s.flush(); second_id = second.id
    def withdraw(*a, **kw):
        with session_scope() as s: s.get(ReviewRequest, second_id).status = 'dismissed'
        encode(*a, **kw)
    monkeypatch.setattr(story_planner, 'extract_preview', withdraw)
    run_case(proxy_case)
    assert len(encoded) == 1
    with session_scope() as s: assert s.get(ReviewRequest, second_id).status == 'dismissed'


def test_proxy_staging_is_not_served_as_project_media(client, proxy_case, monkeypatch):
    library, _, _, _, _, encode = proxy_case
    pid = library[0]
    observed = []
    def check_staging(source, start, duration, out, **kw):
        encode(source, start, duration, out, **kw)
        observed.append(client.get(f'/api/projects/{pid}/media', params={'path': str(out)}).status_code)
    monkeypatch.setattr(story_planner, 'extract_preview', check_staging)
    run_case(proxy_case)
    assert observed == [400]


def test_explicit_replacement_during_encoding_keeps_new_request_pending(client, proxy_case, monkeypatch):
    library, rid, _, _, encoded, encode = proxy_case
    pid, aid, _, *_ = library
    with session_scope() as s:
        new = Segment(project_id=pid, asset_id=aid, start_sec=0, end_sec=6,
                      detected_start_sec=0, detected_end_sec=6)
        s.add(new); s.flush(); new_sid = new.id
    replacements = []
    def replace_request(*a, **kw):
        response = client.post(f'/api/projects/{pid}/review-requests/{rid}/disposition', json={
            'action': 'replace', 'asset_id': aid, 'segment_id': new_sid, 'start_sec': 3, 'end_sec': 3.6})
        assert response.status_code == 200
        replacements.append(response.json()['id'])
        encode(*a, **kw)
    monkeypatch.setattr(story_planner, 'extract_preview', replace_request)
    run_case(proxy_case)
    assert len(encoded) == 1
    assert_preserved(proxy_case, status='superseded', code='REQUEST_CHANGED')
    with session_scope() as s:
        new = s.get(ReviewRequest, replacements[0])
        assert new.status == 'pending' and not new.proxy_ref and new.supersedes_request_id == rid


def test_replayed_job_keeps_published_byte_budget_and_duration_counters(proxy_case):
    run_case(proxy_case)
    library, rid, jid, old, encoded, _ = proxy_case
    pid, aid, sid, *_ = library
    with session_scope() as s:
        job = s.get(Job, jid)
        assert len(job.resume_token['proxy_publications']) == 1
        job.status = 'pending'
        job.config_snapshot = {'max_total_mb': len(b'new-preview') / (1024 * 1024)}
        second = ReviewRequest(project_id=pid, asset_id=aid, segment_id=sid, start_sec=4, end_sec=5)
        s.add(second); s.flush(); second_id = second.id
    run_case(proxy_case)
    assert len(encoded) == 1  # No fresh allocation after the committed budget.
    assert old.read_bytes() == b'old-playable-preview'
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.status == 'partial_failed' and job.stats['skipped_budget'] == 1
        assert job.stats['bytes'] == len(b'new-preview') and job.stats['files_generated'] == 1
        assert job.stats['requested_duration_sec'] == pytest.approx(.6)
        assert s.get(ReviewRequest, second_id).status == 'pending'
        assert s.get(ReviewRequest, rid).status == 'proxied'
