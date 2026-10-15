"""TSB-319: real FastAPI dispatch must retain the UI's explicit consent/options."""
import pytest

from app.database import session_scope
from app.models import Job
from app.services.job_service import runner


@pytest.mark.parametrize('kind', ['vision_analyze', 'motion_review'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_real_route_persists_all_visual_options(client, monkeypatch, kind, wrapped):
    monkeypatch.setattr(runner, 'submit', lambda jid: None)
    pid = client.post('/api/projects', json={'name': 'route-wiring'}).json()['id']
    options = {'segment_ids': ['selected-shot'], 'limit': 2, 'upload_acknowledged': True}
    if kind == 'motion_review':
        options['frame_count'] = 3
    else:
        options['force'] = True
    body = {'options': {k: v for k, v in options.items() if k != 'force'}, 'force': True} if wrapped else options
    response = client.post(f'/api/projects/{pid}/jobs/{kind}', json=body)
    assert response.status_code == 202
    with session_scope() as s:
        job = s.get(Job, response.json()['id'])
        assert job.config_snapshot['upload_acknowledged'] is True
        assert job.config_snapshot['segment_ids'] == ['selected-shot']
        assert job.config_snapshot['limit'] == 2
        assert job.config_snapshot['frame_count' if kind == 'motion_review' else 'force'] == (3 if kind == 'motion_review' else True)


@pytest.mark.parametrize('kind', ['vision_analyze', 'motion_review'])
def test_visual_options_use_dedicated_validation(client, monkeypatch, kind):
    submitted = []
    monkeypatch.setattr(runner, 'submit', submitted.append)
    pid = client.post('/api/projects', json={'name': 'route-validation'}).json()['id']
    response = client.post(f'/api/projects/{pid}/jobs/{kind}', json={'limit': 0, 'upload_acknowledged': True})
    assert response.status_code == 422 and submitted == []


@pytest.mark.parametrize('kind', ['vision_analyze', 'motion_review'])
def test_ambiguous_consent_is_rejected_before_enqueue(client, monkeypatch, kind):
    submitted = []
    monkeypatch.setattr(runner, 'submit', submitted.append)
    pid = client.post('/api/projects', json={'name': 'ambiguous-consent'}).json()['id']
    response = client.post(f'/api/projects/{pid}/jobs/{kind}', json={
        'upload_acknowledged': False, 'options': {'upload_acknowledged': True}})
    assert response.status_code == 422 and submitted == []
