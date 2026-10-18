"""TSB-358 S01-S07: status alone is not stored evidence; reads never mutate it."""
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.database import session_scope
from app.models import Asset, Job, Project, Segment, SegmentMotionReview, UserAnnotation, utcnow

@pytest.fixture()
def contract(client, tmp_path):
    (pid, jid, child, aid, sid, other) = [str(uuid.uuid4()) for _ in range(6)]
    start = utcnow() - timedelta(seconds=5)
    end = utcnow() + timedelta(seconds=1)
    with session_scope() as s:
        s.add(Project(id=pid, name='result-contract', source_roots=[], output_dir=str(tmp_path / 'workspace')))
        s.flush()
        s.add(Asset(id=aid, project_id=pid, source_path=str(tmp_path / 'fixture-only'), display_name='synthetic.mp4', duration_sec=3))
        s.flush()
        for key in [sid, other]:
            s.add(Segment(id=key, project_id=pid, asset_id=aid, start_sec=0.2, end_sec=1.8, detected_start_sec=0, detected_end_sec=2, user_rating='favorite', story_role='开场', status='ready'))
        s.flush()
        s.add(UserAnnotation(segment_id=sid, note='human original note', rating='favorite', in_sec=0.2, out_sec=1.8))
        s.add(Job(id=child, project_id=pid, job_type='motion_review', status='succeeded', started_at=start, finished_at=end, config_snapshot={'segment_ids': [sid], 'limit': 1}, stats={'analyzed': 1}, failures={'count': 0, 'items': []}))
        s.flush()
        s.add(Job(id=jid, project_id=pid, job_type='video_analyze', status='succeeded', stats={'fallback_job_id': child, 'fallback_job_type': 'motion_review', 'fallback_state': 'scheduled'}, config_snapshot={}))

    def row(**kw):
        value = {'project_id': pid, 'asset_id': aid, 'segment_id': sid, 'method': 'multiframe', 'status': 'succeeded', 'start_sec': 0.2, 'end_sec': 1.8, 'motion_type': 'pan', 'action_summary': 'actual stored synthetic evidence', 'is_current': True, 'evidence_json': {'method': 'multiframe', 'frames': [{'time_sec': 0.6}, {'time_sec': 1.2}]}, 'created_at': utcnow(), 'human_note': 'preserve', 'human_verdict': 'unclear'}
        value.update(kw)
        with session_scope() as s:
            s.add(SegmentMotionReview(**value))

    def change(**kw):
        with session_scope() as s:
            obj = s.get(Job, child)
            for (k, v) in kw.items():
                setattr(obj, k, v)

    def read():
        detail = client.get(f'/api/jobs/{jid}')
        assert detail.status_code == 200, detail.text
        listed = client.get(f'/api/projects/{pid}/jobs')
        assert listed.status_code == 200
        item = next((x for x in listed.json() if x['id'] == jid))
        assert item['fallback'] == detail.json()['fallback']
        return detail.json()['fallback']
    return {'pid': pid, 'jid': jid, 'child': child, 'sid': sid, 'other': other, 'start': start, 'row': row, 'change': change, 'read': read}

@pytest.mark.parametrize('status', ['pending', 'running', 'cancelling'])
def test_s01_active_successor_is_not_analysed(contract, status):
    c = contract
    c['change'](status=status)
    assert c['read']()['analysed'] is False

def test_s02_completed_successor_requires_its_selected_real_reading(contract):
    c = contract
    c['row']()
    f = c['read']()
    assert f['analysed'] and f['result_count'] == 1

@pytest.mark.parametrize('kind', ['absent', 'zero-stats', 'wrong-shot', 'historical', 'empty', 'wrong-method', 'wrong-range', 'superseded', 'unstarted', 'missing-frames', 'empty-motion', 'bad-frames', 'out-of-window', 'bad-counter'])
def test_s03_success_without_usable_evidence_is_not_analysed(contract, kind):
    c = contract
    if kind == 'zero-stats':
        c['row']()
        c['change'](stats={'analyzed': 0})
    elif kind == 'wrong-shot':
        c['row'](segment_id=c['other'])
    elif kind == 'historical':
        c['row'](created_at=c['start'] - timedelta(seconds=1))
    elif kind == 'empty':
        c['row'](action_summary='')
    elif kind == 'wrong-method':
        c['row'](method='video_native')
    elif kind == 'wrong-range':
        c['row'](start_sec=0)
    elif kind == 'superseded':
        c['row'](is_current=False)
    elif kind == 'bad-frames':
        c['row'](evidence_json={'frames': {'bad': 1, 'shape': 2}})
    elif kind == 'out-of-window':
        c['row'](evidence_json={'frames': [{'time_sec': 99}, {'time_sec': 100}]})
    elif kind == 'bad-counter':
        c['row']()
        c['change'](stats={'analyzed': 'unknown'})
    elif kind == 'missing-frames':
        c['row'](evidence_json={})
    elif kind == 'empty-motion':
        c['row'](motion_type='')
    elif kind == 'unstarted':
        c['row']()
        c['change'](started_at=None)
    f = c['read']()
    assert not f['analysed']
    assert f['result_count'] == 0
    assert f['error_code'] == 'FALLBACK_NO_RESULTS'

def test_s04_partial_failure_counts_only_existing_valid_subset(contract):
    c = contract
    c['row']()
    c['change'](status='partial_failed', stats={'analyzed': 1, 'total': 2}, failures={'count': 1, 'items': [{'error_code': 'MODEL_BAD_REQUEST'}]})
    f = c['read']()
    assert not f['analysed']
    assert f['result_count'] == 1
    assert f['error_code'] == 'MODEL_BAD_REQUEST'

@pytest.mark.parametrize('status', ['failed', 'cancelled', 'missing'])
def test_s05_terminal_fault_is_not_success_and_diagnostics_are_private(contract, status):
    c = contract
    c['row']()
    if status == 'missing':
        with session_scope() as s:
            s.delete(s.get(Job, c['child']))
    else:
        c['change'](status=status, error='private filesystem URL Key payload', failures={'items': [{'error_code': 'bad secret not a code'}]})
    f = c['read']()
    assert not f['analysed']
    assert f['error_code']
    assert 'private' not in str(f) and 'secret' not in str(f)

@pytest.mark.parametrize('state', ['legacy', 'failed', 'foreign'])
def test_s06_old_missing_or_foreign_successor_is_compatible_and_closed(contract, state):
    c = contract
    with session_scope() as s:
        if state == 'foreign':
            s.add(Project(id='different-project', name='foreign', source_roots=[]))
            s.flush()
            s.get(Job, c['child']).project_id = 'different-project'
        else:
            s.get(Job, c['jid']).stats = {} if state == 'legacy' else {'fallback_state': 'failed', 'fallback_schedule_error': 'private internal detail'}
    f = c['read']()
    assert not f['analysed']
    assert f.get('result_count', 0) == 0

def test_s07_detail_and_list_resolution_preserve_all_human_and_trim_fields(contract):
    c = contract
    c['row']()
    with session_scope() as s:
        shot = s.get(Segment, c['sid'])
        before = (shot.start_sec, shot.end_sec, shot.detected_start_sec, shot.detected_end_sec, shot.user_rating, shot.story_role)
        motion = s.scalar(select(SegmentMotionReview).where(SegmentMotionReview.segment_id == c['sid']))
        before_motion = (motion.human_note, motion.human_verdict, motion.input_signature)
    c['read']()
    c['read']()
    with session_scope() as s:
        shot = s.get(Segment, c['sid'])
        assert before == (shot.start_sec, shot.end_sec, shot.detected_start_sec, shot.detected_end_sec, shot.user_rating, shot.story_role)
        motion = s.scalar(select(SegmentMotionReview).where(SegmentMotionReview.segment_id == c['sid']))
        assert before_motion == (motion.human_note, motion.human_verdict, motion.input_signature)
        assert s.scalar(select(UserAnnotation).where(UserAnnotation.segment_id == c['sid'])).note == 'human original note'
