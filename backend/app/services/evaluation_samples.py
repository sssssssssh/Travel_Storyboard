"""TSB-371-A — which shots a human is asked to look at.

The point of the review is to check whether a model's description matches the
picture. That requires a small, *representative-but-honest* set, and it requires
the set to be reproducible: if the samples changed silently after someone started
rating, the ratings would refer to different evidence than the report claims.

Rules encoded here:

* Only **persisted readings** produce rating items. A shot with no reading is
  offered as a *control* (useful for spotting false recognition) but never enters
  the accuracy denominator.
* Evidence state is carried verbatim. ``freshness_unverified`` is never reported
  as ``current``.
* A segment with two channels yields **two items** (one per reading) but counts
  once toward "distinct shots" — otherwise a shot analysed twice would look like
  two independent samples.
* Exclusion and review status are surfaced, not filtered silently.
* Sampling is deterministic for a given seed.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Asset,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
    UserAnnotation,
)
from . import director_candidates as dc

#: Bumped when the selection rule changes; stored with every session so an old
#: report can explain which rule produced its samples.
SAMPLING_RULE_VERSION = "1.0"

DEFAULT_LIMIT = 5
MAX_LIMIT = 10

#: Recommended mix: stills carry the most verifiable detail, motion adds a temporal
#: claim. Video is included only if it exists (usually it does not).
RECOMMENDED_MIX = {"static": 3, "multiframe": 2, "video_native": 0}


@dataclass
class SampleItem:
    item_id: str
    segment_id: str
    asset_id: str
    channel: str
    result_id: str
    evidence_state: str
    source_window: dict[str, float]
    source_available: bool
    source_status: str
    eligibility: str
    eligible_for_planning: bool
    model: dict[str, Any]
    frame_ids: list[str] = field(default_factory=list)
    readings: dict[str, Any] = field(default_factory=dict)


def _result_state(row, segment: Segment, *, channel: str) -> str:
    """The honest state of one reading, never upgraded."""
    if row is None:
        return dc.STATE_NOT_ANALYZED
    if getattr(row, "status", "") != "succeeded":
        return dc.STATE_FAILED
    if channel == "video_native":
        if dc._window_changed(segment, row.segment_start_sec, row.segment_end_sec):
            return dc.STATE_STALE
    # The source signature cannot be recomputed for a still without re-analysing.
    return dc.STATE_UNVERIFIED


def _source_status(asset: Asset | None) -> tuple[bool, str]:
    if asset is None:
        return False, "SOURCE_UNAVAILABLE"
    from ..ffmpeg import safe_is_file

    try:
        if not safe_is_file(asset.source_path):
            return False, "SOURCE_UNAVAILABLE"
    except OSError:
        return False, "SOURCE_UNAVAILABLE"
    return True, "AVAILABLE"


def _frame_ids(row) -> list[str]:
    """Representative frames recorded with the reading (may be empty)."""
    evidence = getattr(row, "evidence_json", None) or {}
    ids = evidence.get("frame_ids") if isinstance(evidence, dict) else None
    return [str(x) for x in (ids or [])][:6]


def _stable_item_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_project_rows(session: Session, project_id: str):
    segments = {
        g.id: g for g in session.scalars(
            select(Segment).where(Segment.project_id == project_id)).all()
    }
    assets = {
        a.id: a for a in session.scalars(
            select(Asset).where(Asset.project_id == project_id)).all()
    }
    annotations = {
        a.segment_id: a for a in session.scalars(
            select(UserAnnotation).where(
                UserAnnotation.segment_id.in_(list(segments)))).all()
    } if segments else {}
    return segments, assets, annotations


def _current_by_segment(session: Session, model, project_id: str) -> dict[str, list]:
    """One batched query per channel — never one per segment (E16)."""
    rows = session.scalars(
        select(model).where(model.project_id == project_id).order_by(
            model.segment_id, model.created_at.desc(), model.id.desc())
    ).all()
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row.segment_id, []).append(row)
    return out


def collect_samples(
    session: Session, project_id: str, *, seed: str = "default",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return the rating items, the no-evidence controls, and a summary."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    segments, assets, annotations = _load_project_rows(session, project_id)

    static_rows = _current_by_segment(session, SegmentSemanticResult, project_id)
    motion_rows = _current_by_segment(session, SegmentMotionReview, project_id)
    video_rows = _current_by_segment(session, SegmentVideoObservation, project_id)

    candidates: list[SampleItem] = []
    for seg_id, seg in segments.items():
        asset = assets.get(seg.asset_id)
        available, status = _source_status(asset)
        rating, _src = dc.normalized_rating(seg, annotations.get(seg_id))
        eligibility, _reasons = dc._eligibility(seg, rating)
        eligible_for_planning = eligibility == dc.ELIGIBLE

        for channel, rows in (
            ("static", static_rows.get(seg_id, [])),
            ("multiframe", motion_rows.get(seg_id, [])),
            ("video_native", video_rows.get(seg_id, [])),
        ):
            row = next((r for r in rows if r.is_current), None)
            if row is None:
                continue
            state = _result_state(row, seg, channel=channel)
            if state in (dc.STATE_NOT_ANALYZED,):
                continue
            candidates.append(SampleItem(
                item_id=_stable_item_id(project_id, seg_id, channel, row.id),
                segment_id=seg_id,
                asset_id=seg.asset_id,
                channel=channel,
                result_id=row.id,
                evidence_state=state,
                source_window={
                    "start_abs_sec": float(seg.start_sec),
                    "end_abs_sec": float(seg.end_sec),
                },
                source_available=available,
                source_status=status,
                eligibility=eligibility,
                eligible_for_planning=eligible_for_planning,
                model={
                    "provider": getattr(row, "model_provider", ""),
                    "id": getattr(row, "model_id", ""),
                    "prompt_version": getattr(row, "prompt_version", ""),
                },
                # The still a reviewer looks at. Frames, not the result id: the
                # image route is keyed by frame.
                frame_ids=_frame_ids(row),
            ))

    # Deterministic ordering, then a seeded shuffle that still prefers the mix.
    candidates.sort(key=lambda i: (i.channel, i.segment_id, i.item_id))
    rng = random.Random(f"{seed}:{SAMPLING_RULE_VERSION}")

    by_channel: dict[str, list[SampleItem]] = {}
    for item in candidates:
        by_channel.setdefault(item.channel, []).append(item)
    for items in by_channel.values():
        rng.shuffle(items)

    chosen: list[SampleItem] = []
    for channel, want in RECOMMENDED_MIX.items():
        for item in by_channel.get(channel, [])[:want]:
            if len(chosen) < limit:
                chosen.append(item)
    # Top up from whatever is left, in a stable order.
    if len(chosen) < limit:
        leftover = [i for i in candidates if i not in chosen]
        rng.shuffle(leftover)
        chosen.extend(leftover[: limit - len(chosen)])

    chosen.sort(key=lambda i: (i.channel, i.segment_id))

    analysed_segments = {i.segment_id for i in candidates}
    controls = [
        {
            "item_id": _stable_item_id(project_id, seg_id, "control"),
            "segment_id": seg_id,
            "asset_id": seg.asset_id,
            "channel": "none",
            "result_id": None,
            "evidence_state": dc.STATE_NOT_ANALYZED,
            "source_window": {"start_abs_sec": float(seg.start_sec),
                              "end_abs_sec": float(seg.end_sec)},
            "source_available": _source_status(assets.get(seg.asset_id))[0],
            "source_status": _source_status(assets.get(seg.asset_id))[1],
        }
        for seg_id, seg in sorted(segments.items())
        if seg_id not in analysed_segments
    ][:2]

    channel_counts: dict[str, int] = {}
    refresh_unverified = 0
    for item in chosen:
        channel_counts[item.channel] = channel_counts.get(item.channel, 0) + 1
        if item.evidence_state == dc.STATE_UNVERIFIED:
            refresh_unverified += 1

    return {
        "project_id": project_id,
        "sampling_seed": seed,
        "sampling_rule_version": SAMPLING_RULE_VERSION,
        "limit": limit,
        "items": [
            {
                "item_id": i.item_id,
                "segment_id": i.segment_id,
                "asset_id": i.asset_id,
                "channel": i.channel,
                "result_id": i.result_id,
                "evidence_state": i.evidence_state,
                "source_window": i.source_window,
                "source_available": i.source_available,
                "source_status": i.source_status,
                "eligibility": i.eligibility,
                "eligible_for_planning": i.eligible_for_planning,
                "model": i.model,
                "frame_ids": i.frame_ids,
            }
            for i in chosen
        ],
        "controls": controls,
        "summary": {
            "library_segments": len(segments),
            "analysed_segments": len(analysed_segments),
            "distinct_segments": len({i.segment_id for i in chosen}),
            "distinct_assets": len({i.asset_id for i in chosen}),
            "channel_counts": channel_counts,
            "source_unavailable": sum(1 for i in chosen if not i.source_available),
            "freshness_unverified": refresh_unverified,
            "item_count": len(chosen),
        },
        "limitations": [
            "small_sample_not_population_estimate",
            "source_signature_not_recomputable_without_reanalysis",
        ],
    }
