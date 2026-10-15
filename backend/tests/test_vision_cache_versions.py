"""TSB-315: actual per-shot bytes and current-version cache semantics."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from PIL import Image
from sqlalchemy import select, text

from app.config import get_settings
from app.database import session_scope
from app.models import Asset, Frame, Segment, SegmentSemanticResult
from app.paths import resolve_in_project
from app.services.vision import semantic
from test_vision_semantics import project, stubbed, _seed, _make_job, _job_row  # noqa: F401


def seed(project, tmp_path, count=2):
    data = _seed(project['id'], [('a.mp4', count, 'pending')])
    source = tmp_path / 'a.mp4'; source.write_bytes(b'bounded-source-snapshot')
    with session_scope() as s:
        s.get(Asset, data['assets']['a.mp4']).source_path = str(source)
    return data


def run(pid, *, _runner=True, **options):
    jid = _make_job(pid, **options)
    if _runner:
        from app.services.runner import JobRunner
        JobRunner()._run(jid, semantic.vision_analyze_task)
    else:
        semantic.vision_analyze_task(jid)
    return _job_row(jid)


def test_adding_c_only_calls_c(project, stubbed, tmp_path):
    data = seed(project, tmp_path)
    run(project['id'])
    extra = _seed(project['id'], [('c.mp4', 1, 'pending')])
    path = tmp_path / 'c.mp4'; path.write_bytes(b'new-source')
    with session_scope() as s:
        s.get(Asset, extra['assets']['c.mp4']).source_path = str(path)
    row = run(project['id'])
    assert row.stats['skipped'] == 2
    assert row.stats['analyzed'] == 1
    assert len(stubbed.calls) == 3


def test_only_trimmed_a_invalidates(project, stubbed, tmp_path):
    data = seed(project, tmp_path)
    run(project['id'])
    with session_scope() as s:
        s.get(Segment, data['segments'][0]['segment_id']).start_sec += 0.0001
    row = run(project['id'])
    assert (row.stats['analyzed'], row.stats['skipped']) == (1, 1)
    assert len(stubbed.calls) == 3


def test_prompt_round_trip_activates_requested_version(project, stubbed, tmp_path, monkeypatch, client):
    data = seed(project, tmp_path, 1)
    sid = data['segments'][0]['segment_id']
    for prompt in ('v1', 'v2', 'v1'):
        monkeypatch.setattr(get_settings(), 'vision_prompt_version', prompt)
        row = run(project['id'])
        assert row.status == 'succeeded'
        out = client.get(f'/api/segments/{sid}/vision-result').json()
        assert out['prompt_version'] == prompt
    assert len(stubbed.calls) == 3  # design A: historical versions are re-analyzed


@pytest.mark.parametrize('change', ['replace', 'delete'])
def test_actual_frame_change_cannot_hit_cache(project, stubbed, tmp_path, change):
    data = seed(project, tmp_path, 1)
    run(project['id'])
    with session_scope() as s:
        frame = s.scalars(select(Frame).where(Frame.segment_id == data['segments'][0]['segment_id'])).one()
    path = resolve_in_project(project['id'], frame.image_path)
    if change == 'replace':
        Image.new('RGB', (320, 180), 'white').save(path)
    else:
        path.unlink()
    row = run(project['id'])
    assert row.stats['skipped'] == 0
    assert row.stats['analyzed' if change == 'replace' else 'failed'] == 1


def test_force_retains_request_audit_and_actual_wire_fingerprint(project, stubbed, tmp_path):
    data = seed(project, tmp_path, 1)
    one = run(project['id']); two = run(project['id'], force=True)
    assert len(stubbed.calls) == 2
    assert two.stats['calls'] == 1
    with session_scope() as s:
        attempts = s.execute(text('SELECT * FROM segment_semantic_attempts WHERE project_id=:pid'),
                             {'pid': project['id']}).mappings().all()
    assert {r['job_id'] for r in attempts} == {one.id, two.id}
    for r in attempts:
        assert r['frame_manifest_hash']
        assert json.loads(r['evidence_json'])['request_id'] == 'req-1'


def test_parallel_identical_publish_has_one_current_row(project, stubbed, tmp_path, monkeypatch):
    seed(project, tmp_path, 1)
    original = semantic._store_result
    gate = Barrier(2)
    def synchronized(*args, **kwargs):
        gate.wait(timeout=10)
        return original(*args, **kwargs)
    monkeypatch.setattr(semantic, '_store_result', synchronized)
    with ThreadPoolExecutor(2) as pool:
        rows = [pool.submit(run, project['id'], force=True, _runner=False) for _ in range(2)]
        # Deliberately bypass project serialization to stress the publication writer.
        # Direct workers stay running until Runner finalizes; both published once.
        results = [f.result(timeout=20) for f in rows]
        assert all(row.status == 'running' and row.stats['analyzed'] == 1 for row in results)
    with session_scope() as s:
        rows = s.scalars(select(SegmentSemanticResult).where(SegmentSemanticResult.project_id == project['id'])).all()
        assert sum(r.is_current for r in rows) == 1
        # Two intentional provider calls have two audited publications, one version.
        assert s.execute(text('SELECT count(*) FROM segment_semantic_attempts WHERE project_id=:pid'),
                         {'pid': project['id']}).scalar_one() == 2


def test_missing_source_is_not_a_valid_cached_input(project, stubbed, tmp_path):
    seed(project, tmp_path, 1)
    run(project['id'])
    (tmp_path / 'a.mp4').unlink()
    row = run(project['id'])
    assert row.stats['skipped'] == 0
    assert row.stats['failed'] == 1
    assert row.failures['items'][0]['error_code'] == 'SOURCE_CHANGED'


def test_legacy_unknown_signature_is_retained_but_not_treated_as_valid(project, stubbed, tmp_path):
    data = seed(project, tmp_path, 1)
    sid = data['segments'][0]['segment_id']
    with session_scope() as s:
        old = SegmentSemanticResult(project_id=project['id'], asset_id=data['assets']['a.mp4'],
            segment_id=sid, model_id='stub-model', prompt_version=get_settings().vision_prompt_version,
            source_signature='legacy-batch-signature', frame_manifest_hash='legacy-count-only', is_current=True)
        s.add(old); s.flush(); old_id = old.id
    row = run(project['id'])
    assert row.stats['analyzed'] == 1 and row.stats['skipped'] == 0
    with session_scope() as s:
        assert s.get(SegmentSemanticResult, old_id).is_current is False
        assert len(s.scalars(select(SegmentSemanticResult).where(SegmentSemanticResult.segment_id == sid)).all()) == 2


def test_coverage_counts_failures_and_cache_separately(project, stubbed, tmp_path, monkeypatch):
    seed(project, tmp_path, 2)
    run(project['id'])
    with session_scope() as s:
        rows = s.scalars(select(Frame).join(Segment).where(Segment.project_id == project['id'])).all()
    resolve_in_project(project['id'], rows[0].image_path).unlink()
    row = run(project['id'])
    cov = row.stats['coverage']
    assert cov['planned'] == 2
    assert cov['analyzed'] == 0
    assert cov['cached_valid'] == 1
    assert cov['not_covered'] == 1
    assert row.stats['failed'] == 1
