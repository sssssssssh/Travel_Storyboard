"""M4 — edit plan persistence and timeline export (CSV / JSON / OTIO).

The persisted plan is the single source of truth: JSON, CSV, OTIO and the rough
cut are all derived from the same ordered rows, so they cannot disagree
(audit P1-05).

Ordering rule: **array position wins**. The client's ``order`` field is ignored
apart from being echoed back for diagnostics — renumbering from array position
is what makes ``timeline_start_sec`` contiguous and prevents the "sorted by
order but accumulated by input order" mismatch the audit found.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..database import session_scope
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


def _shot_dict(shot: EditPlanShot, asset: Asset | None, segment: Segment | None) -> dict:
    return {
        "order": shot.order,
        "asset_id": shot.asset_id,
        "segment_id": shot.segment_id,
        "source_in_sec": shot.source_in_sec,
        "source_out_sec": shot.source_out_sec,
        "target_duration_sec": shot.target_duration_sec,
        "timeline_start_sec": shot.timeline_start_sec,
        "story_role": shot.story_role,
        "transition_hint": shot.transition_hint,
        "music_cue": shot.music_cue,
        "audio_hint": shot.audio_hint,
        "color_hint": shot.color_hint,
        "evidence": shot.evidence,
        "confidence": shot.confidence,
        "review_status": shot.review_status,
        "source_name": asset.display_name if asset else "",
    }


def _validate_shot(
    index: int,
    raw: dict,
    assets: dict[str, Asset],
    segments: dict[str, Segment],
) -> tuple[dict | None, list[str]]:
    """Validate one incoming shot. Returns ``(clean, problems)``.

    Everything the audit listed is checked here: unknown ids, a segment that
    belongs to a *different* asset, non-finite numbers, inverted or empty
    ranges, ranges outside the source, and a target duration inconsistent with
    the source span.
    """
    problems: list[str] = []

    asset_id = str(raw.get("asset_id") or "")
    segment_id = str(raw.get("segment_id") or "")
    asset = assets.get(asset_id)
    segment = segments.get(segment_id)

    if asset is None:
        problems.append("asset_id not found in this project")
    if segment is None:
        problems.append("segment_id not found in this project")
    elif segment.status == "needs_review":
        problems.append("segment needs review after source/detection changes; choose a current segment")

    # The cross-reference the audit flagged as missing: a shot must not pair one
    # video's metadata with another video's timecodes.
    if asset is not None and segment is not None and segment.asset_id != asset.id:
        problems.append("segment_id does not belong to asset_id")
        asset = None  # never emit a shot with an inconsistent pair

    sin = _finite(raw.get("source_in_sec"))
    sout = _finite(raw.get("source_out_sec"))
    if sin is None or sout is None:
        problems.append("source_in_sec/source_out_sec must be finite numbers")
    else:
        if sin < 0:
            problems.append("source_in_sec must be >= 0")
        if sout - sin < MIN_SHOT_SEC:
            problems.append("source_out_sec must exceed source_in_sec")
        if asset is not None and sout > asset.duration_sec + DURATION_TOLERANCE:
            problems.append("time range exceeds source duration")
        # Stay inside the annotated segment when one is known.
        if segment is not None and sin < min(segment.start_sec, segment.end_sec) - DURATION_TOLERANCE:
            problems.append("source_in_sec is before the segment start")
        if segment is not None and sout > segment.end_sec + DURATION_TOLERANCE:
            problems.append("source_out_sec is after the segment end")

    dur_raw = raw.get("target_duration_sec")
    dur = _finite(dur_raw) if dur_raw not in (None, "") else None
    if dur_raw not in (None, "") and dur is None:
        problems.append("target_duration_sec must be a finite number")
    if dur is not None:
        if dur < MIN_SHOT_SEC:
            problems.append("target_duration_sec must be positive")
        elif sin is not None and sout is not None and dur > (sout - sin) + DURATION_TOLERANCE:
            # Without a speed field, a shot cannot be longer than its source.
            problems.append("target_duration_sec exceeds the source range length")

    conf_raw = raw.get("confidence")
    conf = _finite(conf_raw) if conf_raw not in (None, "") else 0.0
    if conf is None:
        problems.append("confidence must be a finite number")
        conf = 0.0
    elif not 0.0 <= conf <= 1.0:
        problems.append("confidence must be between 0 and 1")

    if problems:
        return None, problems

    assert sin is not None and sout is not None  # narrowed above
    return (
        {
            "asset_id": asset_id,
            "segment_id": segment_id,
            "source_in_sec": sin,
            "source_out_sec": sout,
            "target_duration_sec": dur if dur is not None else (sout - sin),
            "story_role": str(raw.get("story_role") or "")[:32],
            "transition_hint": str(raw.get("transition_hint") or "")[:64],
            "music_cue": str(raw.get("music_cue") or "")[:128],
            "audio_hint": str(raw.get("audio_hint") or "")[:128],
            "color_hint": str(raw.get("color_hint") or "")[:128],
            "evidence": str(raw.get("evidence") or "")[:32],
            "confidence": conf,
            "review_status": str(raw.get("review_status") or "pending")[:32],
            "requested_order": raw.get("order"),
        },
        [],
    )


def build_edit_plan(project_id: str, shots: list[dict]) -> dict:
    """Validate and replace the project's edit plan (all-or-nothing).

    On success the plan is renumbered from array position and given a
    contiguous timeline; the response reports any ``order`` values that were
    overridden so the caller can explain the difference.
    """
    with session_scope() as s:
        assets = {
            a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()
        }
        segments = {
            g.id: g for g in s.scalars(select(Segment).where(Segment.project_id == project_id)).all()
        }

        errors: list[dict] = []
        parsed: list[dict] = []
        for i, sh in enumerate(shots):
            if not isinstance(sh, dict):
                errors.append({"index": i, "message": "shot must be an object"})
                continue
            clean, problems = _validate_shot(i, sh, assets, segments)
            if problems or clean is None:
                errors.append({"index": i, "message": "; ".join(problems)})
                continue
            parsed.append(clean)

        if errors:
            return {"created": 0, "errors": errors, "total_duration_sec": 0.0}

        # Array order is authoritative; renumber and re-accumulate together.
        reordered = [
            i
            for i, sh in enumerate(parsed)
            if sh.get("requested_order") not in (None, i)
        ]

        s.execute(delete(EditPlanShot).where(EditPlanShot.project_id == project_id))
        timeline = 0.0
        for index, sh in enumerate(parsed):
            s.add(
                EditPlanShot(
                    project_id=project_id,
                    order=index,
                    asset_id=sh["asset_id"],
                    segment_id=sh["segment_id"],
                    source_in_sec=sh["source_in_sec"],
                    source_out_sec=sh["source_out_sec"],
                    target_duration_sec=sh["target_duration_sec"],
                    timeline_start_sec=timeline,
                    story_role=sh["story_role"],
                    transition_hint=sh["transition_hint"],
                    music_cue=sh["music_cue"],
                    audio_hint=sh["audio_hint"],
                    color_hint=sh["color_hint"],
                    evidence=sh["evidence"],
                    confidence=sh["confidence"],
                    review_status=sh["review_status"],
                )
            )
            timeline += sh["target_duration_sec"]

        result = {
            "created": len(parsed),
            "errors": [],
            "total_duration_sec": round(timeline, 3),
        }
        if reordered:
            result["order_adjusted"] = reordered
            result["note"] = (
                "顺序按提交数组的位置重新编号，timeline_start_sec 也据此连续计算。"
            )
        return result


def _load_plan(project_id: str) -> list[tuple[EditPlanShot, Asset | None, Segment | None]]:
    with session_scope() as s:
        shots = s.scalars(
            select(EditPlanShot)
            .where(EditPlanShot.project_id == project_id)
            .order_by(EditPlanShot.order)
        ).all()
        assets = {a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()}
        segments = {
            g.id: g for g in s.scalars(select(Segment).where(Segment.project_id == project_id)).all()
        }
    return [(sh, assets.get(sh.asset_id), segments.get(sh.segment_id)) for sh in shots]


def export_json(project_id: str) -> dict:
    shots = [_shot_dict(sh, a, g) for sh, a, g in _load_plan(project_id)]
    total = sum(s["target_duration_sec"] for s in shots)
    return {
        "schema_version": "2.0",
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_duration_sec": round(total, 3),
        "shots": shots,
    }


def export_csv(project_id: str) -> str:
    rows = _load_plan(project_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "序号", "素材名", "入点秒", "出点秒", "目标时长", "时间线起点",
            "用途", "转场", "音乐cue", "音频建议", "调色建议", "证据", "置信度", "复核状态",
        ]
    )
    for sh, asset, _seg in rows:
        writer.writerow(
            [
                sh.order,
                asset.display_name if asset else "",
                f"{sh.source_in_sec:.3f}",
                f"{sh.source_out_sec:.3f}",
                f"{sh.target_duration_sec:.3f}",
                f"{sh.timeline_start_sec:.3f}",
                sh.story_role,
                sh.transition_hint,
                sh.music_cue,
                sh.audio_hint,
                sh.color_hint,
                sh.evidence,
                f"{sh.confidence:.2f}",
                sh.review_status,
            ]
        )
    return buf.getvalue()


def _fps_rate(asset: Asset | None) -> float:
    if asset and asset.fps_den:
        return round(asset.fps_num / asset.fps_den, 6)
    return 24.0


def export_otio(project_id: str) -> str:
    """OpenTimelineIO JSON for the saved plan.

    Validated to round-trip through ``opentimelineio`` (see
    ``tests/test_timeline_export.py``): the timeline, track, clip count, source
    ranges and total duration all survive a read-back, so an NLE that supports
    OTIO should open it. Real-NLE compatibility still needs a manual check —
    see docs/REGRESSION.md.
    """
    rows = _load_plan(project_id)
    rate = 24.0
    clips = []
    for sh, asset, _seg in rows:
        src_rate = _fps_rate(asset)
        src_in = round(sh.source_in_sec * src_rate)
        # The *planned* duration wins when it is shorter than the source span
        # (a shot trimmed for pacing), so the exported timeline matches the
        # saved plan rather than the raw selection.
        span = sh.source_out_sec - sh.source_in_sec
        effective = min(sh.target_duration_sec, span) if sh.target_duration_sec > 0 else span
        src_dur = max(1, round(effective * src_rate))
        clips.append(
            {
                "OTIO_SCHEMA": "Clip.1",
                "name": f"shot_{sh.order:03d}",
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


def export_otio_document(project_id: str) -> dict:
    """The OTIO document as a dict (used by tests and the JSON export)."""
    return json.loads(export_otio(project_id))
