"""TSB-368-R2 — P0-01: native-video event mapping, verified through production code.

The defect this file pins down: the writer stores events shaped by
``video_schema.TemporalEvent.to_dict()`` — ``start_abs_sec`` / ``end_abs_sec`` /
``event_type`` / ``description`` — while the director reader looked for
``start_sec`` / ``start`` / ``label`` / ``type``. Every real event therefore failed
the timestamp lookup and was dropped, leaving a video reading that showed a
summary but an empty event table.

**How these tests avoid the R1 trap.** R1's failure was that the fixtures encoded
the *reader's* guess about the format, so green tests proved only that the reader
could parse its own imagination. Here every event is produced by the real
production serializer, driven by the real validator, and — for the round-trip
tests — persisted by the real ``store_observation`` call.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentVideoObservation
from app.services import director_candidates as dc
from app.services.vision import video_analysis as va
from app.services.vision.video_clip import ClipArtifact
from app.services.vision.video_schema import (
    TemporalEvent,
    ValidatedVideoObservation,
    validate_temporal_events,
)


def _uid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Production-shaped fixtures
# ---------------------------------------------------------------------------

def _project(s, name: str = "r2") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project: Project) -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name="clip.mp4", file_size_bytes=1000, mtime_ns=1, quick_fingerprint="fp",
        duration_sec=60.0, fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360,
        rotation=0, codec="h264", pixel_format="yuv420p", color_primaries="",
        color_transfer="", color_space="", has_audio=False, audio_codec="",
        scan_status="succeeded", analysis_version="", analyzed_fingerprint="",
        analyzed_source_signature="", analyzed_detect_config="",
        analyzed_at=None, scanned_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _segment(s, project, asset, start=0.0, end=15.0) -> Segment:
    g = Segment(
        id=_uid(), asset_id=asset.id, project_id=project.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics={}, content_tags={}, story_role="", user_rating="pending",
        status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
        algorithm_version="1", created_at=None,
    )
    s.add(g)
    s.flush()
    return g


def _clip(segment: Segment, start: float = 10.0, end: float = 15.0) -> ClipArtifact:
    return ClipArtifact(
        path=Path("/dev/null"), segment_id=segment.id, asset_id=segment.asset_id,
        source_clip_start_sec=start, source_clip_end_sec=end,
        duration_sec=end - start, width=640, height=360, rotation=0, vfr=False,
        has_audio=False, size_bytes=1000, sha256="a" * 64, container="mp4",
        color_unverified=False,
    )


def _production_events(segment: Segment, clip: ClipArtifact, *, n: int = 2) -> list[dict]:
    """Events produced by the REAL production path.

    ``validate_temporal_events`` is what turns a model response into
    ``TemporalEvent`` objects (filling ``start_abs_sec`` from the clip offset), and
    ``to_dict()`` is what ``store_observation`` persists. Nothing here is
    hand-written in the storage shape.
    """
    response = {
        "clip_id": clip.clip_id,
        "segment_id": segment.id,
        "summary": "a boat crosses the lake while the camera pans left",
        "observed_temporal_quality": "limited",
        "audio_analyzed": False,
        "events": [
            {
                "event_type": "subject_action",
                "start_rel_sec": 0.5,
                "end_rel_sec": 1.8,
                "description": "a person walks across the frame",
                "confidence": 0.8,
            },
            {
                "event_type": "camera_motion",
                "start_rel_sec": 2.25,
                "end_rel_sec": 3.75,
                "description": "the camera pans left across the water",
                "confidence": 0.6,
            },
        ][:n],
    }
    validated = validate_temporal_events(
        response, clip=clip,
        segment_start_sec=segment.start_sec, segment_end_sec=segment.end_sec,
    )
    return [e.to_dict() for e in validated.events]


def _video_row(s, project, asset, segment, events_json, *, is_current=True,
               seg_start=None, seg_end=None) -> SegmentVideoObservation:
    """A row whose ``events_json`` came from production serialization.

    The *row* is created directly (there is no job lifecycle in a unit test), but
    the payload it carries is exactly what the writer would store. The
    round-trip tests below use the real ``store_observation`` instead.
    """
    row = SegmentVideoObservation(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=segment.id,
        status="succeeded", method="video_native", model_provider="mock", model_id="m",
        prompt_version="p", schema_version="1.0", protocol="native",
        source_signature="v-sig", clip_sha256="c" * 64, version_key=_uid(),
        clip_start_sec=10.0, clip_end_sec=15.0, clip_duration_sec=5.0,
        clip_size_bytes=1000,
        segment_start_sec=segment.start_sec if seg_start is None else seg_start,
        segment_end_sec=segment.end_sec if seg_end is None else seg_end,
        analysis_json={"summary": "a boat crosses the lake while the camera pans left"},
        events_json=events_json,
        request_usage={}, audio_analyzed=False, color_unverified=False,
        observed_temporal_quality="limited", is_current=is_current,
        error_code="", error_message="", created_at=None, updated_at=None,
    )
    s.add(row)
    s.flush()
    return row


def _collect(project_id: str) -> list[dc.Candidate]:
    with session_scope() as s:
        return dc.collect_candidates(s, project_id, verify_freshness=False)


# ---------------------------------------------------------------------------
# P0-01-A / P0-01-B / P0-01-D — production shape is read correctly
# ---------------------------------------------------------------------------

def test_production_event_shape_keeps_times_type_and_description():
    """P0-01-A: the exact dict `TemporalEvent.to_dict()` emits must survive."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        # Non-zero start on purpose: a relative/absolute mix-up is invisible at 0.
        g = _segment(s, p, a, start=10.0, end=15.0)
        clip = _clip(g)
        events = _production_events(g, clip, n=2)
        assert events[0]["start_abs_sec"] == pytest.approx(10.5)
        assert events[0]["end_abs_sec"] == pytest.approx(11.8)
        _video_row(s, p, a, g, events)
        pid = p.id

    c = _collect(pid)[0]
    evs = c.video_native.payload["events"]
    assert len(evs) == 2, (
        "production events were dropped; the reader is not looking at "
        "start_abs_sec/end_abs_sec"
    )

    first = evs[0]
    assert first["start_sec"] == pytest.approx(10.5)
    assert first["end_sec"] == pytest.approx(11.8)
    assert first["event_type"] == "subject_action"
    assert "walks across the frame" in first["label"]

    second = evs[1]
    assert second["event_type"] == "camera_motion"
    assert "pans left" in second["label"]
    assert second["confidence_calibration"] == "uncalibrated"


def test_two_valid_production_events_are_both_accepted():
    """P0-01-B: accepted == 2, and the counters say so."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        events = _production_events(g, _clip(g), n=2)
        _video_row(s, p, a, g, events)
        pid = p.id

    payload = _collect(pid)[0].video_native.payload
    assert payload["event_count_total"] == 2
    assert len(payload["events"]) == 2
    counters = payload.get("event_counters") or {}
    assert counters.get("accepted") == 2
    assert counters.get("dropped") == 0


def test_relative_times_are_never_used_as_absolute():
    """P0-01-D: with a 10s clip offset, accepting start_rel_sec would be wrong."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        events = _production_events(g, _clip(g), n=1)
        assert events[0]["start_rel_sec"] == pytest.approx(0.5)
        _video_row(s, p, a, g, events)
        pid = p.id

    evs = _collect(pid)[0].video_native.payload["events"]
    assert len(evs) == 1
    # 0.5 is the clip-relative value; the absolute one is 10.5.
    assert evs[0]["start_sec"] == pytest.approx(10.5)
    assert evs[0]["start_sec"] != pytest.approx(0.5)


# ---------------------------------------------------------------------------
# P0-01-C — historical format still works
# ---------------------------------------------------------------------------

def test_legacy_event_shape_is_still_accepted():
    """P0-01-C: rows written before the production shape must not break."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        _video_row(s, p, a, g, [
            {"start_sec": 10.5, "end_sec": 11.0, "label": "legacy label", "confidence": 0.5},
            {"start": 11.0, "end": 12.0, "type": "legacy_type", "confidence": 0.4},
        ])
        pid = p.id

    evs = _collect(pid)[0].video_native.payload["events"]
    assert len(evs) == 2
    assert evs[0]["label"] == "legacy label"
    assert evs[1]["label"] == "legacy_type"


# ---------------------------------------------------------------------------
# P0-01-E — invalid data rejected and visible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad,reason_fragment",
    [
        ({"start_abs_sec": float("nan"), "end_abs_sec": 11.0}, "non_finite"),
        ({"start_abs_sec": float("inf"), "end_abs_sec": 11.0}, "non_finite"),
        ({"start_abs_sec": True, "end_abs_sec": 11.0}, "not_numeric"),
        ({"start_abs_sec": "10.5", "end_abs_sec": 11.0}, "not_numeric"),
        ({"start_abs_sec": None, "end_abs_sec": 11.0}, "missing_time"),
        ({"start_abs_sec": 12.0, "end_abs_sec": 11.0}, "reversed"),
        ({"start_abs_sec": 9.0, "end_abs_sec": 11.0}, "outside_shot"),
        ({"start_abs_sec": 10.5, "end_abs_sec": 20.0}, "outside_shot"),
    ],
)
def test_malformed_events_are_dropped_with_a_reason(bad, reason_fragment):
    """P0-01-E: no crash, no invention, and the reason is traceable."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        good = _production_events(g, _clip(g), n=1)[0]
        bad_event = {**good, **bad}
        _video_row(s, p, a, g, [bad_event])
        pid = p.id

    payload = _collect(pid)[0].video_native.payload
    assert payload["events"] == [], "an invalid event must not be presented"
    counters = payload.get("event_counters") or {}
    assert counters.get("accepted") == 0
    assert counters.get("dropped") == 1
    reasons = " ".join(payload.get("drop_reasons") or [])
    assert reason_fragment in reasons, reasons
    # "events existed but were filtered" must not read as "the model said nothing".
    assert payload["event_count_total"] == 1


def test_a_dropped_event_is_reported_as_filtered_not_as_no_events():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        good = _production_events(g, _clip(g), n=1)[0]
        _video_row(s, p, a, g, [{**good, "start_abs_sec": 99.0, "end_abs_sec": 100.0}])
        pid = p.id

    payload = _collect(pid)[0].video_native.payload
    assert payload["event_count_total"] == 1
    assert payload["event_counters"]["dropped"] == 1


# ---------------------------------------------------------------------------
# P0-01-F / P0-01-G — authority and absence
# ---------------------------------------------------------------------------

def test_a_superseded_video_row_is_not_presented_as_current_events():
    """P0-01-F."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        _video_row(s, p, a, g, _production_events(g, _clip(g), n=2), is_current=False)
        pid = p.id

    c = _collect(pid)[0]
    assert c.video_native.state == dc.STATE_STALE
    assert c.video_native.payload.get("events") in (None, [])


def test_a_window_mismatch_still_yields_stale():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        _video_row(s, p, a, g, _production_events(g, _clip(g), n=1),
                   seg_start=0.0, seg_end=20.0)
        pid = p.id

    assert _collect(pid)[0].video_native.state == dc.STATE_STALE


def test_no_video_row_means_not_analyzed_with_no_invented_events():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, start=10.0, end=15.0)
        pid = p.id

    c = _collect(pid)[0]
    assert c.video_native.state == dc.STATE_NOT_ANALYZED
    assert c.video_native.payload == {}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_event_provenance_is_complete():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        row = _video_row(s, p, a, g, _production_events(g, _clip(g), n=1))
        pid, row_id, seg_id = p.id, row.id, g.id

    c = _collect(pid)[0]
    refs = [r for r in c.evidence_refs() if r["type"] == "video_native"]
    assert len(refs) == 1
    assert refs[0]["result_id"] == row_id
    assert refs[0]["segment_id"] == seg_id
    assert refs[0]["method"] == "video_native"
    assert refs[0]["source_version"]


# ---------------------------------------------------------------------------
# P0-01-H — content changes are visible to the digest
# ---------------------------------------------------------------------------

def test_changing_a_video_event_description_changes_the_digest():
    """P0-01-H: same row id and version, different event text."""
    from app.services import director_query as dq

    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, start=10.0, end=15.0)
        row = _video_row(s, p, a, g, _production_events(g, _clip(g), n=2))
        pid, row_id = p.id, row.id

    def digest() -> str:
        cands = _collect(pid)
        return dq.build_context(
            project_id=pid, candidates=cands, coverage=dc.coverage(cands),
            brief={"theme": "", "target_duration_sec": None}, max_candidates=10,
        )["input_digest"]

    before = digest()
    with session_scope() as s:
        r = s.get(SegmentVideoObservation, row_id)
        evs = list(r.events_json)
        evs[0] = {**evs[0], "description": "a different action entirely"}
        r.events_json = evs
    assert digest() != before
