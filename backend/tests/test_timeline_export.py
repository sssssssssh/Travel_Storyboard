"""Audit P1-05 — edit plan validation, ordering and export consistency.

The acceptance criteria from the audit: shuffled/duplicate ``order``,
cross-asset references, negative or non-finite numbers, and durations longer
than the source must all fail validation; and the JSON/CSV/OTIO exports must
agree on shot count, order and duration.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, EditPlanShot, Segment
from app.services.timeline_export import (
    build_edit_plan,
    export_csv,
    export_json,
    export_otio,
    export_otio_document,
)


@pytest.fixture()
def seeded(client):
    """Two assets, each with a segment; enough to test cross-asset mistakes."""
    res = client.post("/api/projects", json={"name": "plan-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    ids: dict[str, dict] = {}
    for name, duration in (("first.mp4", 30.0), ("second.mp4", 45.0)):
        asset_id = str(uuid.uuid4())
        with session_scope() as s:
            s.add(
                Asset(
                    id=asset_id,
                    project_id=project_id,
                    source_path=f"C:/footage/{name}",
                    display_name=name,
                    duration_sec=duration,
                    fps_num=30,
                    fps_den=1,
                    scan_status="succeeded",
                )
            )
        seg_id = str(uuid.uuid4())
        with session_scope() as s:
            s.add(
                Segment(
                    id=seg_id,
                    project_id=project_id,
                    asset_id=asset_id,
                    detected_start_sec=0.0,
                    detected_end_sec=duration,
                    start_sec=0.0,
                    end_sec=duration,
                    boundary_reason="hard_cut",
                )
            )
        ids[name] = {"asset_id": asset_id, "segment_id": seg_id, "duration": duration}

    return project_id, ids


def _shot(ids: dict, name: str, sin: float, sout: float, **kw) -> dict:
    entry = ids[name]
    return {
        "asset_id": entry["asset_id"],
        "segment_id": entry["segment_id"],
        "source_in_sec": sin,
        "source_out_sec": sout,
        **kw,
    }


# --- validation failures -----------------------------------------------------

def test_valid_plan_is_accepted(seeded):
    project_id, ids = seeded
    result = build_edit_plan(project_id, [_shot(ids, "first.mp4", 1.0, 6.0)])
    assert result["errors"] == []
    assert result["created"] == 1
    assert result["total_duration_sec"] == pytest.approx(5.0)


def test_unknown_ids_are_rejected(seeded):
    project_id, _ = seeded
    result = build_edit_plan(
        project_id,
        [{"asset_id": "nope", "segment_id": "nope", "source_in_sec": 0, "source_out_sec": 5}],
    )
    assert result["created"] == 0
    assert "asset_id not found" in result["errors"][0]["message"]
    assert "segment_id not found" in result["errors"][0]["message"]


def test_cross_asset_reference_is_rejected(seeded):
    """The audit's core point: a segment must belong to the asset in the shot."""
    project_id, ids = seeded
    crossed = {
        "asset_id": ids["first.mp4"]["asset_id"],
        "segment_id": ids["second.mp4"]["segment_id"],  # belongs to the other video
        "source_in_sec": 1.0,
        "source_out_sec": 5.0,
    }
    result = build_edit_plan(project_id, [crossed])
    assert result["created"] == 0
    assert "does not belong" in result["errors"][0]["message"]


@pytest.mark.parametrize(
    "sin,sout,needle",
    [
        (5.0, 2.0, "must exceed"),
        (4.0, 4.0, "must exceed"),
        (-1.0, 3.0, ">= 0"),
        (0.0, 9999.0, "exceeds source duration"),
    ],
)
def test_bad_time_ranges_are_rejected(seeded, sin, sout, needle):
    project_id, ids = seeded
    result = build_edit_plan(project_id, [_shot(ids, "first.mp4", sin, sout)])
    assert result["created"] == 0
    assert needle in result["errors"][0]["message"]


@pytest.mark.parametrize("bad", ["abc", None, float("nan"), float("inf")])
def test_non_finite_times_are_rejected(seeded, bad):
    project_id, ids = seeded
    shot = _shot(ids, "first.mp4", 1.0, 5.0)
    shot["source_out_sec"] = bad
    result = build_edit_plan(project_id, [shot])
    assert result["created"] == 0
    assert result["errors"]


def test_target_longer_than_source_is_rejected(seeded):
    """No speed-change field exists, so stretching a shot is invalid."""
    project_id, ids = seeded
    result = build_edit_plan(
        project_id, [_shot(ids, "first.mp4", 1.0, 4.0, target_duration_sec=30.0)]
    )
    assert result["created"] == 0
    assert "exceeds the source range" in result["errors"][0]["message"]


def test_shorter_target_duration_is_allowed(seeded):
    project_id, ids = seeded
    result = build_edit_plan(
        project_id, [_shot(ids, "first.mp4", 1.0, 9.0, target_duration_sec=3.0)]
    )
    assert result["errors"] == []
    plan = export_json(project_id)
    assert plan["shots"][0]["target_duration_sec"] == pytest.approx(3.0)


def test_confidence_out_of_range_is_rejected(seeded):
    project_id, ids = seeded
    for bad in (-0.5, 1.5):
        result = build_edit_plan(
            project_id, [_shot(ids, "first.mp4", 1.0, 5.0, confidence=bad)]
        )
        assert result["created"] == 0
        assert "confidence" in result["errors"][0]["message"]


def test_negative_target_duration_is_rejected(seeded):
    project_id, ids = seeded
    result = build_edit_plan(
        project_id, [_shot(ids, "first.mp4", 1.0, 5.0, target_duration_sec=-2.0)]
    )
    assert result["created"] == 0


def test_non_object_shot_is_rejected(seeded):
    project_id, _ = seeded
    result = build_edit_plan(project_id, ["not a dict"])
    assert result["created"] == 0
    assert "must be an object" in result["errors"][0]["message"]


def test_validation_is_all_or_nothing(seeded):
    """One bad shot must not partially replace a previously saved plan."""
    project_id, ids = seeded
    good = build_edit_plan(project_id, [_shot(ids, "first.mp4", 1.0, 4.0)])
    assert good["created"] == 1

    bad = build_edit_plan(
        project_id,
        [_shot(ids, "first.mp4", 1.0, 4.0), {"asset_id": "x", "segment_id": "y",
                                             "source_in_sec": 0, "source_out_sec": 1}],
    )
    assert bad["created"] == 0
    # The old plan survives untouched.
    plan = export_json(project_id)
    assert len(plan["shots"]) == 1


# --- ordering / timeline continuity -----------------------------------------

def test_array_order_wins_and_timeline_is_contiguous(seeded):
    """Shuffled `order` values must not desynchronise the timeline (P1-05)."""
    project_id, ids = seeded
    a = _shot(ids, "first.mp4", 0.0, 3.0, order=99)
    b = _shot(ids, "second.mp4", 0.0, 5.0, order=0)
    result = build_edit_plan(project_id, [a, b])

    assert result["errors"] == []
    # Both shots carried an `order` that disagreed with their array position.
    assert result.get("order_adjusted") == [0, 1]
    assert "note" in result

    plan = export_json(project_id)
    orders = [s["order"] for s in plan["shots"]]
    assert orders == [0, 1], "rows must be renumbered from array position"

    starts = [s["timeline_start_sec"] for s in plan["shots"]]
    durations = [s["target_duration_sec"] for s in plan["shots"]]
    assert starts[0] == 0.0
    assert starts[1] == pytest.approx(durations[0]), "timeline must be contiguous"
    assert plan["total_duration_sec"] == pytest.approx(sum(durations))


def test_duplicate_order_values_do_not_break_continuity(seeded):
    project_id, ids = seeded
    shots = [
        _shot(ids, "first.mp4", 0.0, 2.0, order=1),
        _shot(ids, "first.mp4", 5.0, 8.0, order=1),
        _shot(ids, "second.mp4", 0.0, 4.0, order=1),
    ]
    result = build_edit_plan(project_id, shots)
    assert result["errors"] == []
    plan = export_json(project_id)
    assert [s["order"] for s in plan["shots"]] == [0, 1, 2]
    running = 0.0
    for s in plan["shots"]:
        assert s["timeline_start_sec"] == pytest.approx(running)
        running += s["target_duration_sec"]
    assert plan["total_duration_sec"] == pytest.approx(running)


def test_missing_order_field_defaults_to_position(seeded):
    project_id, ids = seeded
    result = build_edit_plan(
        project_id,
        [_shot(ids, "first.mp4", 0.0, 2.0), _shot(ids, "second.mp4", 0.0, 2.0)],
    )
    assert result["errors"] == []
    assert "order_adjusted" not in result


# --- export consistency ------------------------------------------------------

@pytest.fixture()
def saved_plan(seeded):
    project_id, ids = seeded
    build_edit_plan(
        project_id,
        [
            _shot(ids, "first.mp4", 1.0, 6.0, target_duration_sec=4.0, story_role="opening"),
            _shot(ids, "second.mp4", 2.0, 10.0, target_duration_sec=6.0, story_role="climax"),
        ],
    )
    return project_id


def test_json_csv_otio_agree_on_shots_and_duration(saved_plan):
    project_id = saved_plan

    plan = export_json(project_id)
    csv_text = export_csv(project_id)
    otio_doc = export_otio_document(project_id)

    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(rows) == len(plan["shots"]) == 2

    clips = otio_doc["tracks"]["children"][0]["children"]
    assert len(clips) == 2

    # Durations agree between JSON and CSV.
    for shot, row in zip(plan["shots"], rows):
        assert float(row["目标时长"]) == pytest.approx(shot["target_duration_sec"], abs=1e-3)
        assert float(row["时间线起点"]) == pytest.approx(shot["timeline_start_sec"], abs=1e-3)
        assert int(row["序号"]) == shot["order"]

    # And CSV order matches JSON order.
    assert [int(r["序号"]) for r in rows] == [s["order"] for s in plan["shots"]]


def test_csv_includes_source_names(saved_plan):
    rows = list(csv.DictReader(io.StringIO(export_csv(saved_plan))))
    assert [r["素材名"] for r in rows] == ["first.mp4", "second.mp4"]


# --- OTIO round-trip through the real library -------------------------------

def test_otio_round_trips_through_opentimelineio(saved_plan, tmp_path: Path):
    """The exported OTIO must be readable by the reference implementation."""
    otio = pytest.importorskip("opentimelineio")

    path = tmp_path / "plan.otio"
    path.write_text(export_otio(saved_plan), encoding="utf-8")

    timeline = otio.adapters.read_from_file(str(path))
    assert timeline.name
    assert len(timeline.tracks) == 1

    track = timeline.tracks[0]
    assert track.kind == "Video"
    assert len(track) == 2

    plan = export_json(saved_plan)
    for clip, shot in zip(track, plan["shots"]):
        # Source in-point survives the round trip in the source frame rate.
        src_rate = clip.source_range.start_time.rate
        assert clip.source_range.start_time.value == pytest.approx(
            shot["source_in_sec"] * src_rate, abs=1
        )
        assert clip.media_reference.target_url  # non-empty media path

    # Total duration is within a frame of the planned total.
    total = timeline.duration().to_seconds()
    assert total == pytest.approx(plan["total_duration_sec"], abs=0.1)


def test_otio_has_required_schema_fields(saved_plan):
    doc = export_otio_document(saved_plan)
    assert doc["OTIO_SCHEMA"] == "Timeline.1"
    assert doc["tracks"]["OTIO_SCHEMA"] == "Stack.1"
    track = doc["tracks"]["children"][0]
    assert track["OTIO_SCHEMA"] == "Track.1"
    for clip in track["children"]:
        assert clip["OTIO_SCHEMA"] == "Clip.1"
        assert clip["source_range"]["OTIO_SCHEMA"] == "TimeRange.1"
        assert clip["media_reference"]["OTIO_SCHEMA"] == "ExternalReference.1"


def test_otio_clip_duration_uses_planned_length(saved_plan):
    """A shot shortened for pacing must not export its full source span."""
    doc = export_otio_document(saved_plan)
    clips = doc["tracks"]["children"][0]["children"]
    plan = export_json(saved_plan)
    for clip, shot in zip(clips, plan["shots"]):
        rate = clip["source_range"]["duration"]["rate"]
        seconds = clip["source_range"]["duration"]["value"] / rate
        assert seconds == pytest.approx(shot["target_duration_sec"], abs=0.05)


def test_empty_plan_exports_cleanly(seeded):
    project_id, _ = seeded
    plan = export_json(project_id)
    assert plan["shots"] == []
    assert plan["total_duration_sec"] == 0
    assert export_otio_document(project_id)["tracks"]["children"][0]["children"] == []
    # CSV still has its header row.
    assert "序号" in export_csv(project_id)


def test_outpoint_cannot_cross_segment_and_old_plan_survives(seeded):
    pid, ids = seeded
    good = _shot(ids, 'first.mp4', 1, 5)
    assert build_edit_plan(pid, [good])['created'] == 1
    before = export_json(pid)['shots']
    with session_scope() as s:
        s.get(Segment, ids['first.mp4']['segment_id']).end_sec = 8
    bad = _shot(ids, 'first.mp4', 1, 9)
    assert build_edit_plan(pid, [good, bad])['errors']
    assert export_json(pid)['shots'] == before


@pytest.mark.parametrize('duration', [float('nan'), float('inf'), 'bad'])
def test_invalid_target_duration_rejected(seeded, duration):
    pid, ids = seeded
    assert build_edit_plan(pid, [_shot(ids, 'first.mp4', 1, 5, target_duration_sec=duration)])['errors']
