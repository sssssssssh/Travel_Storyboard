"""TSB-368-R2 — production write path round-trip.

The R1 lesson was that a fixture encoding the *reader's* idea of the format proves
nothing. So this file does not hand-write ``events_json``: it drives the real
chain

    model response
      → video_schema.validate_temporal_events   (real validator)
      → TemporalEvent.to_dict()                 (real serializer)
      → video_analysis.store_observation        (real persistence, real job check)
      → SegmentVideoObservation row in SQLite
      → GET /segments/{id}/video-observation
      → GET /projects/{id}/director/candidates
      → GET /projects/{id}/director/context

and checks the same event survives every hop with its type, description and
absolute times intact.

The source file must be real because ``validate_publication`` recomputes
``source_identity(asset)`` and refuses to publish when it does not match. A
synthetic clip is generated with the project's own FFmpeg for that reason.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Job, Project, Segment, SegmentVideoObservation
from app.services.vision import video_analysis as va
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import validate_temporal_events
from app.services.vision.verdicts import source_identity

pytestmark = pytest.mark.usefixtures("_app_ready")


def _uid() -> str:
    return str(uuid.uuid4())


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg

    exe = locate_ffmpeg()
    if not exe:
        pytest.skip("ffmpeg is required for the production round-trip test")
    return str(exe)


@pytest.fixture()
def synthetic_source(tmp_path: Path) -> Path:
    """A short real file so source_identity can be computed."""
    out = tmp_path / "r2-source.mp4"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi",
         "-i", "testsrc=s=320x180:d=6:r=25",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         str(out)],
        check=True, capture_output=True,
    )
    return out


def _make_project(source: Path) -> dict:
    """Project + asset + one 10–15s shot + a running video job."""
    pid = _uid()
    aid = _uid()
    sid = _uid()
    jid = _uid()
    stat = source.stat()
    with session_scope() as s:
        s.add(Project(id=pid, name="r2-e2e", source_roots=[], output_dir="", settings={}))
        s.flush()
        s.add(Asset(
            id=aid, project_id=pid, source_path=str(source),
            display_name="r2-source.mp4", file_size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns, quick_fingerprint="fp", duration_sec=6.0,
            fps_num=25, fps_den=1, vfr_flag=False, width=320, height=180, rotation=0,
            codec="h264", pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        ))
        s.flush()
        s.add(Segment(
            id=sid, asset_id=aid, project_id=pid,
            detected_start_sec=0.0, detected_end_sec=6.0,
            start_sec=0.0, end_sec=6.0, is_trimmed=False, boundary_reason="",
            quality_metrics={}, content_tags={}, story_role="", user_rating="pending",
            status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
            algorithm_version="1", created_at=None,
        ))
        s.add(Job(
            id=jid, project_id=pid, job_type=va.VIDEO_JOB_TYPE, status="running",
            config_snapshot={}, stats={}, failures={}, resume_token={}, error="",
            created_at=None, started_at=None, finished_at=None,
        ))
    return {"project_id": pid, "asset_id": aid, "segment_id": sid, "job_id": jid}


def _candidate(ids: dict, source: Path) -> va.VideoCandidate:
    with session_scope() as s:
        asset = s.get(Asset, ids["asset_id"])
        identity = source_identity(asset)
    return va.VideoCandidate(
        segment_id=ids["segment_id"], asset_id=ids["asset_id"],
        project_id=ids["project_id"], source_path=str(source),
        start_sec=0.0, end_sec=6.0, source_identity=identity,
    )


def _clip(ids: dict) -> ClipArtifact:
    """A derived clip over 0–6 s; events are expressed relative to it."""
    return ClipArtifact(
        path=Path("/dev/null"), segment_id=ids["segment_id"], asset_id=ids["asset_id"],
        source_clip_start_sec=0.0, source_clip_end_sec=6.0, duration_sec=6.0,
        width=320, height=180, rotation=0, vfr=False, has_audio=False,
        size_bytes=1000, sha256="b" * 64, container="mp4", color_unverified=False,
    )


def _model_response(ids: dict) -> dict:
    """What a provider returns: clip-relative events, plus a summary."""
    return {
        "clip_id": ids["segment_id"],
        "segment_id": ids["segment_id"],
        "summary": "a test pattern with a moving element crosses the frame",
        "observed_temporal_quality": "limited",
        "audio_analyzed": False,
        "events": [
            {
                "event_type": "subject_action",
                "start_rel_sec": 1.0,
                "end_rel_sec": 2.5,
                "description": "the pattern shifts to the left half",
                "confidence": 0.8,
            },
            {
                "event_type": "camera_motion",
                "start_rel_sec": 3.0,
                "end_rel_sec": 4.5,
                "description": "the frame scrolls upward",
                "confidence": 0.55,
            },
        ],
    }


def _persist(ids: dict, source: Path) -> str:
    """Drive the real persistence call and return the stored row id."""
    clip = _clip(ids)
    candidate = _candidate(ids, source)
    validated = validate_temporal_events(
        _model_response(ids), clip=clip,
        segment_start_sec=0.0, segment_end_sec=6.0,
    )
    return va.store_observation(
        candidate=candidate, validated=validated, clip=clip,
        job_id=ids["job_id"], provider_name="mock",
        model_id="mock-video-1", prompt_version="p1", protocol="native",
        usage={"total_tokens": 10},
    )


# ---------------------------------------------------------------------------
# The round-trip
# ---------------------------------------------------------------------------

def _app_ready_fixture_note() -> None:  # pragma: no cover - documentation only
    """`_app_ready` comes from conftest and gives a migrated isolated database."""


def test_store_observation_persists_production_event_shape(synthetic_source):
    """The DB must contain the serializer's own keys, not a look-alike."""
    ids = _make_project(synthetic_source)
    row_id = _persist(ids, synthetic_source)

    with session_scope() as s:
        row = s.get(SegmentVideoObservation, row_id)
        assert row is not None
        stored = row.events_json
    assert len(stored) == 2
    first = stored[0]
    # The production contract keys, exactly.
    assert first["event_type"] == "subject_action"
    assert first["description"] == "the pattern shifts to the left half"
    assert first["start_abs_sec"] == pytest.approx(1.0, abs=1e-3)
    assert first["end_abs_sec"] == pytest.approx(2.5, abs=1e-3)
    assert first["confidence_is_calibrated"] is False


def test_native_video_api_returns_the_stored_events(synthetic_source, client):
    ids = _make_project(synthetic_source)
    row_id = _persist(ids, synthetic_source)

    r = client.get(f"/api/segments/{ids['segment_id']}/video-observation")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == row_id
    events = body["events"]
    assert len(events) == 2
    assert events[0]["event_type"] == "subject_action"
    assert events[0]["start_abs_sec"] == pytest.approx(1.0, abs=1e-3)


def test_director_candidates_surface_the_same_events(synthetic_source, client):
    ids = _make_project(synthetic_source)
    row_id = _persist(ids, synthetic_source)

    r = client.get(f"/api/projects/{ids['project_id']}/director/candidates?eligibility=all&limit=10")
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["segment_id"] == ids["segment_id"])
    vn = item["observations"]["video_native"]
    assert vn["result_id"] == row_id
    assert vn["state"] in ("current", "freshness_unverified")
    detail = vn["status_detail"]
    assert "test pattern" in detail["summary"]
    assert len(detail["events"]) == 2
    assert detail["events"][0]["event_type"] == "subject_action"
    assert "shifts to the left half" in detail["events"][0]["label"]
    assert detail["events"][1]["event_type"] == "camera_motion"


def test_context_references_the_same_result_and_content(synthetic_source, client):
    """End of the chain: the context points at the row that actually holds it."""
    ids = _make_project(synthetic_source)
    row_id = _persist(ids, synthetic_source)

    r = client.get(f"/api/projects/{ids['project_id']}/director/context?max_candidates=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert ids["segment_id"] in body["candidate_segment_ids"]
    summary = next(
        x for x in body["candidate_summaries"] if x["segment_id"] == ids["segment_id"]
    )
    readings = [x for x in summary["readings"] if x["channel"] == "video_native"]
    assert len(readings) == 1
    assert readings[0]["result_id"] == row_id

    # Following the reference must return the same content.
    ob = client.get(f"/api/segments/{ids['segment_id']}/video-observation").json()
    assert ob["id"] == readings[0]["result_id"]
    assert ob["events"][0]["description"] == "the pattern shifts to the left half"


def test_no_raw_model_payload_or_path_leaks(synthetic_source, client):
    ids = _make_project(synthetic_source)
    _persist(ids, synthetic_source)

    blob = json.dumps(
        client.get(f"/api/projects/{ids['project_id']}/director/candidates?eligibility=all&limit=10").json()
    )
    assert "total_tokens" not in blob
    assert "request_usage" not in blob
    assert str(synthetic_source) not in blob
    assert "source_path" not in blob


# ---------------------------------------------------------------------------
# Superseding: a re-run must swap the current row, not blur two answers
# ---------------------------------------------------------------------------

def test_a_second_run_supersedes_the_first_and_only_one_remains_current(synthetic_source):
    ids = _make_project(synthetic_source)
    first_id = _persist(ids, synthetic_source)
    second_id = _persist(ids, synthetic_source)

    with session_scope() as s:
        rows = s.query(SegmentVideoObservation).filter(
            SegmentVideoObservation.segment_id == ids["segment_id"]).all()
        currents = [r for r in rows if r.is_current]
    assert len(currents) == 1, "exactly one observation may be current"
    assert currents[0].id == second_id


def test_the_candidate_reads_the_current_row_after_a_rerun(synthetic_source, client):
    ids = _make_project(synthetic_source)
    _persist(ids, synthetic_source)
    second_id = _persist(ids, synthetic_source)

    r = client.get(f"/api/projects/{ids['project_id']}/director/candidates?eligibility=all&limit=10")
    item = next(i for i in r.json()["items"] if i["segment_id"] == ids["segment_id"])
    assert item["observations"]["video_native"]["result_id"] == second_id


def test_a_source_change_blocks_publication_visibly(synthetic_source):
    """The guard must refuse rather than publish a stale answer."""
    from app.services.vision.errors import ModelError

    ids = _make_project(synthetic_source)
    clip = _clip(ids)
    candidate = _candidate(ids, synthetic_source)
    # Mutate the stored identity so validate_publication sees drift.
    candidate.source_identity = "tampered"
    validated = validate_temporal_events(
        _model_response(ids), clip=clip, segment_start_sec=0.0, segment_end_sec=6.0)

    with pytest.raises(ModelError) as excinfo:
        va.store_observation(
            candidate=candidate, validated=validated, clip=clip,
            job_id=ids["job_id"], provider_name="mock",
            model_id="mock-video-1", prompt_version="p1", protocol="native",
        )
    assert excinfo.value.code in ("SOURCE_CHANGED", "JOB_INVALID")


def test_publication_is_refused_when_the_job_is_not_running(synthetic_source):
    from app.services.vision.errors import ModelError

    ids = _make_project(synthetic_source)
    with session_scope() as s:
        s.get(Job, ids["job_id"]).status = "succeeded"

    clip = _clip(ids)
    candidate = _candidate(ids, synthetic_source)
    validated = validate_temporal_events(
        _model_response(ids), clip=clip, segment_start_sec=0.0, segment_end_sec=6.0)

    with pytest.raises(ModelError) as excinfo:
        va.store_observation(
            candidate=candidate, validated=validated, clip=clip,
            job_id=ids["job_id"], provider_name="mock",
            model_id="mock-video-1", prompt_version="p1", protocol="native",
        )
    assert excinfo.value.code == "JOB_INVALID"
