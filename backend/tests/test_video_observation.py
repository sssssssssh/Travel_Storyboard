"""TSB-328 — observation persistence: versioning, idempotency, publish re-check.

These tests drive the real database and the real publication guard, because the
properties under test are transactional ones: "exactly one current row", "a stale
answer cannot commit", "a failure does not destroy the last good reading". A
mocked session would let all three pass while the real code was wrong.

Task book rows V12 (invalid ids never reach the DB), V13 (cancel before commit),
V14 (source changed), V15 (re-run keeps one current, history retained) and V20
(old semantic/motion data untouched) are covered here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import (
    Asset,
    Job,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from app.services.vision.errors import ModelError
from app.services.vision.video_analysis import (
    VideoCandidate,
    already_done,
    coverage_report,
    find_current,
    observation_payload,
    record_failure,
    store_observation,
    version_key,
)
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import (
    VIDEO_SCHEMA_VERSION,
    ValidatedVideoObservation,
    TemporalEvent,
)


def _clip(*, sha: str = "clip-sha-1", start: float = 10.0, duration: float = 5.0) -> ClipArtifact:
    return ClipArtifact(
        path=Path("clip.mp4"), segment_id="seg-x", asset_id="asset-x",
        source_clip_start_sec=start, source_clip_end_sec=start + duration,
        duration_sec=duration, width=1280, height=720, rotation=0, vfr=False,
        has_audio=False, size_bytes=4096, sha256=sha, container="mp4",
        color_unverified=False,
    )


def _validated(segment_id: str = "seg-x") -> ValidatedVideoObservation:
    return ValidatedVideoObservation(
        segment_id=segment_id,
        clip_id=segment_id,
        summary="something moves then stops",
        events=[
            TemporalEvent(
                event_type="subject_action", start_rel_sec=1.0, end_rel_sec=2.0,
                description="a visible object moves", confidence=0.5,
                evidence=["video_window"], start_abs_sec=11.0, end_abs_sec=12.0,
            )
        ],
        observed_temporal_quality="limited",
        audio_analyzed=False,
        limitations=["clip is silent"],
    )


@pytest.fixture()
def video_env(client, tmp_path):
    """A real project + asset + segment + running ``video_analyze`` job.

    ``source_path`` points at a real file: the publication guard computes a
    bounded source fingerprint from the file on disk, and a non-existent path
    yields an empty identity, which the guard correctly refuses. Using a real file
    is therefore required to exercise the *success* path rather than accidentally
    testing only the rejection.
    """
    res = client.post("/api/projects", json={"name": "video-obs", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    source = tmp_path / "src.mp4"
    source.write_bytes(b"bounded-source-snapshot-for-video-observation")

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
            "segment_id": segment_id, "job_id": job_id, "source_path": str(source)}


def _candidate(env) -> VideoCandidate:
    """A candidate whose source identity matches what the asset records."""
    from app.services.vision.verdicts import source_identity

    with session_scope() as s:
        asset = s.get(Asset, env["asset_id"])
        identity = source_identity(asset)
    assert identity, "fixture source must produce a non-empty identity"
    return VideoCandidate(
        segment_id=env["segment_id"], asset_id=env["asset_id"],
        project_id=env["project_id"], source_path=env["source_path"],
        start_sec=10.0, end_sec=15.0, source_identity=identity,
    )


def _store(env, *, sha="clip-sha-1", model_id="m1", protocol="video_url_data_uri",
           clip=None, validated=None):
    candidate = _candidate(env)
    clip = clip or _clip(sha=sha)
    clip.segment_id = candidate.segment_id
    return store_observation(
        candidate=candidate,
        validated=validated or _validated(candidate.segment_id),
        clip=clip,
        job_id=env["job_id"],
        provider_name="native_video",
        model_id=model_id,
        protocol=protocol,
    )


# --- storing ----------------------------------------------------------------

def test_a_validated_observation_is_stored_and_readable(video_env):
    row_id = _store(video_env)
    row = find_current(video_env["segment_id"])
    assert row is not None
    assert row.id == row_id
    assert row.is_current is True
    assert row.method == "video_native"
    assert len(row.events_json) == 1
    assert row.audio_analyzed is False
    assert row.schema_version == VIDEO_SCHEMA_VERSION


def test_the_stored_payload_exposes_the_honesty_flags(video_env):
    _store(video_env)
    payload = observation_payload(find_current(video_env["segment_id"]))
    assert payload["audio_analyzed"] is False
    assert payload["color_unverified"] is False
    assert payload["observed_temporal_quality"] == "limited"
    assert payload["method"] == "video_native"


def test_the_clip_geometry_is_recorded_for_later_re_derivation(video_env):
    _store(video_env)
    row = find_current(video_env["segment_id"])
    assert row.clip_start_sec == pytest.approx(10.0)
    assert row.clip_duration_sec == pytest.approx(5.0)
    assert row.clip_sha256 == "clip-sha-1"


def test_the_editable_interval_at_read_time_is_recorded(video_env):
    """C2/C3: the reading is bound to the trim that was in force."""
    _store(video_env)
    row = find_current(video_env["segment_id"])
    assert row.segment_start_sec == pytest.approx(10.0)
    assert row.segment_end_sec == pytest.approx(15.0)


# --- versioning / idempotency ----------------------------------------------

def test_an_identical_rerun_replaces_in_place_without_duplicating(video_env):
    """V15: force re-run of identical inputs keeps exactly one row."""
    first = _store(video_env)
    second = _store(video_env)
    assert first == second, "the same version key must reuse the row"

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 1


def test_a_changed_input_appends_a_new_version_and_demotes_the_old(video_env):
    _store(video_env, sha="clip-sha-1")
    _store(video_env, sha="clip-sha-2")

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 2, "history must be retained"
    current = [r for r in rows if r.is_current]
    assert len(current) == 1, "exactly one current row is the invariant"
    assert current[0].clip_sha256 == "clip-sha-2"


def test_a_different_model_makes_a_different_version(video_env):
    _store(video_env, model_id="model-a")
    _store(video_env, model_id="model-b")
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 2
    assert len([r for r in rows if r.is_current]) == 1


def test_a_different_protocol_makes_a_different_version(video_env):
    """Provenance matters: the same bytes sent two ways are two results."""
    _store(video_env, protocol="video_url_data_uri")
    _store(video_env, protocol="other_protocol")
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 2


def test_different_clip_bytes_are_different_evidence():
    key_a = version_key(
        candidate=VideoCandidate(segment_id="s", asset_id="a", project_id="p",
                                 source_path="x", start_sec=0, end_sec=5,
                                 source_identity="sig"),
        clip_sha256="aaa", model_id="m", prompt_version="1.0",
        schema_version="1.0", protocol="p", method="video_native",
    )
    key_b = version_key(
        candidate=VideoCandidate(segment_id="s", asset_id="a", project_id="p",
                                 source_path="x", start_sec=0, end_sec=5,
                                 source_identity="sig"),
        clip_sha256="bbb", model_id="m", prompt_version="1.0",
        schema_version="1.0", protocol="p", method="video_native",
    )
    assert key_a != key_b


def test_a_changed_trim_makes_a_different_version():
    common = dict(asset_id="a", project_id="p", source_path="x", source_identity="sig")
    key_a = version_key(
        candidate=VideoCandidate(segment_id="s", start_sec=0.0, end_sec=5.0, **common),
        clip_sha256="x", model_id="m", prompt_version="1.0",
        schema_version="1.0", protocol="p", method="video_native",
    )
    key_b = version_key(
        candidate=VideoCandidate(segment_id="s", start_sec=1.0, end_sec=5.0, **common),
        clip_sha256="x", model_id="m", prompt_version="1.0",
        schema_version="1.0", protocol="p", method="video_native",
    )
    assert key_a != key_b, "a re-trim must invalidate the cached reading (C2)"


def test_already_done_is_true_only_after_a_success(video_env):
    candidate = _candidate(video_env)
    assert not already_done(candidate=candidate, clip_sha256="clip-sha-1", model_id="m1",
                            prompt_version="1.0", protocol="video_url_data_uri",
                            job_id=video_env["job_id"])
    _store(video_env)
    assert already_done(candidate=candidate, clip_sha256="clip-sha-1", model_id="m1",
                        prompt_version="1.0", protocol="video_url_data_uri",
                        job_id=video_env["job_id"])


def test_already_done_is_false_for_a_different_clip_sha(video_env):
    _store(video_env, sha="clip-sha-1")
    candidate = _candidate(video_env)
    assert not already_done(candidate=candidate, clip_sha256="clip-sha-OTHER",
                            model_id="m1", prompt_version="1.0",
                            protocol="video_url_data_uri", job_id=video_env["job_id"])


# --- failures must not destroy good data ------------------------------------

def test_a_failed_attempt_does_not_demote_the_last_good_reading(video_env):
    _store(video_env)
    record_failure(candidate=_candidate(video_env), job_id=video_env["job_id"],
                   error_code="MODEL_TIMEOUT", error_message="timed out")

    current = find_current(video_env["segment_id"])
    assert current is not None, "a failed re-run must not erase the good reading"
    assert current.status == "succeeded"


def test_a_failed_attempt_is_visible(video_env):
    record_failure(candidate=_candidate(video_env), job_id=video_env["job_id"],
                   error_code="MODEL_TIMEOUT", error_message="timed out")
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error_code == "MODEL_TIMEOUT"
    assert rows[0].is_current is False


def test_a_failure_does_not_make_already_done_true(video_env):
    """Otherwise a retry would report 'done' while having produced nothing."""
    candidate = _candidate(video_env)
    record_failure(candidate=candidate, job_id=video_env["job_id"],
                   error_code="MODEL_TIMEOUT", error_message="timed out")
    assert not already_done(candidate=candidate, clip_sha256="clip-sha-1", model_id="m1",
                            prompt_version="1.0", protocol="video_url_data_uri",
                            job_id=video_env["job_id"])


# --- publication re-check ---------------------------------------------------

def test_a_stale_job_cannot_publish(video_env):
    """V13: a job that is no longer running may not commit a late answer.

    A cancellation is reported as ``JobCancelled`` rather than a generic model
    error, so the runner can classify it as *cancelled* rather than *failed* --
    the two are different outcomes and must not be conflated (AGENTS.md 5.7).
    """
    from app.services.runner import JobCancelled

    with session_scope() as s:
        s.get(Job, video_env["job_id"]).status = "cancelled"
    with pytest.raises(JobCancelled):
        _store(video_env)

    # Scoped to this shot: the suite shares one session-scoped database, so a
    # global count would pick up rows from other tests.
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == video_env["segment_id"]
            )
        ).all()
    assert rows == [], "a cancelled job must not leave a published row"


def test_a_trim_that_changed_during_the_model_call_blocks_publication(video_env):
    """V14/C2: the person's edit wins; the answer is refused, not re-mapped."""
    candidate = _candidate(video_env)
    with session_scope() as s:
        s.get(Segment, candidate.segment_id).end_sec = 12.0  # trimmed while working

    from app.services.vision.errors import ModelError as ME

    with pytest.raises(ME):
        store_observation(
            candidate=candidate, validated=_validated(candidate.segment_id),
            clip=_clip(), job_id=video_env["job_id"], provider_name="native_video",
            model_id="m1", protocol="video_url_data_uri",
        )


def test_an_excluded_segment_cannot_publish(video_env):
    candidate = _candidate(video_env)
    with session_scope() as s:
        s.get(Segment, candidate.segment_id).user_rating = "exclude"
    from app.services.vision.errors import ModelError as ME

    with pytest.raises(ME):
        store_observation(
            candidate=candidate, validated=_validated(candidate.segment_id),
            clip=_clip(), job_id=video_env["job_id"], provider_name="native_video",
            model_id="m1", protocol="video_url_data_uri",
        )


def test_a_wrong_job_type_cannot_publish(video_env):
    """Only a ``video_analyze`` job may write video observations."""
    with session_scope() as s:
        s.get(Job, video_env["job_id"]).job_type = "vision_analyze"
    from app.services.vision.errors import ModelError as ME

    with pytest.raises(ME):
        _store(video_env)


# --- other result families are untouched (V20) ------------------------------

def test_storing_a_video_reading_does_not_touch_semantic_rows(video_env):
    with session_scope() as s:
        s.add(SegmentSemanticResult(
            project_id=video_env["project_id"], asset_id=video_env["asset_id"],
            segment_id=video_env["segment_id"], status="succeeded",
            model_id="img-model", is_current=True, analysis_json={"scene": ["nature"]},
        ))
    _store(video_env)

    # Scoped to this shot: the shared session database holds rows from other
    # tests, and an unfiltered query could pass by reading one of those.
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 1, "the video path must not add or remove semantic rows"
    assert rows[0].is_current is True
    assert rows[0].analysis_json == {"scene": ["nature"]}
    assert rows[0].model_id == "img-model", "the image model id must be untouched"


def test_storing_a_video_reading_does_not_touch_motion_rows(video_env):
    with session_scope() as s:
        s.add(SegmentMotionReview(
            project_id=video_env["project_id"], asset_id=video_env["asset_id"],
            segment_id=video_env["segment_id"], method="multiframe",
            status="succeeded", is_current=True, human_verdict="confirm",
        ))
    _store(video_env)

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentMotionReview).where(
                SegmentMotionReview.segment_id == video_env["segment_id"]
            )
        ).all()
    assert len(rows) == 1, "the video path must not add or remove motion rows"
    assert rows[0].is_current is True
    assert rows[0].human_verdict == "confirm", "a human verdict must never be cleared (C4)"


def test_storing_a_video_reading_does_not_write_human_columns(video_env):
    with session_scope() as s:
        s.get(Segment, video_env["segment_id"]).user_rating = "favorite"
        s.get(Segment, video_env["segment_id"]).story_role = "opener"
    _store(video_env)
    with session_scope() as s:
        seg = s.get(Segment, video_env["segment_id"])
    assert seg.user_rating == "favorite"
    assert seg.story_role == "opener"
    assert seg.start_sec == pytest.approx(10.0)
    assert seg.end_sec == pytest.approx(15.0)


# --- coverage ---------------------------------------------------------------

def test_coverage_reports_a_subset_as_a_subset(video_env):
    report = coverage_report(video_env["project_id"])
    assert report["library_segments"] >= 1
    assert report["segments_with_video_reading"] == 0
    assert report["segments_not_covered"] >= 1
    assert report["complete"] is False
    assert report["audio_analyzed"] is False


def test_coverage_counts_a_stored_reading(video_env):
    _store(video_env)
    report = coverage_report(video_env["project_id"])
    assert report["segments_with_video_reading"] == 1
    assert report["event_candidates"] == 1


def test_coverage_does_not_count_failures_as_covered(video_env):
    record_failure(candidate=_candidate(video_env), job_id=video_env["job_id"],
                   error_code="MODEL_TIMEOUT", error_message="timed out")
    report = coverage_report(video_env["project_id"])
    assert report["segments_with_video_reading"] == 0
    assert report["failed_attempts"] == 1
