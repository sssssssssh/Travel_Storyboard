"""Timeline export (CSV / JSON / OTIO), now reading a storyboard *version*.

TSB-366 moved the authoritative plan into ``StoryboardVersion``. These exporters
therefore no longer read ``EditPlanShot`` at all: each one goes through
``storyboard_service.load_export_context``, which pins a single ``version_id``
and resolves its shots. That is what makes the audit's "the persisted plan is the
single source of truth" true after versioning — JSON, CSV, OTIO and the roughcut
all read the *same version*, so they cannot disagree (audit P1-05).

Ordering rule (unchanged): **array position wins**. A snapshot's shot order is
the timeline order, and ``timeline_start_sec`` is accumulated in that order.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..database import begin_write, session_scope
from ..models import Asset, EditPlanShot, Segment

#: A shot may be shortened for pacing, but not lengthened beyond its source
#: span unless a speed change is introduced (not supported yet).
DURATION_TOLERANCE = 1e-3
MIN_SHOT_SEC = 0.05


def _finite(value) -> float | None:
    """Parse a number, rejecting NaN/inf and non-numerics."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_edit_plan(
    project_id: str,
    shots: list[dict],
    *,
    base_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    """Compatibility adapter for callers of the pre-TSB-366 write path.

    The legacy function replaced every ``EditPlanShot`` row with no version
    awareness — the exact bypass §5.1 forbids. It is kept as a *thin* adapter so
    existing internal callers (and the acceptance tool) keep a single entry
    point, but it now:

    * requires ``base_version_id`` (a missing one raises
      :class:`BaseVersionRequired`, surfaced as ``428`` by the API);
    * appends a storyboard version instead of deleting rows;
    * returns the historical response keys (``created``/``errors``/
      ``total_duration_sec``) plus the new ``version_id``.

    Validation happens through the snapshot service, so an invalid plan writes
    nothing at all rather than a partially-replaced table.
    """
    from .storyboard_service import save_version

    view = save_version(
        project_id=project_id,
        base_version_id=base_version_id,
        shots=shots,
        actor="user",
        reason="manual_edit",
        idempotency_key=idempotency_key,
    )
    timeline = sum(float(s.get("target_duration_sec") or 0.0) for s in view.shots)
    return {
        "created": len(view.shots),
        "errors": [],
        "total_duration_sec": round(timeline, 3),
        "board_id": view.board_id,
        "version_id": view.version_id,
        "version_no": view.version_no,
        "validation_summary": view.validation_summary,
    }


def _shots_for_export(project_id: str, version_id: str | None = None):
    """Resolve one version's shots into ``(shot_dict, asset, segment)`` triples.

    A single call per export guarantees every row comes from one version (§5.3).
    """
    from .storyboard_service import get_current_version, load_export_context

    if version_id is None:
        # Resolve first so a project with no board gets an (empty) version 1
        # rather than a 404 from the export path.
        version_id = get_current_version(project_id=project_id).version_id
    ctx = load_export_context(project_id=project_id, version_id=version_id)
    rows = []
    for shot in ctx.shots:
        asset = ctx.assets.get(str(shot.get("asset_id") or ""))
        segment = ctx.segments.get(str(shot.get("segment_id") or ""))
        rows.append((shot, asset, segment))
    return ctx.view, rows


def _as_shot_view(shot: dict) -> dict:
    """The legacy field names an exporter writes, read from the snapshot.

    Kept as an explicit projection so a snapshot field rename cannot silently
    change an export column.
    """
    return {
        "order": shot.get("order", 0),
        "asset_id": shot.get("asset_id", ""),
        "segment_id": shot.get("segment_id", ""),
        "source_in_sec": float(shot.get("source_in_sec") or 0.0),
        "source_out_sec": float(shot.get("source_out_sec") or 0.0),
        "target_duration_sec": float(shot.get("target_duration_sec") or 0.0),
        "timeline_start_sec": float(shot.get("timeline_start_sec") or 0.0),
        "story_role": shot.get("story_role", ""),
        "transition_hint": shot.get("transition_hint", ""),
        "music_cue": shot.get("music_cue", ""),
        "audio_hint": shot.get("audio_hint", ""),
        "color_hint": shot.get("color_hint", ""),
        "evidence": shot.get("evidence", ""),
        "confidence": float(shot.get("confidence") or 0.0),
        "review_status": shot.get("review_status", "pending"),
        "shot_id": shot.get("shot_id", ""),
    }


def export_json(project_id: str, *, version_id: str | None = None) -> dict:
    """The plan document, rendered from exactly one version (§5.3)."""
    view, rows = _shots_for_export(project_id, version_id)
    shots = []
    for index, (shot, asset, _segment) in enumerate(rows):
        item = _as_shot_view(shot)
        item["order"] = index       # array position is the timeline order
        item["source_name"] = asset.display_name if asset else ""
        shots.append(item)
    total = sum(s["target_duration_sec"] for s in shots)
    return {
        "schema_version": "2.0",
        "project_id": project_id,
        # TSB-366: which board/version this document came from.
        "board_id": view.board_id,
        "version_id": view.version_id,
        "version_no": view.version_no,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_duration_sec": round(total, 3),
        "shots": shots,
    }


def export_csv(project_id: str, *, version_id: str | None = None) -> str:
    _view, rows = _shots_for_export(project_id, version_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "序号", "素材名", "入点秒", "出点秒", "目标时长", "时间线起点",
            "用途", "转场", "音乐cue", "音频建议", "调色建议", "证据", "置信度", "复核状态",
        ]
    )
    for index, (raw, asset, _seg) in enumerate(rows):
        sh = _as_shot_view(raw)
        writer.writerow(
            [
                index,
                asset.display_name if asset else "",
                f"{sh['source_in_sec']:.3f}",
                f"{sh['source_out_sec']:.3f}",
                f"{sh['target_duration_sec']:.3f}",
                f"{sh['timeline_start_sec']:.3f}",
                sh["story_role"],
                sh["transition_hint"],
                sh["music_cue"],
                sh["audio_hint"],
                sh["color_hint"],
                sh["evidence"],
                f"{sh['confidence']:.2f}",
                sh["review_status"],
            ]
        )
    return buf.getvalue()


def _fps_rate(asset: Asset | None) -> float:
    if asset and asset.fps_den:
        return round(asset.fps_num / asset.fps_den, 6)
    return 24.0


def export_otio(project_id: str, *, version_id: str | None = None) -> str:
    """OpenTimelineIO JSON for the saved plan.

    Validated to round-trip through ``opentimelineio`` (see
    ``tests/test_timeline_export.py``): the timeline, track, clip count, source
    ranges and total duration all survive a read-back, so an NLE that supports
    OTIO should open it. Real-NLE compatibility still needs a manual check —
    see docs/REGRESSION.md.
    """
    _view, rows = _shots_for_export(project_id, version_id)
    rate = 24.0
    clips = []
    for index, (raw, asset, _seg) in enumerate(rows):
        sh = _as_shot_view(raw)
        src_rate = _fps_rate(asset)
        src_in = round(sh["source_in_sec"] * src_rate)
        # The *planned* duration wins when it is shorter than the source span
        # (a shot trimmed for pacing), so the exported timeline matches the
        # saved plan rather than the raw selection.
        span = sh["source_out_sec"] - sh["source_in_sec"]
        planned = sh["target_duration_sec"]
        effective = min(planned, span) if planned > 0 else span
        src_dur = max(1, round(effective * src_rate))
        clips.append(
            {
                "OTIO_SCHEMA": "Clip.1",
                "name": f"shot_{index:03d}",
                "source_range": {
                    "OTIO_SCHEMA": "TimeRange.1",
                    "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": src_rate, "value": src_in},
                    "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": src_rate, "value": src_dur},
                },
                "media_reference": {
                    "OTIO_SCHEMA": "ExternalReference.1",
                    "target_url": asset.source_path if asset else "",
                    "available_range": {
                        "OTIO_SCHEMA": "TimeRange.1",
                        "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": src_rate, "value": 0},
                        "duration": {
                            "OTIO_SCHEMA": "RationalTime.1",
                            "rate": src_rate,
                            "value": round((asset.duration_sec if asset else 0) * src_rate),
                        },
                    },
                },
            }
        )

    otio = {
        "OTIO_SCHEMA": "Timeline.1",
        "name": "Travel Storyboard Edit",
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": 0},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": "V1",
                    "kind": "Video",
                    "children": clips,
                }
            ],
        },
    }
    return json.dumps(otio, ensure_ascii=False, indent=2)


def export_otio_document(project_id: str, *, version_id: str | None = None) -> dict:
    """The OTIO document as a dict (used by tests and the JSON export)."""
    return json.loads(export_otio(project_id, version_id=version_id))
