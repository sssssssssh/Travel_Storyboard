"""TSB-371-E — turn human judgements into a planning signal.

The review used to be a dead end: it produced a report and nothing read it, so a
reviewer could mark every sentence wrong and the planner would keep using the same
readings. This module closes that loop with the narrowest rule that is still useful.

What it does:

* reads the human's per-sentence verdicts for a shot;
* decides a state — ``trusted`` / ``wrong`` / ``unreviewed``;
* hands the planner *reasons*, the same way every other eligibility problem is
  expressed, so a downgrade is visible rather than silent.

What it deliberately does **not** do:

* rewrite the model reading, the segment, the annotation or the storyboard. A
  review is an opinion *about* evidence, and the evidence stays as recorded;
* treat silence as disapproval. An unreviewed shot is untouched;
* treat "not sure" as "wrong". Only an explicit ``wrong`` downgrades;
* count a judgement of a superseded reading against the current one.

Because the rule is computed from the review tables on every read, changing a
verdict — or deleting it — restores the shot immediately.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    EvaluationItem,
    EvaluationRating,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
)

STATE_TRUSTED = "trusted"
STATE_WRONG = "wrong"
STATE_UNREVIEWED = "unreviewed"

#: Reasons this module contributes to the planner's eligibility reasons.
REASON_JUDGED_WRONG = "review_judged_wrong"

_CHANNEL_MODEL = {
    "static": SegmentSemanticResult,
    "multiframe": SegmentMotionReview,
    "video_native": SegmentVideoObservation,
}


def _current_result_id(session: Session, item: EvaluationItem) -> bool:
    model = _CHANNEL_MODEL.get(item.channel)
    if model is None:
        return False
    row = session.get(model, item.result_id)
    return row is not None and bool(getattr(row, "is_current", False))


def review_verdict(session: Session, project_id: str, segment_id: str) -> dict[str, Any]:
    """The human verdict for one shot, computed from the review tables.

    Never writes. Returns ``unreviewed`` when there is nothing to say, so callers
    cannot mistake absence of judgement for a judgement.
    """
    empty = {
        "state": STATE_UNREVIEWED,
        "right": 0, "wrong": 0, "unsure": 0, "judged": 0,
        "planning_reasons": [],
        "session_id": "",
        "item_id": "",
    }

    items = session.scalars(
        select(EvaluationItem).where(
            EvaluationItem.project_id == project_id,
            EvaluationItem.segment_id == segment_id,
        ).order_by(EvaluationItem.position.desc())
    ).all()
    if not items:
        return empty

    right = wrong = unsure = 0
    judged_item = None
    for item in items:
        # A judgement about a reading that has since been replaced does not grade
        # the new reading; skip it rather than transfer the opinion.
        if not _current_result_id(session, item):
            continue
        rating = session.scalars(
            select(EvaluationRating).where(EvaluationRating.item_id == item.id)
        ).first()
        if rating is None:
            continue
        for entry in (rating.claim_verdicts or []):
            if not isinstance(entry, dict):
                continue
            verdict = entry.get("verdict")
            if verdict == "right":
                right += 1
            elif verdict == "wrong":
                wrong += 1
            elif verdict == "unsure":
                unsure += 1
        if right or wrong or unsure:
            judged_item = (item, rating)

    judged = right + wrong + unsure
    if judged == 0:
        return empty

    # "Not sure" about every sentence says nothing about the model, so the shot
    # stays unreviewed. Only an explicit right/wrong expresses a judgement.
    if right == 0 and wrong == 0:
        return {**empty, "unsure": unsure, "judged": judged}

    state = STATE_WRONG if wrong > 0 else STATE_TRUSTED
    reasons = [REASON_JUDGED_WRONG] if state == STATE_WRONG else []
    item, _rating = judged_item  # type: ignore[misc]
    return {
        "state": state,
        "right": right,
        "wrong": wrong,
        "unsure": unsure,
        "judged": judged,
        "planning_reasons": reasons,
        "session_id": item.session_id,
        "item_id": item.id,
    }


def verdicts_for_project(session: Session, project_id: str,
                         segment_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch form, so the candidate list does not issue one query per shot."""
    return {sid: review_verdict(session, project_id, sid) for sid in segment_ids}
