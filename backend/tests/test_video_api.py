"""TSB-328/329 — the video observation API and its wiring.

These tests go through the real HTTP surface with the real router, because that is
where a wiring gap would hide: a service that works while the route is unreachable
(or reachable without the token) is exactly the TSB-253 class of defect. The
response shape is asserted field by field, since the honesty flags must be present
even when false -- a consumer that has to infer "audio not analysed" from a missing
key will eventually infer it wrong.

Task book row V23 (real UI/job wiring) starts here and continues in TSB-331.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, Project, Segment, SegmentVideoObservation


@pytest.fixture()
def api_env(client, tmp_path):
    """A project with one eligible shot and a stored observation."""
    res = client.post("/api/projects", json={"name": "video-api", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    source = tmp_path / "src.mp4"
    source.write_bytes(b"api-source-bytes")
    asset_id, segment_id, job_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path=str(source),
                    display_name="src.mp4", scan_status="succeeded",
                    file_size_bytes=source.stat().st_size,
                    mtime_ns=source.stat().st_mtime_ns))
        s.flush()
        s.add(Segment(id=segment_id, project_id=project_id, asset_id=asset_id,
                      detected_start_sec=10.0, detected_end_sec=15.0,
                      start_sec=10.0, end_sec=15.0, boundary_reason="hard_cut",
                      user_rating="pending"))
        s.add(Job(id=job_id, project_id=project_id, job_type="video_analyze",
                  status="running", config_snapshot={}, stats={},
                  failures={"count": 0, "items": []}))
    return {"project_id": project_id, "asset_id": asset_id,
            "segment_id": segment_id, "job_id": job_id}


def _store_row(env, **overrides) -> str:
    """Write one observation directly, so the read paths can be tested."""
    from app.models import utcnow

    row_id = str(uuid.uuid4())
    payload = {
        "id": row_id,
        "project_id": env["project_id"],
        "asset_id": env["asset_id"],
        "segment_id": env["segment_id"],
        "status": "succeeded",
        "method": "video_native",
        "model_id": "test-model",
        "prompt_version": "1.0",
        "schema_version": "1.0",
        "protocol": "video_url_data_uri",
        "clip_start_sec": 10.0,
        "clip_end_sec": 15.0,
        "clip_duration_sec": 5.0,
        "clip_size_bytes": 4096,
        "clip_sha256": "abc123",
        "segment_start_sec": 10.0,
        "segment_end_sec": 15.0,
        "version_key": uuid.uuid4().hex,
        "analysis_json": {"summary": "something moves", "limitations": ["clip is silent"],
                          "dropped": []},
        "events_json": [
            {"event_type": "subject_action", "start_rel_sec": 1.0, "end_rel_sec": 2.0,
             "start_abs_sec": 11.0, "end_abs_sec": 12.0,
             "description": "an object moves", "confidence": 0.5,
             "confidence_is_calibrated": False, "evidence": ["video_window"],
             "limitations": []}
        ],
        "request_usage": {"total_tokens": 100},
        "audio_analyzed": False,
        "color_unverified": False,
        "observed_temporal_quality": "limited",
        "is_current": True,
    }
    payload.update(overrides)
    with session_scope() as s:
        s.add(SegmentVideoObservation(**payload))
    return row_id


# --- read paths -------------------------------------------------------------

def test_the_observation_list_is_empty_before_any_analysis(client, api_env):
    res = client.get(f"/api/projects/{api_env['project_id']}/video-observations")
    assert res.status_code == 200
    assert res.json() == []


def test_a_stored_observation_is_returned_with_its_events(client, api_env):
    _store_row(api_env)
    res = client.get(f"/api/projects/{api_env['project_id']}/video-observations")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["method"] == "video_native"
    assert row["summary"] == "something moves"
    assert len(row["events"]) == 1
    event = row["events"][0]
    assert event["start_abs_sec"] == pytest.approx(11.0)
    assert event["confidence_is_calibrated"] is False


def test_the_response_always_carries_the_honesty_flags(client, api_env):
    """A consumer must never infer these from a missing key."""
    _store_row(api_env)
    row = client.get(
        f"/api/projects/{api_env['project_id']}/video-observations"
    ).json()[0]
    for key in ("audio_analyzed", "color_unverified", "observed_temporal_quality",
                "method", "protocol", "schema_version"):
        assert key in row, f"{key} missing from the response"


def test_the_clip_and_segment_windows_are_exposed(client, api_env):
    """The UI needs both to place an event on a timeline."""
    _store_row(api_env)
    row = client.get(
        f"/api/projects/{api_env['project_id']}/video-observations"
    ).json()[0]
    assert row["clip"]["start_sec"] == pytest.approx(10.0)
    assert row["clip"]["duration_sec"] == pytest.approx(5.0)
    assert row["segment_window"]["end_sec"] == pytest.approx(15.0)


def test_the_single_segment_endpoint_returns_the_current_reading(client, api_env):
    _store_row(api_env)
    res = client.get(f"/api/segments/{api_env['segment_id']}/video-observation")
    assert res.status_code == 200
    assert res.json()["is_current"] is True


def test_the_single_segment_endpoint_is_404_without_a_reading(client, api_env):
    res = client.get(f"/api/segments/{api_env['segment_id']}/video-observation")
    assert res.status_code == 404


def test_history_returns_superseded_versions(client, api_env):
    """V15: an older version stays retrievable for audit."""
    _store_row(api_env, is_current=False, clip_sha256="old",
               analysis_json={"summary": "older reading", "limitations": [], "dropped": []})
    _store_row(api_env, is_current=True, clip_sha256="new")

    history = client.get(
        f"/api/segments/{api_env['segment_id']}/video-observation/history"
    ).json()
    assert len(history) == 2

    current = client.get(
        f"/api/projects/{api_env['project_id']}/video-observations"
    ).json()
    assert len(current) == 1, "the default list shows only the live reading"
    assert current[0]["clip"]["sha256"] == "new"


def test_include_history_exposes_every_version(client, api_env):
    _store_row(api_env, is_current=False)
    _store_row(api_env, is_current=True)
    rows = client.get(
        f"/api/projects/{api_env['project_id']}/video-observations",
        params={"include_history": True},
    ).json()
    assert len(rows) == 2


def test_observations_are_scoped_to_their_project(client, api_env):
    """A reading must not leak across projects."""
    _store_row(api_env)
    other = client.post("/api/projects", json={"name": "other", "source_roots": []}).json()
    rows = client.get(f"/api/projects/{other['id']}/video-observations").json()
    assert rows == []


# --- coverage ---------------------------------------------------------------

def test_coverage_reports_the_subset_not_a_total_claim(client, api_env):
    res = client.get(f"/api/projects/{api_env['project_id']}/video-coverage")
    assert res.status_code == 200
    body = res.json()
    assert body["library_segments"] >= 1
    assert body["segments_with_video_reading"] == 0
    assert body["segments_not_covered"] >= 1
    assert body["complete"] is False
    assert body["audio_analyzed"] is False


def test_coverage_counts_a_stored_reading(client, api_env):
    _store_row(api_env)
    body = client.get(f"/api/projects/{api_env['project_id']}/video-coverage").json()
    assert body["segments_with_video_reading"] == 1
    assert body["event_candidates"] == 1


def test_a_failed_attempt_is_not_counted_as_coverage(client, api_env):
    _store_row(api_env, status="failed", is_current=False, events_json=[],
               error_code="MODEL_TIMEOUT")
    body = client.get(f"/api/projects/{api_env['project_id']}/video-coverage").json()
    assert body["segments_with_video_reading"] == 0
    assert body["failed_attempts"] == 1


# --- the job endpoint -------------------------------------------------------

def test_starting_a_video_job_without_a_verified_channel_is_refused(client, api_env):
    """The route is reachable and typed; the channel is what is unavailable.

    Asserting 422 rather than 202 is the point: this build has no verified video
    capability on record, so a ``video_native`` request must be answered here
    instead of producing a job that fails inside the worker. The fixture's own job
    is released first, since a running job legitimately answers 409 instead.
    """
    with session_scope() as s:
        s.get(Job, api_env["job_id"]).status = "succeeded"

    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True, "limit": 5},
    )
    assert res.status_code == 422, res.text
    assert "capability_state" in res.json()["detail"]


def test_a_running_job_is_reported_as_a_conflict_before_any_capability_verdict(
    client, api_env
):
    """Both facts can be true; the actionable one wins.

    If the capability refusal were evaluated first it would answer 422 and hide
    that the project is busy, so a caller would retry against a lock it never
    learned about.
    """
    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True},
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error_code"] == "JOB_CONFLICT"


def test_an_unverified_video_channel_is_refused_before_enqueueing(client, api_env):
    """V03: the request must be refused, not queued and failed later.

    Enqueueing would take the project lock and show the user a job that could never
    succeed, so the capability check runs at request time.
    """
    with session_scope() as s:
        s.get(Job, api_env["job_id"]).status = "succeeded"  # release the lock

    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True},
    )
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["error_code"] in {
        "MODEL_NOT_CONFIGURED", "MODEL_CAPABILITY_UNSUPPORTED",
    }
    assert detail["capability_state"] == "unverified"
    assert detail["verified"] is False
    assert "multiframe" in detail["remedy"], "the refusal must offer the fallback"

    # And no job row was created for the refused request.
    with session_scope() as s:
        jobs = s.scalars(
            select(Job).where(
                Job.project_id == api_env["project_id"],
                Job.job_type == "video_analyze",
                Job.status.in_(("pending", "running")),
            )
        ).all()
    assert jobs == [], "a refused request must not leave a queueable job"


def test_a_fallback_request_is_accepted_when_video_is_unavailable(client, api_env):
    """V18: an explicit fallback is allowed; a silent one is not."""
    with session_scope() as s:
        s.get(Job, api_env["job_id"]).status = "succeeded"

    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True,
              "allow_fallback": True, "max_segments": 3},
    )
    assert res.status_code == 202, res.text


def test_a_multiframe_request_is_never_blocked_by_video_capability(client, api_env):
    """V03: the image/multi-frame path must stay usable."""
    with session_scope() as s:
        s.get(Job, api_env["job_id"]).status = "succeeded"

    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "multiframe", "upload_acknowledged": True},
    )
    assert res.status_code == 202, res.text


def test_an_invalid_mode_is_rejected_before_enqueue(client, api_env):
    """A bad mode must fail at validation, not start a job that cannot run."""
    res = client.post(
        f"/api/projects/{api_env['project_id']}/jobs/video_analyze",
        json={"mode": "telepathy"},
    )
    assert res.status_code == 422


def test_starting_a_video_job_for_an_unknown_project_is_404(client):
    res = client.post(
        "/api/projects/no-such-project/jobs/video_analyze",
        json={"upload_acknowledged": True},
    )
    assert res.status_code == 404


def test_the_job_endpoint_is_reachable_as_a_registered_type(client, api_env):
    """Guards the wiring: the type must be accepted by the job service."""
    from app.services.job_service import VALID_JOB_TYPES

    assert "video_analyze" in VALID_JOB_TYPES


# --- authentication ---------------------------------------------------------

def test_video_endpoints_require_the_access_token(client, api_env):
    """C5: /api/* needs the token; this must include the new routes."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1:8737") as bare:
        for path in (
            f"/api/projects/{api_env['project_id']}/video-observations",
            f"/api/projects/{api_env['project_id']}/video-coverage",
            f"/api/segments/{api_env['segment_id']}/video-observation",
            f"/api/segments/{api_env['segment_id']}/video-observation/history",
        ):
            res = bare.get(path)
            assert res.status_code in (401, 403), f"{path} was reachable without a token"


def test_a_non_loopback_host_is_refused(client, api_env):
    """C5: the Host guard applies to the new routes too."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import get_token

    with TestClient(app, base_url="http://evil.example.com",
                    headers={"X-Access-Token": get_token()}) as evil:
        res = evil.get(f"/api/projects/{api_env['project_id']}/video-coverage")
        assert res.status_code == 403
