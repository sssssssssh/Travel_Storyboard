"""M4 — edit plan persistence and timeline export (CSV / JSON / OTIO)."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone

from sqlalchemy import delete, select

from ..database import session_scope
from ..models import Asset, EditPlanShot, Segment


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


def build_edit_plan(project_id: str, shots: list[dict]) -> dict:
    """Validate and replace the project's edit plan (all-or-nothing)."""
    with session_scope() as s:
        assets = {
            a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()
        }
        segments = {
            g.id: g for g in s.scalars(select(Segment).where(Segment.project_id == project_id)).all()
        }

        errors: list[dict] = []
        parsed: list[tuple[dict, Asset]] = []
        for i, sh in enumerate(shots):
            if not isinstance(sh, dict):
                errors.append({"index": i, "message": "shot must be an object"})
                continue
            problems: list[str] = []
            asset = assets.get(sh.get("asset_id", ""))
            seg = segments.get(sh.get("segment_id", ""))
            if not asset:
                problems.append("asset_id not found")
            if not seg:
                problems.append("segment_id not found")
            try:
                sin = float(sh.get("source_in_sec"))
                sout = float(sh.get("source_out_sec"))
            except (TypeError, ValueError):
                sin = sout = 0.0
                problems.append("invalid source_in_sec/source_out_sec")
            else:
                if sin >= sout:
                    problems.append("source_in_sec must be < source_out_sec")
                if asset and (sin < 0 or sout > asset.duration_sec + 1e-3):
                    problems.append("time range exceeds source duration")
            if problems:
                errors.append({"index": i, "message": "; ".join(problems)})
                continue
            parsed.append((sh, asset))

        if errors:
            return {"created": 0, "errors": errors}

        s.execute(delete(EditPlanShot).where(EditPlanShot.project_id == project_id))
        timeline = 0.0
        for sh, asset in parsed:
            dur = float(sh.get("target_duration_sec") or 0.0)
            if dur <= 0:
                dur = float(sh["source_out_sec"]) - float(sh["source_in_sec"])
            s.add(
                EditPlanShot(
                    project_id=project_id,
                    order=int(sh.get("order", len(parsed))),
                    asset_id=sh["asset_id"],
                    segment_id=sh["segment_id"],
                    source_in_sec=float(sh["source_in_sec"]),
                    source_out_sec=float(sh["source_out_sec"]),
                    target_duration_sec=dur,
                    timeline_start_sec=timeline,
                    story_role=sh.get("story_role", ""),
                    transition_hint=sh.get("transition_hint", ""),
                    music_cue=sh.get("music_cue", ""),
                    audio_hint=sh.get("audio_hint", ""),
                    color_hint=sh.get("color_hint", ""),
                    evidence=sh.get("evidence", ""),
                    confidence=float(sh.get("confidence", 0.0) or 0.0),
                    review_status=sh.get("review_status", "pending"),
                )
            )
            timeline += dur

        return {"created": len(parsed), "errors": [], "total_duration_sec": round(timeline, 3)}


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
    """Best-effort OpenTimelineIO JSON (experimental — validate in target NLE)."""
    rows = _load_plan(project_id)
    rate = 24.0
    clips = []
    timeline = 0.0
    for sh, asset, _seg in rows:
        src_rate = _fps_rate(asset)
        src_in = round(sh.source_in_sec * src_rate)
        src_dur = round((sh.source_out_sec - sh.source_in_sec) * src_rate)
        clip_dur = round(sh.target_duration_sec * rate)
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
        timeline += sh.target_duration_sec

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
