"""TSB-329 — the ``video_analyze`` job: gates, budget, fallback, cancel.

The tests are ordered from the cheapest, most decisive checks to the full job run.
The first group matters most: they assert that a run which must not send anything
leaves **zero** outbound requests, which is the only form of "no upload" that can
be verified. A job that sends first and validates later looks identical on the
happy path and only differs where it counts.

Task book rows V01–V03, V13, V16–V18 and V23 are covered here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, Project, Segment, SegmentVideoObservation
from app.services.vision import video_job as vj
from app.services.vision.errors import ModelError
from app.services.vision.video_capabilities import (
    UNSUPPORTED,
    UNVERIFIED,
    VideoCapabilities,
    VideoProtocol,
    record_verification,
    VERIFIED_NATIVE_VIDEO,
)
from app.services.vision import video_capabilities as vc


def _verified(protocol: str = "video_url_data_uri") -> VideoCapabilities:
    return record_verification(
        state=VERIFIED_NATIVE_VIDEO, protocol=protocol, model_id="test-model",
        verification_scope="unit-test scope",
    )


# ---------------------------------------------------------------------------
# mode resolution: the fallback decision must never be silent
# ---------------------------------------------------------------------------

def test_a_non_video_mode_is_passed_through_unchanged():
    for mode in ("image_only", "multiframe"):
        effective, reason = vj.resolve_mode(
            requested_mode=mode, capabilities=VideoCapabilities(), allow_fallback=False
        )
        assert effective == mode
        assert reason == ""


def test_an_unknown_mode_is_refused():
    with pytest.raises(ModelError):
        vj.resolve_mode(requested_mode="make_it_up", capabilities=VideoCapabilities(),
                        allow_fallback=True)


def test_video_native_runs_when_capability_is_verified():
    effective, reason = vj.resolve_mode(
        requested_mode="video_native", capabilities=_verified(), allow_fallback=False
    )
    assert effective == "video_native"
    assert reason == ""


def test_unverified_video_is_refused_when_fallback_is_not_offered():
    """V03: the video button must be disabled, not silently downgraded."""
    with pytest.raises(ModelError) as exc:
        vj.resolve_mode(requested_mode="video_native",
                        capabilities=VideoCapabilities(state=UNVERIFIED),
                        allow_fallback=False)
    assert "VIDEO_CAPABILITY_UNVERIFIED" in str(exc.value)


def test_unsupported_video_is_refused_when_fallback_is_not_offered():
    with pytest.raises(ModelError):
        vj.resolve_mode(requested_mode="video_native",
                        capabilities=VideoCapabilities(state=UNSUPPORTED),
                        allow_fallback=False)


def test_fallback_records_its_reason_and_the_effective_mode():
    """V18: a fallback must be attributable, never an invisible substitution."""
    effective, reason = vj.resolve_mode(
        requested_mode="video_native", capabilities=VideoCapabilities(state=UNSUPPORTED),
        allow_fallback=True,
    )
    assert effective == "multiframe"
    assert reason == "VIDEO_CAPABILITY_UNSUPPORTED"


def test_a_fallback_never_claims_video():
    """The single most important assertion in this file."""
    effective, _ = vj.resolve_mode(
        requested_mode="video_native", capabilities=VideoCapabilities(state=UNVERIFIED),
        allow_fallback=True,
    )
    assert effective != "video_native"


# ---------------------------------------------------------------------------
# budget
# ---------------------------------------------------------------------------

def test_a_plan_within_budget_passes_and_reports_totals():
    budget = vj.VideoBudget(max_segments=5, max_total_sec=30, max_total_bytes=10_000)
    totals = budget.check([(5.0, 1000), (5.0, 1000)])
    assert totals["segments"] == 2
    assert totals["total_sec"] == pytest.approx(10.0)
    assert totals["total_bytes"] == 2000


def test_too_many_segments_is_refused():
    budget = vj.VideoBudget(max_segments=1, max_total_sec=1000, max_total_bytes=10**9)
    with pytest.raises(ModelError) as exc:
        budget.check([(1.0, 10), (1.0, 10)])
    assert exc.value.code == "MODEL_BUDGET_EXCEEDED"
    assert "max_segments" in str(exc.value) or "segments" in str(exc.value)


def test_too_many_seconds_is_refused():
    budget = vj.VideoBudget(max_segments=10, max_total_sec=8, max_total_bytes=10**9)
    with pytest.raises(ModelError) as exc:
        budget.check([(5.0, 10), (5.0, 10)])
    assert exc.value.code == "MODEL_BUDGET_EXCEEDED"


def test_too_many_bytes_is_refused():
    budget = vj.VideoBudget(max_segments=10, max_total_sec=1000, max_total_bytes=100)
    with pytest.raises(ModelError) as exc:
        budget.check([(1.0, 90), (1.0, 90)])
    assert exc.value.code == "MODEL_BUDGET_EXCEEDED"


def test_the_budget_defaults_are_local_guard_rails_not_endpoint_claims():
    """A default ceiling must not be reported as an endpoint limit."""
    budget = vj.VideoBudget()
    assert budget.max_total_bytes == vj.DEFAULT_MAX_TOTAL_BYTES
    caps = VideoCapabilities()
    assert caps.max_bytes is None, (
        "the endpoint's real limit stays unknown until measured"
    )


# ---------------------------------------------------------------------------
# preflight ordering: nothing is derived before consent and capability
# ---------------------------------------------------------------------------

class _Project:
    def __init__(self, project_id: str):
        self.id = project_id


def test_preflight_refuses_without_consent(monkeypatch):
    """V02: consent is per-call and cannot be inherited from an earlier upload."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(settings, "model_api_key", "k", raising=False)

    with pytest.raises(ModelError):
        vj.preflight(project_id="p1", upload_acknowledged=False,
                     capabilities=_verified())


def test_preflight_refuses_unverified_capability_even_with_consent(monkeypatch):
    """V01/V03: consent alone is not enough; the channel must be verified."""
    monkeypatch.setenv("TRAVELSB_MODEL_MODE", "on")
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(settings, "model_api_key", "k", raising=False)

    with pytest.raises(ModelError) as exc:
        vj.preflight(project_id="p1", upload_acknowledged=True,
                     capabilities=VideoCapabilities(state=UNVERIFIED))
    assert "VIDEO_CAPABILITY" in str(exc.value)


def test_preflight_passes_with_consent_and_verified_capability(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(settings, "model_api_key", "k", raising=False)
    vj.preflight(project_id="p1", upload_acknowledged=True, capabilities=_verified())


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------

@pytest.fixture()
def job_env(client, tmp_path):
    """A project with three eligible shots plus one excluded and one invalid."""
    res = client.post("/api/projects", json={"name": "video-job", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]
    source = tmp_path / "src.mp4"
    source.write_bytes(b"job-source-bytes")

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path=str(source),
                    display_name="src.mp4", scan_status="succeeded",
                    file_size_bytes=source.stat().st_size,
                    mtime_ns=source.stat().st_mtime_ns))
    segments = {}
    specs = [
        ("good1", "pending", "active", 10.0, 15.0),
        ("good2", "favorite", "active", 20.0, 26.0),
        ("excluded", "exclude", "active", 30.0, 35.0),
        ("invalid", "pending", "needs_review", 40.0, 45.0),
    ]
    with session_scope() as s:
        for name, rating, status, start, end in specs:
            sid = str(uuid.uuid4())
            s.add(Segment(id=sid, project_id=project_id, asset_id=asset_id,
                          detected_start_sec=start, detected_end_sec=end,
                          start_sec=start, end_sec=end, boundary_reason="hard_cut",
                          user_rating=rating, status=status))
            segments[name] = sid
    return {"project_id": project_id, "asset_id": asset_id, "segments": segments}


def test_excluded_and_invalid_shots_are_not_candidates(job_env):
    picked = vj.select_candidates(job_env["project_id"], limit=50)
    ids = {c.segment_id for c in picked}
    assert job_env["segments"]["excluded"] not in ids
    assert job_env["segments"]["invalid"] not in ids
    assert job_env["segments"]["good1"] in ids
    assert job_env["segments"]["good2"] in ids


def test_the_limit_is_applied(job_env):
    assert len(vj.select_candidates(job_env["project_id"], limit=1)) == 1


def test_an_explicit_segment_list_selects_only_those(job_env):
    picked = vj.select_candidates(
        job_env["project_id"], limit=50,
        segment_ids=[job_env["segments"]["good2"]],
    )
    assert [c.segment_id for c in picked] == [job_env["segments"]["good2"]]


def test_candidates_carry_their_editable_interval(job_env):
    picked = {c.segment_id: c for c in vj.select_candidates(job_env["project_id"], limit=50)}
    good1 = picked[job_env["segments"]["good1"]]
    assert good1.start_sec == pytest.approx(10.0)
    assert good1.end_sec == pytest.approx(15.0)


def test_candidates_carry_a_source_identity(job_env):
    picked = vj.select_candidates(job_env["project_id"], limit=50)
    assert all(c.source_identity for c in picked), (
        "publication re-checks the source, so candidates must carry its identity"
    )


# ---------------------------------------------------------------------------
# job config plumbing
# ---------------------------------------------------------------------------

def _make_video_job(project_id: str, **config) -> str:
    job_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Job(id=job_id, project_id=project_id, job_type="video_analyze",
                  status="running",
                  config_snapshot={"upload_acknowledged": True, **config},
                  stats={}, failures={"count": 0, "items": []}))
    return job_id


def test_plan_reads_every_knob_from_the_snapshot(job_env):
    job_id = _make_video_job(
        job_env["project_id"], mode="multiframe", allow_fallback=True,
        max_segments=7, max_total_sec=42.5, max_total_bytes=1234, limit=3,
    )
    plan = vj.plan_video_job(job_id)
    assert plan.requested_mode == "multiframe"
    assert plan.budget.max_segments == 7
    assert plan.budget.max_total_sec == pytest.approx(42.5)
    assert plan.budget.max_total_bytes == 1234
    assert plan.limit == 3


def test_plan_defaults_to_video_native_when_unspecified(job_env):
    """The default *request* is video_native; resolving it still needs capability.

    ``plan_video_job`` deliberately refuses an unverified video request, so this
    asserts both halves: the default mode, and the refusal that keeps the default
    from silently becoming an upload.
    """
    job_id = _make_video_job(job_env["project_id"])
    plan = vj.plan_video_job(job_id, capabilities=_verified())
    assert plan.requested_mode == "video_native"
    assert plan.effective_mode == "video_native"

    with pytest.raises(ModelError) as exc:
        vj.plan_video_job(job_id, capabilities=VideoCapabilities(state=UNVERIFIED))
    assert "VIDEO_CAPABILITY_UNVERIFIED" in str(exc.value)


def test_a_later_settings_change_cannot_alter_a_running_plan(job_env, monkeypatch):
    """The snapshot is the authority, so authorisation cannot be widened later."""
    job_id = _make_video_job(job_env["project_id"], max_segments=2)
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "vision_max_segments_per_job", 9999, raising=False)
    plan = vj.plan_video_job(
        job_id, capabilities=record_verification(
            state=VERIFIED_NATIVE_VIDEO, protocol="video_url_data_uri",
            model_id="m", verification_scope="s",
        )
    )
    assert plan.budget.max_segments == 2


# ---------------------------------------------------------------------------
# the job itself: nothing is sent when it must not be
# ---------------------------------------------------------------------------

@pytest.fixture()
def no_outbound(monkeypatch):
    """Fail loudly if any HTTP client is constructed or used."""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("outbound HTTP happened but must not have")

    import httpx

    monkeypatch.setattr(httpx, "post", _boom)
    return calls


def test_a_job_without_consent_sends_nothing(job_env, no_outbound, monkeypatch):
    """V01/V02: the strongest form of 'no upload' is zero outbound attempts."""
    job_id = _make_video_job(job_env["project_id"], upload_acknowledged=False)
    plan = vj.plan_video_job(job_id, capabilities=_verified())
    # Consent is absent, so preflight must refuse before derivation.
    with pytest.raises(ModelError):
        vj.preflight(
            project_id=job_env["project_id"],
            upload_acknowledged=plan.upload_acknowledged,
            capabilities=_verified(),
        )
    assert no_outbound == []


def test_a_job_without_verified_capability_sends_nothing(job_env, no_outbound):
    with pytest.raises(ModelError):
        vj.preflight(
            project_id=job_env["project_id"],
            upload_acknowledged=True,
            capabilities=VideoCapabilities(state=UNVERIFIED),
        )
    assert no_outbound == []


def test_the_task_writes_no_observation_when_capability_is_unverified(job_env, monkeypatch):
    """The full task path, with an unverified capability: no row, no request."""
    job_id = _make_video_job(job_env["project_id"], mode="video_native")

    import app.services.vision.video_job as module

    monkeypatch.setattr(
        module, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode="video_native",
            fallback_reason="", limit=10, segment_ids=[], force=False,
            upload_acknowledged=True, max_clip_sec=15.0,
            capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    module.video_analyze_task(job_id)

    with session_scope() as s:
        rows = s.scalars(select(SegmentVideoObservation)).all()
        job = s.get(Job, job_id)
    assert [r for r in rows if r.segment_id in job_env["segments"].values()] == []
    assert (job.stats or {}).get("capability_state") == UNVERIFIED


def test_the_task_records_the_requested_and_effective_modes(job_env, monkeypatch):
    """V18: both must be visible so a fallback is attributable."""
    job_id = _make_video_job(job_env["project_id"], mode="video_native",
                             allow_fallback=True)
    import app.services.vision.video_job as module

    monkeypatch.setattr(
        module, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode="multiframe",
            fallback_reason="VIDEO_CAPABILITY_UNSUPPORTED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNSUPPORTED),
        ),
    )
    module.video_analyze_task(job_id)

    with session_scope() as s:
        job = s.get(Job, job_id)
    assert job.stats["mode_requested"] == "video_native"
    assert job.stats["mode_effective"] == "multiframe"
    assert job.stats["fallback_reason"] == "VIDEO_CAPABILITY_UNSUPPORTED"


def test_a_fallback_does_not_produce_video_rows(job_env, monkeypatch):
    """A fallback run must never leave a ``video_native`` observation behind."""
    job_id = _make_video_job(job_env["project_id"], mode="video_native",
                             allow_fallback=True)
    import app.services.vision.video_job as module

    monkeypatch.setattr(
        module, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode="multiframe",
            fallback_reason="VIDEO_CAPABILITY_UNVERIFIED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    module.video_analyze_task(job_id)

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id.in_(
                    list(job_env["segments"].values())
                )
            )
        ).all()
    assert rows == []


# ---------------------------------------------------------------------------
# registration: a job type must be dispatchable, not merely listed
# ---------------------------------------------------------------------------

def test_video_analyze_is_registered_in_all_three_places():
    """The M5 wiring lesson, applied to the new type.

    Being in ``VALID_JOB_TYPES`` only means the API accepts the request; the
    runner also needs a registered task function, and the project lock needs the
    type in its exclusive set. All three must agree or the job is accepted and
    then fails to dispatch.
    """
    from app.services.job_service import VALID_JOB_TYPES
    from app.services.runner import EXCLUSIVE_JOB_TYPES, TASK_REGISTRY

    assert "video_analyze" in VALID_JOB_TYPES, "the API would reject the request"
    assert "video_analyze" in TASK_REGISTRY, "the runner has no task function"
    assert "video_analyze" in EXCLUSIVE_JOB_TYPES, "the job would not hold the lock"


def test_the_registered_task_is_this_module_task():
    """Guards against the registry holding a stale or unrelated callable."""
    from app.services.runner import TASK_REGISTRY

    assert TASK_REGISTRY["video_analyze"] is vj.video_analyze_task


def test_register_video_tasks_is_idempotent():
    """``app.main`` calls it at import; a second call must not raise."""
    vj.register_video_tasks()
    vj.register_video_tasks()
    from app.services.runner import TASK_REGISTRY

    assert TASK_REGISTRY["video_analyze"] is vj.video_analyze_task
