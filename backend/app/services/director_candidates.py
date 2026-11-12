"""TSB-368-A — read-only aggregation of director material candidates.

This module answers one question without touching a model, the filesystem tree,
or the storyboard:

    Which shots exist in this project, what has actually been *observed* about
    them, and what is merely unknown?

Design rules that shaped the code:

* **The candidate universe is every Segment.** Not ``vision-candidates``, not the
  semantic selection queue. A shot with no representative frame and no model run
  still counts, and still appears in coverage — otherwise the numbers would lie
  by omission (C6).
* **The three observation channels stay separate.** A still-frame reading is not
  a video reading, and a multi-frame motion conclusion is not native video
  understanding. They are reported side by side and never merged into one
  "analyzed" flag.
* **Human judgement outranks machine judgement, always.** A low-quality shot the
  user marked ``favorite`` is not demoted by its blur score, and no AI field can
  overwrite a rating.
* **Freshness is verified where it can be, and admitted where it cannot.**
  Native-video observations carry the segment window they were computed for, so
  a comparison is free and exact. Semantic results need a source signature that
  requires a bounded sample read; when that read is impossible the state is
  ``freshness_unverified`` rather than a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Asset,
    Segment,
    SegmentMotionReview,
    SegmentMotionVerdict,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from .review_validation import INVALID_SEGMENT_STATUSES

#: Ratings the UI can set (``app/api/segments.py``). Anything else is preserved
#: verbatim in the DTO but treated as neutral for ranking.
POSITIVE_RATINGS = ("favorite", "like")
NEGATIVE_RATINGS = ("exclude",)

#: ``Segment.quality_metrics`` keys the analyzer actually writes.
QUALITY_KEYS = (
    "blur_avg",
    "blur_min",
    "brightness_avg",
    "overexposed_ratio",
    "underexposed_ratio",
    "frame_count",
    "near_duplicate_segments",
)

#: ``analysis_json["visual"]`` keys produced by the semantic schema.
VISUAL_KEYS = ("scene_tags", "subjects", "visible_actions", "lighting", "composition", "mood")

STATIC = "static"
MULTIFRAME = "multiframe"
VIDEO_NATIVE = "video_native"

# Observation states. Deliberately distinct: "we never looked", "we looked but
# the result is about a different version of the shot", and "we looked and it
# still holds" are three different facts.
STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_UNVERIFIED = "freshness_unverified"
STATE_NOT_ANALYZED = "not_analyzed"
STATE_FAILED = "failed"

ELIGIBLE = "eligible"
NEEDS_REVIEW = "needs_review"
EXCLUDED = "excluded"


@dataclass
class ObservationChannel:
    """One analysis channel's view of a shot."""

    state: str = STATE_NOT_ANALYZED
    result_id: str | None = None
    method: str = ""
    status: str = ""
    source_version: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "result_id": self.result_id,
            "method": self.method,
            "status": self.status,
            "source_version": self.source_version,
            "status_detail": self.payload,
            "limitations": self.limitations,
        }


@dataclass
class Candidate:
    """A normalized, evidence-backed director candidate."""

    segment_id: str
    asset_id: str
    asset_display_name: str
    source_start: float
    source_end: float
    detected_start: float
    detected_end: float
    eligibility: str
    exclusion_reasons: list[str]
    human: dict[str, Any]
    technical: dict[str, Any]
    static: ObservationChannel
    multiframe: ObservationChannel
    video_native: ObservationChannel
    editorial: dict[str, Any]
    limitations: list[str]
    represent: bool = True

    @property
    def duration(self) -> float:
        return max(0.0, self.source_end - self.source_start)

    def evidence_refs(self) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for channel, kind in (
            (self.static, STATIC),
            (self.multiframe, MULTIFRAME),
            (self.video_native, VIDEO_NATIVE),
        ):
            if channel.result_id and channel.state != STATE_NOT_ANALYZED:
                refs.append(
                    {
                        "type": kind,
                        "result_id": channel.result_id,
                        "segment_id": self.segment_id,
                        "method": channel.method,
                        "source_version": channel.source_version,
                        "state": channel.state,
                    }
                )
        return refs

    def as_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "asset_display_name": self.asset_display_name,
            "source_window": {"start_sec": self.source_start, "end_sec": self.source_end},
            "detected_window": {"start_sec": self.detected_start, "end_sec": self.detected_end},
            "duration_sec": round(self.duration, 3),
            "eligibility": self.eligibility,
            "exclusion_reasons": list(self.exclusion_reasons),
            "human": dict(self.human),
            "technical": dict(self.technical),
            "observations": {
                STATIC: self.static.as_dict(),
                MULTIFRAME: self.multiframe.as_dict(),
                VIDEO_NATIVE: self.video_native.as_dict(),
            },
            "editorial_hints": dict(self.editorial),
            "evidence_refs": self.evidence_refs(),
            "limitations": list(self.limitations),
            "is_representative": self.represent,
        }


def _visual_payload(analysis: dict | None) -> dict[str, Any]:
    """Pull the `visual` block out of a semantic result, tolerating both shapes.

    The production writer stores ``validated.visual`` directly
    (``semantic._store_result`` → ``analysis_json=validated.visual``), i.e. the
    row looks like ``{"scene_tags": [...], "subjects": [...]}``. Older rows wrap
    it as ``{"visual": {...}}``. Assuming only one shape silently reported every
    real still-frame reading as empty — the content existed, the reader missed it.
    """
    data = analysis or {}
    nested = data.get("visual")
    if isinstance(nested, dict):
        return nested
    return {k: data[k] for k in VISUAL_KEYS if k in data}


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

def _source_signature_for(session: Session | None, asset: Asset) -> str:
    """Recompute the semantic source signature, or '' when that is impossible.

    ``verdicts.source_identity`` reads BOUNDED samples (three 64 KiB reads), not
    the whole file and not the directory tree. It still touches the filesystem,
    so an offline or unreadable source legitimately yields no answer — and the
    caller must say "unverified" rather than assume "current".
    """
    try:
        from .vision.verdicts import source_identity

        return source_identity(asset) or ""
    except Exception:  # noqa: BLE001 - any failure means "cannot verify"
        return ""


def _freshness_from_signature(
    *, recorded: str, recomputed: str, limitations: list[str]
) -> str:
    if not recorded:
        limitations.append("no_recorded_source_signature")
        return STATE_UNVERIFIED
    if not recomputed:
        limitations.append("source_not_readable_for_verification")
        return STATE_UNVERIFIED
    if recorded != recomputed:
        limitations.append("source_changed_since_analysis")
        return STATE_STALE
    return STATE_CURRENT


def _window_changed(segment: Segment, start: float | None, end: float | None) -> bool:
    """True when a stored analysis window no longer matches the edited window."""
    if start is None or end is None:
        return False
    return abs(start - segment.start_sec) > 1e-6 or abs(end - segment.end_sec) > 1e-6


# ---------------------------------------------------------------------------
# Channel readers
# ---------------------------------------------------------------------------

# _read_static: replaced by batched loaders below


# _read_multiframe: replaced by batched loaders below


# _read_video_native: replaced by batched loaders below



# ---------------------------------------------------------------------------
# Batched channel loading
# ---------------------------------------------------------------------------
#
# One query per channel for the WHOLE project, then grouped in memory. A
# per-segment lookup would be 3 statements per shot — 3000+ round trips on a
# 1000-shot library — which is exactly the N+1 the query-cost tests forbid.

def _load_current(
    session: Session, model, segment_ids: list[str], project_id: str
) -> dict[str, list]:
    if not segment_ids:
        return {}
    rows = session.scalars(
        select(model)
        .where(model.project_id == project_id, model.segment_id.in_(segment_ids))
        .order_by(model.segment_id, model.created_at.desc(), model.id.desc())
    ).all()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.segment_id, []).append(row)
    return grouped


def _static_channel(
    rows: list, segment: Segment, asset: Asset, limitations: list[str],
    *, verify_freshness: bool,
) -> ObservationChannel:
    if not rows:
        limitations.append("no_static_analysis")
        return ObservationChannel(limitations=["static_not_analyzed"])
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("static_results_all_superseded")
        return ObservationChannel(
            state=STATE_STALE, result_id=rows[0].id, method=STATIC,
            status=rows[0].status, source_version=rows[0].source_signature or "",
            limitations=["no_current_static_result"],
        )
    if row.status != "succeeded":
        limitations.append("static_analysis_failed")
        return ObservationChannel(
            state=STATE_FAILED, result_id=row.id, method=STATIC,
            status=row.status, source_version=row.source_signature or "",
            limitations=["static_analysis_failed"],
        )

    channel_limits: list[str] = []
    if verify_freshness:
        recomputed = _source_signature_for(None, asset)
        state = _freshness_from_signature(
            recorded=row.source_signature or "", recomputed=recomputed,
            limitations=channel_limits,
        )
    else:
        state = STATE_UNVERIFIED
        channel_limits.append("freshness_not_checked_for_this_query")
    limitations.extend(channel_limits)

    analysis = row.analysis_json or {}
    visual = _visual_payload(analysis)
    evidence = row.evidence_json or {}
    frame_ids = evidence.get("frame_ids") or []
    payload = {
        "visual": {k: visual.get(k) for k in VISUAL_KEYS if k in visual},
        "evidence_state": evidence.get("state", ""),
        "observed_confidence": evidence.get("observed_confidence"),
        "confidence_calibration": "uncalibrated",
        "frame_count": len(frame_ids),
        "evidence_limitations": list(evidence.get("limitations") or [])[:8],
        "model": {
            "provider": row.model_provider, "id": row.model_id,
            "prompt_version": row.prompt_version, "schema_version": row.schema_version,
        },
    }
    if len(frame_ids) <= 1:
        channel_limits.append("single_frame_cannot_prove_camera_motion")
    return ObservationChannel(
        state=state, result_id=row.id, method=STATIC, status=row.status,
        source_version=row.source_signature or "", payload=payload,
        limitations=channel_limits,
    )


def _multiframe_channel(
    rows: list, segment: Segment, verdict: SegmentMotionVerdict | None, limitations: list[str]
) -> ObservationChannel:
    if not rows:
        limitations.append("no_multiframe_analysis")
        return ObservationChannel(limitations=["multiframe_not_analyzed"])
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("multiframe_results_all_superseded")
        return ObservationChannel(
            state=STATE_STALE, result_id=rows[0].id, method=rows[0].method or MULTIFRAME,
            status=rows[0].status, source_version=rows[0].input_signature or "",
            limitations=["no_current_multiframe_result"],
        )
    if row.status != "succeeded":
        limitations.append("multiframe_analysis_failed")
        return ObservationChannel(
            state=STATE_FAILED, result_id=row.id, method=row.method or MULTIFRAME,
            status=row.status, source_version=row.input_signature or "",
            limitations=["multiframe_analysis_failed"],
        )

    channel_limits: list[str] = []
    if _window_changed(segment, row.start_sec, row.end_sec):
        limitations.append("multiframe_window_differs_from_current_shot")
        state = STATE_STALE
        channel_limits.append("analysis_window_differs_from_current_shot")
    else:
        state = STATE_UNVERIFIED
        channel_limits.append("input_signature_not_recomputable_offline")

    verdict_value = row.human_verdict or ""
    verdict_note = row.human_note or ""
    if verdict is not None:
        verdict_value = verdict.verdict or verdict_value
        verdict_note = verdict.note or verdict_note

    payload = {
        "motion_type": row.motion_type,
        "stability_risk": row.stability_risk,
        "action_summary": row.action_summary,
        "human_verdict": verdict_value,
        "human_note": verdict_note,
        "window": {"start_sec": row.start_sec, "end_sec": row.end_sec},
        "model": {
            "provider": row.model_provider, "id": row.model_id,
            "prompt_version": row.prompt_version,
        },
    }
    return ObservationChannel(
        state=state, result_id=row.id, method=row.method or MULTIFRAME,
        status=row.status, source_version=row.input_signature or "",
        payload=payload, limitations=channel_limits,
    )


def _video_channel(rows: list, segment: Segment, limitations: list[str]) -> ObservationChannel:
    if not rows:
        limitations.append("no_video_native_analysis")
        return ObservationChannel(limitations=["video_native_not_analyzed"])
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("video_native_results_all_superseded")
        return ObservationChannel(
            state=STATE_STALE, result_id=rows[0].id, method=rows[0].method or VIDEO_NATIVE,
            status=rows[0].status, source_version=rows[0].source_signature or "",
            limitations=["no_current_video_native_result"],
        )
    if row.status != "succeeded":
        limitations.append("video_native_analysis_failed")
        return ObservationChannel(
            state=STATE_FAILED, result_id=row.id, method=row.method or VIDEO_NATIVE,
            status=row.status, source_version=row.source_signature or "",
            limitations=["video_native_analysis_failed"],
        )

    channel_limits: list[str] = []
    if _window_changed(segment, row.segment_start_sec, row.segment_end_sec):
        limitations.append("video_native_window_differs_from_current_shot")
        state = STATE_STALE
        channel_limits.append("analysis_window_differs_from_current_shot")
    else:
        state = STATE_UNVERIFIED
        channel_limits.append("source_signature_not_recomputable_without_reanalysis")

    analysis = row.analysis_json or {}
    events_raw = row.events_json if isinstance(row.events_json, list) else []
    events, counters, drop_reasons = _normalize_video_events(events_raw, segment)
    # Surface every drop reason as a limitation as well: a consumer that only
    # reads `limitations` must still learn that events existed and were filtered,
    # rather than concluding the model reported nothing (C6).
    for reason in drop_reasons:
        if reason not in channel_limits:
            channel_limits.append(reason)

    payload = {
        "summary": str(analysis.get("summary") or "")[:600],
        "events": events[:MAX_EVENTS],
        "event_count_total": len(events_raw),
        # "events existed but were filtered" must never read as "the model said
        # nothing" — so the counts and the reasons travel with the payload.
        "event_counters": counters,
        "drop_reasons": drop_reasons,
        "clip": {
            "start_sec": row.clip_start_sec, "end_sec": row.clip_end_sec,
            "duration_sec": row.clip_duration_sec,
        },
        "honesty": {
            "audio_analyzed": bool(row.audio_analyzed),
            "color_unverified": bool(row.color_unverified),
            "observed_temporal_quality": row.observed_temporal_quality,
        },
        "model": {
            "provider": row.model_provider, "id": row.model_id,
            "prompt_version": row.prompt_version, "schema_version": row.schema_version,
        },
    }
    return ObservationChannel(
        state=state, result_id=row.id, method=row.method or VIDEO_NATIVE,
        status=row.status, source_version=row.source_signature or "",
        payload=payload, limitations=channel_limits,
    )


#: Upper bound on events projected into a candidate payload.
MAX_EVENTS = 20

#: Absolute-time keys produced by ``video_schema.TemporalEvent.to_dict()``.
_ABS_START = "start_abs_sec"
_ABS_END = "end_abs_sec"
#: Historical shapes seen in rows written before the production format settled.
_LEGACY_START = ("start_sec", "start")
_LEGACY_END = ("end_sec", "end")
#: Tolerance for float drift when comparing against the shot window.
_EVENT_EPS = 1e-6


def _numeric(value: Any) -> float | None:
    """A finite float, or None. Rejects bools, strings, NaN and Inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _normalize_video_events(
    events_raw: list, segment: Segment
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Map stored video events onto the public, absolute-time shape.

    The production writer serializes ``TemporalEvent``, whose authoritative times
    are ``start_abs_sec`` / ``end_abs_sec`` in **source seconds**, with
    ``start_rel_sec`` / ``end_rel_sec`` measured from the clip start. Reading the
    relative values as absolute would silently shift every event by the clip
    offset, so they are never used as a fallback: if the absolute pair is absent
    the event is dropped and the reason is recorded.

    Legacy rows (``start_sec``/``end_sec`` or ``start``/``end``) are still read so
    historical data does not disappear. When both shapes are present and
    disagree, the production contract wins and the conflict is reported rather
    than quietly resolved in whichever direction happens to be convenient.
    """
    accepted: list[dict[str, Any]] = []
    counters = {"accepted": 0, "dropped": 0, "skipped_non_object": 0}
    reasons: list[str] = []

    def drop(reason: str) -> None:
        counters["dropped"] += 1
        if reason not in reasons:
            reasons.append(reason)

    for ev in events_raw:
        if not isinstance(ev, dict):
            counters["skipped_non_object"] += 1
            continue

        start = _numeric(ev.get(_ABS_START))
        end = _numeric(ev.get(_ABS_END))

        legacy_start = next(
            (v for v in (_numeric(ev.get(k)) for k in _LEGACY_START) if v is not None),
            None,
        )
        legacy_end = next(
            (v for v in (_numeric(ev.get(k)) for k in _LEGACY_END) if v is not None),
            None,
        )

        if start is None or end is None:
            # Distinguish "the field was wrong" from "the field was absent": the
            # first is a data defect worth chasing, the second is an empty value.
            defect = ""
            for key in (_ABS_START, _ABS_END):
                raw = ev.get(key)
                if raw is None:
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    defect = "event_time_not_numeric"
                elif not math.isfinite(float(raw)):
                    defect = "event_time_non_finite"
                if defect:
                    break
            if defect:
                drop(defect)
                continue
            if legacy_start is None or legacy_end is None:
                drop("event_missing_time")
                continue
            # Historical row: its stored values were absolute for those writers.
            start, end = legacy_start, legacy_end
        elif legacy_start is not None and abs(legacy_start - start) > 1e-3:
            # Both shapes present and inconsistent: keep production, report it.
            if "event_time_shape_conflict" not in reasons:
                reasons.append("event_time_shape_conflict")

        assert start is not None and end is not None
        if start >= end:
            drop("event_reversed_window")
            continue
        if start < segment.start_sec - _EVENT_EPS or end > segment.end_sec + _EVENT_EPS:
            drop("event_outside_shot_window")
            continue

        text = ev.get("description") or ev.get("label") or ev.get("type") or ""
        accepted.append({
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "event_type": str(ev.get("event_type") or ev.get("type") or "")[:64],
            "label": str(text)[:200],
            "confidence": ev.get("confidence"),
            # The model's own number, never a calibrated probability.
            "confidence_calibration": "uncalibrated",
            "evidence": [str(x)[:32] for x in (ev.get("evidence") or [])][:6],
            "limitations": [str(x)[:64] for x in (ev.get("limitations") or [])][:6],
        })
        counters["accepted"] += 1

    return accepted, counters, reasons


# ---------------------------------------------------------------------------
# Eligibility & editorial signals
# ---------------------------------------------------------------------------

def normalized_rating(segment: Segment, annotation: Any) -> tuple[str, str]:
    """The single source of truth for a shot's human rating.

    Returns ``(rating, source)``. Precedence, deliberately narrow:

    1. an annotation with an *explicit* rating (``favorite``/``like``/``exclude``)
       wins — it is the newest and most specific human statement;
    2. otherwise the segment column;
    3. otherwise ``pending``.

    The trap this exists to close (TSB-368-R1 P0-01): the app creates an
    annotation row with ``rating="pending"`` as soon as a user merely types a
    note. Treating a non-empty annotation rating as an override let that
    placeholder erase an explicit ``exclude`` on the segment, so an excluded shot
    came back as eligible. A ``pending`` annotation carries no opinion and must
    never outrank a real one.
    """
    seg_rating = (segment.user_rating or "").strip()
    ann_rating = (getattr(annotation, "rating", None) or "").strip()
    if ann_rating and ann_rating != "pending":
        return ann_rating, "annotation"
    if seg_rating:
        return seg_rating, "segment"
    return "pending", "none"


def _eligibility(segment: Segment, rating: str, *,
                 source_retired: bool = False) -> tuple[str, list[str]]:
    """Classify a shot for planning, from the *already normalized* rating.

    Takes the rating rather than re-deriving it: two independent precedence
    rules is exactly how `human.rating` and `eligibility` drifted apart.
    """
    reasons: list[str] = []

    # TSB-371: the user no longer has this file. Distinct from "human_excluded" --
    # nobody judged these shots, the footage simply is not there any more.
    if source_retired:
        reasons.append("source_retired_by_user")
    if rating in NEGATIVE_RATINGS:
        reasons.append("human_excluded")
    if segment.status in INVALID_SEGMENT_STATUSES:
        reasons.append(f"segment_status_{segment.status}")

    start, end = segment.start_sec, segment.end_sec
    try:
        valid_window = start is not None and end is not None and float(start) < float(end)
        finite = abs(float(start)) < 1e12 and abs(float(end)) < 1e12
    except (TypeError, ValueError):
        valid_window, finite = False, False
    if not valid_window or not finite:
        reasons.append("invalid_time_window")

    if ("human_excluded" in reasons or "invalid_time_window" in reasons
            or "source_retired_by_user" in reasons):
        return EXCLUDED, reasons
    if reasons:
        # status-based: still visible, but not offered for automatic planning.
        return NEEDS_REVIEW, reasons
    return ELIGIBLE, reasons


def _human(segment: Segment, annotation: Any) -> dict[str, Any]:
    """Human values with a deterministic, documented precedence.

    An explicit annotation row wins over the segment column (it is the newer,
    more specific record); the segment column wins over the default. Neither is
    ever derived from AI output.
    """
    rating, source = normalized_rating(segment, annotation)
    note = (getattr(annotation, "note", None) or "").strip()
    return {
        "rating": rating,
        "rating_source": source,
        "story_role": (segment.story_role or "").strip(),
        "note": note,
    }


def _technical(segment: Segment) -> dict[str, Any]:
    metrics_in = segment.quality_metrics or {}
    # Only keys the analyzer actually writes; a missing metric stays missing
    # rather than becoming a fabricated zero.
    metrics = {k: metrics_in[k] for k in QUALITY_KEYS if k in metrics_in}
    out: dict[str, Any] = {
        "quality_metrics": metrics,
        "duration_sec": round(max(0.0, (segment.end_sec or 0) - (segment.start_sec or 0)), 3),
        "is_trimmed": bool(segment.is_trimmed),
    }
    tags = segment.content_tags or {}
    if tags:
        out["content_tags"] = tags
    return out


def _editorial(
    human: dict[str, Any],
    static: ObservationChannel,
    multiframe: ObservationChannel,
    video_native: ObservationChannel,
) -> dict[str, Any]:
    """Discrete, explainable preference — never a fabricated score.

    The ordering is a *stated policy*, not a predicted audience response:
    explicit human intent first, then evidence with a known provenance, then
    nothing. There is deliberately no composite number, because nothing in the
    data calibrates one.
    """
    reasons: list[str] = []
    rating = human["rating"]
    if rating in POSITIVE_RATINGS:
        priority = "preferred"
        reasons.append(f"human_rating_{rating}")
    elif rating in NEGATIVE_RATINGS:
        priority = "excluded"
        reasons.append("human_rating_exclude")
    elif human["story_role"]:
        priority = "noted"
        reasons.append("human_story_role_set")
    elif any(c.state == STATE_CURRENT for c in (static, multiframe, video_native)):
        priority = "noted"
        reasons.append("has_verified_observation")
    else:
        priority = "unrated"

    for channel, name in (
        (static, STATIC),
        (multiframe, MULTIFRAME),
        (video_native, VIDEO_NATIVE),
    ):
        if channel.state == STATE_CURRENT:
            reasons.append(f"{name}_observed")
        elif channel.state in (STATE_STALE, STATE_UNVERIFIED):
            reasons.append(f"{name}_{channel.state}")

    return {"priority": priority, "reasons": reasons}


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect_candidates(
    session: Session, project_id: str, *, verify_freshness: bool = True
) -> list[Candidate]:
    """Every Segment of the project as a normalized candidate.

    Cost is bounded: one query for segments, one for their assets, one for
    annotations, one per analysis channel, and one for motion verdicts — all
    restricted to this project, regardless of how many shots there are.

    ``verify_freshness=False`` skips the bounded source read; semantic results
    then report ``freshness_unverified`` instead of ``current``/``stale``.
    """
    segments = session.scalars(
        select(Segment)
        .where(Segment.project_id == project_id)
        .order_by(Segment.asset_id, Segment.start_sec, Segment.id)
    ).all()
    if not segments:
        return []

    seg_ids = [s.id for s in segments]
    asset_ids = {s.asset_id for s in segments}

    assets = {
        a.id: a
        for a in session.scalars(
            select(Asset).where(Asset.project_id == project_id, Asset.id.in_(asset_ids))
        ).all()
    }

    from ..models import UserAnnotation

    annotations = {
        a.segment_id: a
        for a in session.scalars(
            select(UserAnnotation).where(UserAnnotation.segment_id.in_(seg_ids))
        ).all()
    }

    # Three channel queries + one verdict query for the entire project.
    static_rows = _load_current(session, SegmentSemanticResult, seg_ids, project_id)
    motion_rows = _load_current(session, SegmentMotionReview, seg_ids, project_id)
    video_rows = _load_current(session, SegmentVideoObservation, seg_ids, project_id)

    verdicts: dict[str, SegmentMotionVerdict] = {}
    for v in session.scalars(
        select(SegmentMotionVerdict)
        .where(SegmentMotionVerdict.project_id == project_id,
               SegmentMotionVerdict.segment_id.in_(seg_ids))
        .order_by(SegmentMotionVerdict.segment_id, SegmentMotionVerdict.revision.desc())
    ).all():
        verdicts.setdefault(v.segment_id, v)

    out: list[Candidate] = []
    for seg in segments:
        asset = assets.get(seg.asset_id)
        if asset is None:
            # An orphaned segment cannot be attributed to this project's assets.
            # Dropping it would hide a broken library, so it is reported as an
            # explicit exclusion instead (C6).
            out.append(
                Candidate(
                    segment_id=seg.id,
                    asset_id=seg.asset_id,
                    asset_display_name="",
                    source_start=seg.start_sec,
                    source_end=seg.end_sec,
                    detected_start=seg.detected_start_sec,
                    detected_end=seg.detected_end_sec,
                    eligibility=EXCLUDED,
                    exclusion_reasons=["asset_missing_from_project"],
                    human={"rating": "pending", "rating_source": "none",
                           "story_role": "", "note": ""},
                    technical={},
                    static=ObservationChannel(limitations=["asset_missing"]),
                    multiframe=ObservationChannel(limitations=["asset_missing"]),
                    video_native=ObservationChannel(limitations=["asset_missing"]),
                    editorial={"priority": "unrated", "reasons": ["asset_missing"]},
                    limitations=["asset_missing"],
                )
            )
            continue

        limitations: list[str] = []
        annotation = annotations.get(seg.id)
        human = _human(seg, annotation)
        # Classify from the same normalized rating the DTO reports, so the two
        # can never contradict each other.
        eligibility, exclusion_reasons = _eligibility(
            seg, human["rating"],
            source_retired=(getattr(asset, "source_state", "active") == "retired"),
        )

        static = _static_channel(
            static_rows.get(seg.id, []), seg, asset, limitations,
            verify_freshness=verify_freshness,
        )
        multiframe = _multiframe_channel(
            motion_rows.get(seg.id, []), seg, verdicts.get(seg.id), limitations
        )
        video_native = _video_channel(
            video_rows.get(seg.id, []), seg, limitations
        )

        if eligibility == EXCLUDED:
            limitations.extend(exclusion_reasons)
        # A failed channel is a distinct fact from "never analysed": the user
        # needs to know a run happened and produced nothing usable (C6).
        for channel, name in (
            (static, STATIC),
            (multiframe, MULTIFRAME),
            (video_native, VIDEO_NATIVE),
        ):
            if channel.state == STATE_FAILED and f"{name}_analysis_failed" not in limitations:
                limitations.append(f"{name}_analysis_failed")
        if not any(
            c.state in (STATE_CURRENT, STATE_UNVERIFIED)
            for c in (static, multiframe, video_native)
        ):
            limitations.append("no_valid_model_reading")

        out.append(
            Candidate(
                segment_id=seg.id,
                asset_id=seg.asset_id,
                asset_display_name=asset.display_name or "",
                source_start=float(seg.start_sec),
                source_end=float(seg.end_sec),
                detected_start=float(seg.detected_start_sec),
                detected_end=float(seg.detected_end_sec),
                eligibility=eligibility,
                exclusion_reasons=exclusion_reasons,
                human=human,
                technical=_technical(seg),
                static=static,
                multiframe=multiframe,
                video_native=video_native,
                editorial=_editorial(human, static, multiframe, video_native),
                limitations=limitations,
            )
        )
    return out


def coverage(candidates: Iterable[Candidate]) -> dict[str, Any]:
    """Project-wide coverage, independent of any filter or page.

    A shot covered by all three channels counts once in the "any reading" total
    but once per channel in the per-channel totals — adding the channel numbers
    would double-count.
    """
    cands = list(candidates)
    library = len(cands)
    eligible = sum(1 for c in cands if c.eligibility == ELIGIBLE)
    needs_review = sum(1 for c in cands if c.eligibility == NEEDS_REVIEW)
    excluded = sum(1 for c in cands if c.eligibility == EXCLUDED)

    def usable(ch: str) -> int:
        return sum(
            1
            for c in cands
            if getattr(c, ch).state in (STATE_CURRENT, STATE_UNVERIFIED)
            and getattr(c, ch).result_id
        )

    static_valid = usable(STATIC)
    multiframe_valid = usable(MULTIFRAME)
    video_valid = usable(VIDEO_NATIVE)
    any_reading = sum(
        1
        for c in cands
        if any(
            getattr(c, ch).state in (STATE_CURRENT, STATE_UNVERIFIED)
            and getattr(c, ch).result_id
            for ch in (STATIC, MULTIFRAME, VIDEO_NATIVE)
        )
    )
    return {
        "library_segments": library,
        "eligible_segments": eligible,
        "needs_review_segments": needs_review,
        "excluded_segments": excluded,
        "static_valid": static_valid,
        "multiframe_valid": multiframe_valid,
        "video_native_valid": video_valid,
        "segments_with_any_valid_reading": any_reading,
        "no_valid_model_reading": library - any_reading,
        "coverage_complete": library > 0 and any_reading == library,
    }
