"""TSB-371-B — sessions, ratings, and metrics that cannot flatter.

The metric rules come straight from the task book §3.3, and each one exists to
prevent a specific way of overstating the result:

* a denominator of **zero** yields ``None``, never ``100%``;
* ``cannot judge`` (``None``) is *not* a wrong answer — it leaves the denominator;
* a rating whose evidence row was superseded stops counting (``STALE_EVIDENCE``),
  because it describes a model answer that is no longer current;
* a shot whose source is unavailable keeps its rating for audit but is excluded
  from "did the model describe the picture correctly", since nobody could look.

Small samples are reported as small samples. No function here extrapolates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Asset,
    EvaluationItem,
    EvaluationRating,
    EvaluationSession,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from . import evaluation_samples as samples
from .vision.verdicts import source_identity

DIMENSIONS = ("scene", "subject", "action", "timing")
VALID_SCORES = (0, 1, 2)
VALID_HALLUCINATION = ("severe", "mild", "none")
VALID_USABILITY = ("useful", "partly_useful", "useless")


def new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item_key(session_id: str, segment_id: str, channel: str, result_id: str) -> str:
    raw = f"{session_id}|{segment_id}|{channel}|{result_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Creating / resuming a session
# ---------------------------------------------------------------------------

def create_session(
    session: Session, project_id: str, *, seed: str = "default",
    limit: int = samples.DEFAULT_LIMIT, reviewer: str = "",
    kind: str = "footage",
) -> EvaluationSession:
    """Freeze the items a human will be asked about."""
    collected = samples.collect_samples(session, project_id, seed=seed, limit=limit)

    row = EvaluationSession(
        id=new_id(), project_id=project_id, kind=kind, status="IN_PROGRESS",
        sample_seed=seed, sampling_rule_version=collected["sampling_rule_version"],
        reviewer=reviewer, created_at=_now(), updated_at=_now(),
        context_json={
            "summary": collected["summary"],
            "limitations": collected["limitations"],
        },
    )
    session.add(row)
    session.flush()

    for position, item in enumerate(collected["items"]):
        session.add(EvaluationItem(
            id=new_id(), session_id=row.id, project_id=project_id,
            segment_id=item["segment_id"], asset_id=item["asset_id"],
            channel=item["channel"], result_id=item["result_id"] or "",
            source_signature=_current_signature(session, item["asset_id"]),
            evidence_state=item["evidence_state"],
            start_abs_sec=item["source_window"]["start_abs_sec"],
            end_abs_sec=item["source_window"]["end_abs_sec"],
            source_available=item["source_available"],
            item_key=_item_key(row.id, item["segment_id"], item["channel"],
                               item["result_id"] or ""),
            position=position,
            frame_ids=list(item.get("frame_ids") or []),
            readings_json=dict(item.get("readings") or {}),
        ))
    session.flush()
    return row


def _current_signature(session: Session, asset_id: str) -> str:
    asset = session.get(Asset, asset_id)
    if asset is None:
        return ""
    try:
        return source_identity(asset) or ""
    except OSError:
        return ""


def get_session(session: Session, project_id: str, session_id: str
                ) -> EvaluationSession | None:
    row = session.get(EvaluationSession, session_id)
    if row is None or row.project_id != project_id:
        return None
    return row


def _result_is_current(session: Session, item: EvaluationItem) -> bool:
    """Has the evidence this item was frozen against changed underneath it?"""
    if item.channel == "static":
        row = session.get(SegmentSemanticResult, item.result_id)
    elif item.channel == "multiframe":
        row = session.get(SegmentMotionReview, item.result_id)
    elif item.channel == "video_native":
        row = session.get(SegmentVideoObservation, item.result_id)
    else:
        return True
    if row is None:
        return False
    return bool(getattr(row, "is_current", False))


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

class RatingError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def _validate_score(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RatingError("EVAL_RATING_INVALID", f"{name} must be 0, 1, 2 or null", 422)
    if value not in VALID_SCORES:
        raise RatingError("EVAL_RATING_INVALID", f"{name} must be 0, 1, 2 or null", 422)
    return value


def save_rating(
    session: Session, project_id: str, session_id: str, item_id: str,
    payload: dict, *, reviewer: str = "",
) -> EvaluationRating:
    row = get_session(session, project_id, session_id)
    if row is None:
        raise RatingError("EVAL_SESSION_NOT_FOUND", "no such session", 404)
    item = session.get(EvaluationItem, item_id)
    if item is None or item.session_id != session_id or item.project_id != project_id:
        raise RatingError("EVAL_ITEM_NOT_FOUND", "no such item in this session", 404)

    if not item.source_available:
        # Nobody could have watched this shot, so the judgement cannot be genuine.
        # Refusing it keeps the metric free of opinions about pictures the reviewer
        # never saw.
        raise RatingError(
            "EVAL_ITEM_SOURCE_UNAVAILABLE",
            "the source file for this item is not available; it cannot be rated", 409)

    existing = session.scalars(
        select(EvaluationRating).where(EvaluationRating.item_id == item_id)
    ).first()

    expected = payload.get("expected_version")
    if expected is not None and existing is not None:
        if int(existing.version) != int(expected):
            raise RatingError(
                "EVAL_RATING_CONFLICT",
                "the rating changed since this page loaded the value", 409)

    scores = {d: _validate_score(d, payload.get(d)) for d in DIMENSIONS}

    hallucination = payload.get("hallucination")
    if hallucination is not None and hallucination not in VALID_HALLUCINATION:
        raise RatingError("EVAL_RATING_INVALID",
                          f"hallucination must be one of {VALID_HALLUCINATION}", 422)
    usability = payload.get("usability")
    if usability is not None and usability not in VALID_USABILITY:
        raise RatingError("EVAL_RATING_INVALID",
                          f"usability must be one of {VALID_USABILITY}", 422)

    claim_verdicts = payload.get("claim_verdicts")
    if claim_verdicts is not None:
        if not isinstance(claim_verdicts, list):
            raise RatingError("EVAL_RATING_INVALID", "claim_verdicts must be a list", 422)
        cleaned = []
        for entry in claim_verdicts[:200]:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "")[:80]
            verdict = str(entry.get("verdict") or "")
            if key and verdict in ("right", "wrong", "unsure"):
                cleaned.append({"key": key, "verdict": verdict})
        claim_verdicts = cleaned

    timing_error = payload.get("timing_error_sec")
    if timing_error is not None:
        try:
            timing_error = abs(float(timing_error))
        except (TypeError, ValueError):
            raise RatingError("EVAL_RATING_INVALID", "timing_error_sec must be a number", 422)

    if existing is None:
        existing = EvaluationRating(
            id=new_id(), item_id=item_id, session_id=session_id,
            project_id=project_id, reviewer=reviewer or row.reviewer,
            version=1, created_at=_now(), updated_at=_now(),
        )
        session.add(existing)
    else:
        existing.version = int(existing.version) + 1
        existing.updated_at = _now()

    existing.scene = scores["scene"]
    existing.subject = scores["subject"]
    existing.action = scores["action"]
    existing.timing = scores["timing"]
    existing.timing_error_sec = timing_error
    existing.hallucination = hallucination
    existing.usability = usability
    if claim_verdicts is not None:
        existing.claim_verdicts = claim_verdicts
    session.flush()
    return existing


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _rate(numerator: int, denominator: int) -> float | None:
    """A rate with no denominator is *unknown*, not perfect."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _accuracy_block(values: list[int | None]) -> dict[str, Any]:
    judged = [v for v in values if v is not None]
    n = len(judged)
    return {
        "evaluated": n,
        "strict_hits": sum(1 for v in judged if v == 2),
        "partial_hits": sum(1 for v in judged if v >= 1),
        "strict_accuracy": _rate(sum(1 for v in judged if v == 2), n),
        "partial_or_better_rate": _rate(sum(1 for v in judged if v >= 1), n),
    }


def summarise(session: Session, project_id: str, session_id: str) -> dict[str, Any]:
    """Compute every metric from saved human ratings only."""
    row = get_session(session, project_id, session_id)
    if row is None:
        raise RatingError("EVAL_SESSION_NOT_FOUND", "no such session", 404)

    items = session.scalars(
        select(EvaluationItem).where(EvaluationItem.session_id == session_id)
        .order_by(EvaluationItem.position)
    ).all()
    ratings = {
        r.item_id: r for r in session.scalars(
            select(EvaluationRating).where(EvaluationRating.session_id == session_id)
        ).all()
    }

    counts = {"pending": 0, "rated": 0, "stale_evidence": 0,
              "source_unavailable": 0, "unjudged": 0}
    per_dim: dict[str, list[int | None]] = {d: [] for d in DIMENSIONS}
    hallucination_counts = {"severe": 0, "mild": 0, "none": 0, "unjudged": 0}
    usability_counts = {"useful": 0, "partly_useful": 0, "useless": 0, "unjudged": 0}
    claim_verdict_counts = {"right": 0, "wrong": 0, "unsure": 0}
    timing_errors: list[float] = []
    usable_denominator = 0

    item_rows: list[dict[str, Any]] = []
    for item in items:
        rating = ratings.get(item.id)
        current = _result_is_current(session, item)
        status = "PENDING"
        if not current:
            status = "STALE_EVIDENCE"
        elif not item.source_available:
            status = "SOURCE_UNAVAILABLE"
        elif rating is not None:
            scored = any(getattr(rating, d) is not None for d in DIMENSIONS)
            status = "RATED" if scored else "UNJUDGED"

        if rating is None:
            counts["pending"] += 1
        elif status == "STALE_EVIDENCE":
            counts["stale_evidence"] += 1
        elif status == "SOURCE_UNAVAILABLE":
            counts["source_unavailable"] += 1
        elif status == "UNJUDGED":
            counts["unjudged"] += 1
        else:
            counts["rated"] += 1

        # Only ratings that are current AND viewable enter the metrics.
        countable = (rating is not None and current and item.source_available)
        if countable:
            usable_denominator += 1
            for d in DIMENSIONS:
                per_dim[d].append(getattr(rating, d))
            # Per-sentence verdicts are the finest thing the reviewer produced.
            # Counting only the derived dimension scores threw that detail away
            # and reported "7 claims" for 46 judgements.
            for entry in (rating.claim_verdicts or []):
                if not isinstance(entry, dict):
                    continue
                verdict = entry.get("verdict")
                if verdict in ("right", "wrong", "unsure"):
                    claim_verdict_counts[verdict] += 1
            if rating.hallucination:
                hallucination_counts[rating.hallucination] += 1
            else:
                hallucination_counts["unjudged"] += 1
            if rating.usability:
                usability_counts[rating.usability] += 1
            else:
                usability_counts["unjudged"] += 1
            if rating.timing_error_sec is not None:
                timing_errors.append(float(rating.timing_error_sec))

        item_rows.append({
            "item_id": item.id,
            "segment_id": item.segment_id,
            "channel": item.channel,
            "result_id": item.result_id,
            "evidence_state": item.evidence_state,
            "source_available": item.source_available,
            "source_window": {"start_abs_sec": item.start_abs_sec,
                              "end_abs_sec": item.end_abs_sec},
            "review_status": status,
            "version": rating.version if rating else 0,
            "ratings": {
                d: (getattr(rating, d) if rating else None) for d in DIMENSIONS
            },
        })

    # The headline pools every judgement the reviewer actually made.
    #
    # Two granularities are reported, and the label says which is which:
    #
    #   sentences  -- one per claim the reviewer ticked. This is what the page
    #                 actually asks for, so it is the headline when available.
    #   dimensions -- the legacy per-dimension scores, kept for older sessions
    #                 (and for a channel whose reading has no sentence list).
    #
    # A reviewer who judged 46 sentences must not be reported as "7 claims".
    headline = [v for d in DIMENSIONS for v in per_dim[d] if v is not None]
    hallucination_judged = (hallucination_counts["severe"]
                            + hallucination_counts["mild"]
                            + hallucination_counts["none"])

    sentences_right = claim_verdict_counts["right"]
    sentences_wrong = claim_verdict_counts["wrong"]
    sentences_unsure = claim_verdict_counts["unsure"]
    # "не sure" leaves the denominator: it is a judgement about the reviewer's
    # confidence, not about the model being wrong.
    sentence_denominator = sentences_right + sentences_wrong

    strict = sum(1 for v in headline if v == 2)
    partial = sum(1 for v in headline if v >= 1)
    n = len(headline)

    if sentence_denominator > 0:
        headline_basis = "sentences"
        headline_n = sentence_denominator
        headline_strict = sentences_right
        headline_partial = sentences_right
    else:
        headline_basis = "dimensions"
        headline_n = n
        headline_strict = strict
        headline_partial = partial

    # Complete means every item was judged *and* every one was actually viewable.
    # A session where the reviewer skipped everything, or where sources vanished,
    # is not complete however many rows exist.
    status_label = "MANUAL_REVIEW_PENDING"
    if headline_n > 0:
        if counts["pending"] or counts["unjudged"] or counts["stale_evidence"]:
            status_label = "IN_PROGRESS"
        else:
            status_label = "COMPLETE"

    return {
        "session_id": session_id,
        "project_id": project_id,
        "status": status_label,
        "sample_seed": row.sample_seed,
        "sampling_rule_version": row.sampling_rule_version,
        "sample_count": len(items),
        # Which unit the headline counts, so a reader cannot mistake 46 sentences
        # for 46 shots.
        "metric_basis": headline_basis,
        "evaluated_claims": headline_n,
        "strict_accuracy": _rate(headline_strict, headline_n),
        "partial_or_better_rate": _rate(headline_partial, headline_n),
        "sentences": {
            "right": sentences_right,
            "wrong": sentences_wrong,
            "unsure": sentences_unsure,
            "judged": sentences_right + sentences_wrong + sentences_unsure,
            "evaluated": sentence_denominator,
        },
        "unjudged": counts["unjudged"],
        "counts": counts,
        "by_dimension": {d: _accuracy_block(per_dim[d]) for d in DIMENSIONS},
        "hallucination": {
            "severe": hallucination_counts["severe"],
            "mild": hallucination_counts["mild"],
            "none": hallucination_counts["none"],
            "judged": hallucination_judged,
            "rate": _rate(hallucination_counts["severe"] + hallucination_counts["mild"],
                          hallucination_judged),
        },
        "usability": usability_counts,
        "timing": {
            "samples": len(timing_errors),
            "median_sec": _median(timing_errors),
            "max_sec": max(timing_errors) if timing_errors else None,
        },
        "denominator_notes": {
            "countable_items": usable_denominator,
            "excludes": ["pending", "unjudged", "stale_evidence", "source_unavailable"],
        },
        "limitations": [
            "small_sample_not_population_estimate",
            "model_confidence_is_not_accuracy",
        ],
        "items": item_rows,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def session_detail(session: Session, project_id: str, session_id: str) -> dict[str, Any]:
    """Everything the page needs to resume: items, ratings and progress."""
    row = get_session(session, project_id, session_id)
    if row is None:
        raise RatingError("EVAL_SESSION_NOT_FOUND", "no such session", 404)

    items = session.scalars(
        select(EvaluationItem).where(EvaluationItem.session_id == session_id)
        .order_by(EvaluationItem.position)
    ).all()
    ratings = {
        r.item_id: r for r in session.scalars(
            select(EvaluationRating).where(EvaluationRating.session_id == session_id)
        ).all()
    }

    out_items = []
    for item in items:
        rating = ratings.get(item.id)
        current = _result_is_current(session, item)
        status = ("STALE_EVIDENCE" if not current
                  else "SOURCE_UNAVAILABLE" if not item.source_available
                  else "RATED" if rating is not None else "PENDING")
        out_items.append({
            "item_id": item.id,
            "segment_id": item.segment_id,
            "asset_id": item.asset_id,
            "channel": item.channel,
            "result_id": item.result_id,
            "evidence_state": item.evidence_state,
            "source_window": {"start_abs_sec": item.start_abs_sec,
                              "end_abs_sec": item.end_abs_sec},
            "source_available": item.source_available,
            "frame_ids": list(item.frame_ids or []),
            "readings": dict(item.readings_json or {}),
            "review_status": status,
            "version": rating.version if rating else 0,
            "ratings": {
                "scene": rating.scene if rating else None,
                "subject": rating.subject if rating else None,
                "action": rating.action if rating else None,
                "timing": rating.timing if rating else None,
                "hallucination": rating.hallucination if rating else None,
                "usability": rating.usability if rating else None,
                "timing_error_sec": rating.timing_error_sec if rating else None,
            },
            # Returned so the page can redraw the boxes the reviewer already ticked.
            # Without it, paging away and back showed an empty form and re-picking
            # a few silently replaced the saved set.
            "claim_verdicts": (list(rating.claim_verdicts or []) if rating else []),
        })

    rated = sum(1 for i in out_items if i["review_status"] == "RATED")
    return {
        "session_id": row.id,
        "project_id": row.project_id,
        "status": row.status,
        "sample_seed": row.sample_seed,
        "sampling_rule_version": row.sampling_rule_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sample_count": len(out_items),
        "rated_count": rated,
        "items": out_items,
        "context": row.context_json or {},
    }
