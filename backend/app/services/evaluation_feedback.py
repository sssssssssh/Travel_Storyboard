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

    Kept for single lookups; whole pages should use :func:`verdicts_for_project`,
    which answers in a constant number of queries.
    """
    items = session.scalars(
        select(EvaluationItem).where(
            EvaluationItem.project_id == project_id,
            EvaluationItem.segment_id == segment_id,
        ).order_by(EvaluationItem.position.desc())
    ).all()
    if not items:
        return _empty_verdict()

    pairs: list[tuple[EvaluationItem, EvaluationRating | None]] = []
    for item in items:
        if not _current_result_id(session, item):
            continue
        rating = session.scalars(
            select(EvaluationRating).where(EvaluationRating.item_id == item.id)
        ).first()
        if rating is None:
            continue
        pairs.append((item, rating))
    return _summarise_verdicts(pairs)


def verdicts_for_project(session: Session, project_id: str,
                         segment_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Every shot's verdict in a constant number of queries.

    TSB-371-R1 P2-01: the per-shot form issued three queries per shot (items,
    currency check, rating), so a 100-shot page cost 300 and a 1000-shot library
    cost 3000. This loads the three tables once and groups in memory.

    The per-shot :func:`review_verdict` remains for single lookups; both share
    :func:`_summarise_verdicts`, so they cannot disagree.
    """
    if not segment_ids:
        return {}

    wanted = set(segment_ids)
    items = session.scalars(
        select(EvaluationItem).where(
            EvaluationItem.project_id == project_id,
            EvaluationItem.segment_id.in_(wanted),
        ).order_by(EvaluationItem.position.desc())
    ).all()
    if not items:
        return {sid: _empty_verdict() for sid in segment_ids}

    # Which of these readings are still current? One query for all three channels.
    current: set[str] = set()
    by_channel: dict[str, list[str]] = {}
    for item in items:
        if item.result_id:
            by_channel.setdefault(item.channel, []).append(item.result_id)
    for channel, ids in by_channel.items():
        model = _CHANNEL_MODEL.get(channel)
        if model is None:
            continue
        rows = session.scalars(
            select(model.id).where(model.id.in_(ids),
                                   model.is_current.is_(True))
        ).all()
        current.update(rows)

    item_ids = [item.id for item in items]
    ratings = {
        r.item_id: r for r in session.scalars(
            select(EvaluationRating).where(EvaluationRating.item_id.in_(item_ids))
        ).all()
    }

    grouped: dict[str, list[tuple[EvaluationItem, EvaluationRating | None]]] = {}
    for item in items:
        rating = ratings.get(item.id)
        is_current = bool(item.result_id) and item.result_id in current
        grouped.setdefault(item.segment_id, []).append(
            (item, rating if is_current else None))

    out: dict[str, dict[str, Any]] = {}
    for seg_id in segment_ids:
        out[seg_id] = _summarise_verdicts(grouped.get(seg_id, []))
    return out


def _empty_verdict() -> dict[str, Any]:
    return {
        "state": STATE_UNREVIEWED,
        "right": 0, "wrong": 0, "unsure": 0, "judged": 0,
        "planning_reasons": [], "session_id": "", "item_id": "",
    }


def _summarise_verdicts(pairs: list[tuple[EvaluationItem, EvaluationRating | None]]
                        ) -> dict[str, Any]:
    """Count verdicts for one shot. Shared by the single and batch paths."""
    right = wrong = unsure = 0
    judged_item: EvaluationItem | None = None
    for item, rating in pairs:
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
            judged_item = item

    judged = right + wrong + unsure
    if judged == 0:
        return _empty_verdict()

    # "Not sure" about every sentence says nothing about the model, so the shot
    # stays unreviewed. Only an explicit right/wrong expresses a judgement.
    if right == 0 and wrong == 0:
        return {**_empty_verdict(), "unsure": unsure, "judged": judged}

    state = STATE_WRONG if wrong > 0 else STATE_TRUSTED
    reasons = [REASON_JUDGED_WRONG] if state == STATE_WRONG else []
    return {
        "state": state,
        "right": right,
        "wrong": wrong,
        "unsure": unsure,
        "judged": judged,
        "planning_reasons": reasons,
        "session_id": judged_item.session_id if judged_item else "",
        "item_id": judged_item.id if judged_item else "",
    }


# ---------------------------------------------------------------------------
# TSB-371-R1 P1-01 — feed the judgement into the planner, per claim
# ---------------------------------------------------------------------------
#
# The candidate-list flag was not enough: ``build_planning_input`` never read the
# ratings, so a sentence marked wrong still reached the plan.
#
# The unit here is the *claim*, not the shot. A wrong description does not make
# the footage worthless (task book §2), so this never excludes a shot and never
# narrows its usable time window. It withholds the specific claims a human marked
# wrong, keeps the ones marked right, ignores "not sure", and reports what it did.

#: Claim groups that can be withheld, keyed by the readings group name.
WITHHOLDABLE_GROUPS = ("scene_tags", "subjects", "visible_actions", "mood")

#: Single-value claims: a wrong verdict blanks the field.
SCALAR_CLAIMS = ("lighting", "composition", "action_summary",
                 "motion_type", "stability_risk")

REASON_WITHHELD = "human_review_withheld_claims"
REASON_CLAIMS_RIGHT = "human_review_claims_confirmed"


def _claim_key(group: str, index: int) -> str:
    return f"{group}:{index}"


def claim_verdicts_for_result(session: Session, project_id: str,
                              result_id: str) -> dict[str, str]:
    """Verdicts keyed by claim, for *one* result row, if it is still current.

    Returns ``{}`` when the row is not current: an opinion about a superseded
    reading must not grade the new one.
    """
    if not result_id:
        return {}
    items = session.scalars(
        select(EvaluationItem).where(
            EvaluationItem.project_id == project_id,
            EvaluationItem.result_id == result_id,
        )
    ).all()
    out: dict[str, str] = {}
    for item in items:
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
            key, verdict = entry.get("key"), entry.get("verdict")
            if isinstance(key, str) and verdict in ("right", "wrong", "unsure"):
                out[key] = verdict
    return out


def filter_visual(visual: dict[str, Any],
                  verdicts: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Withhold the claims a human marked wrong.

    Returns the filtered payload and the reasons to disclose. Order is preserved:
    a reviewer's verdict removes an item, it does not reshuffle the reading.
    """
    if not verdicts:
        return visual, []

    filtered = dict(visual)
    withheld = 0
    confirmed = 0

    for group in WITHHOLDABLE_GROUPS:
        values = visual.get(group)
        if not isinstance(values, list) or not values:
            continue
        kept = []
        for index, value in enumerate(values):
            verdict = verdicts.get(_claim_key(group, index))
            if verdict == "wrong":
                withheld += 1
                continue
            if verdict == "right":
                confirmed += 1
            kept.append(value)
        filtered[group] = kept

    # Scalar claims live in the caller's payload; blank them even when the key is
    # absent from the list groups. "in filtered" would skip action_summary, which
    # the motion reading adds to the same dict.
    for name in SCALAR_CLAIMS:
        verdict = verdicts.get(name)
        if verdict is None:
            verdict = verdicts.get(f"{name}:0")
        if verdict == "wrong":
            filtered[name] = ""
            withheld += 1
        elif verdict == "right":
            confirmed += 1

    reasons: list[str] = []
    if withheld:
        reasons.append(REASON_WITHHELD)
    if confirmed and not withheld:
        reasons.append(REASON_CLAIMS_RIGHT)
    elif confirmed:
        reasons.append(REASON_CLAIMS_RIGHT)
    return filtered, reasons


def review_signature(session: Session, project_id: str,
                     result_ids: list[str]) -> str:
    """A stable digest of the review facts that affected a planning input.

    Included in the planning digest so a plan built from filtered evidence can
    never be confused with one built from untouched evidence.
    """
    import hashlib

    parts: list[str] = []
    for result_id in result_ids:
        verdicts = claim_verdicts_for_result(session, project_id, result_id or "")
        if not verdicts:
            continue
        joined = ";".join(f"{k}={verdicts[k]}" for k in sorted(verdicts))
        parts.append(f"{result_id}:{joined}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# TSB-371-R1 P1-04 — the claim catalogue of a frozen item
# ---------------------------------------------------------------------------

#: Groups whose values are lists: claim key is "<group>:<index>".
LIST_GROUPS = ("scene_tags", "subjects", "visible_actions", "mood",
               "events", "limitations", "evidence_times_sec")

#: Single-value groups: claim key is "<group>:0".
SCALAR_GROUPS = ("lighting", "composition", "action_summary", "motion_type",
                 "stability_risk", "summary")


def claim_catalogue(readings: dict[str, Any] | None) -> dict[str, str]:
    """Every claim a frozen item supports, mapped to the text being judged.

    Built from ``EvaluationItem.readings_json`` — the snapshot taken when the
    session was created — so a session stays internally consistent even if the
    model is re-run afterwards. Callers use this to reject verdicts for claims
    that were never offered, and to render the report.
    """
    catalogue: dict[str, str] = {}
    for group, value in (readings or {}).items():
        if isinstance(value, list):
            for index, item in enumerate(value):
                catalogue[f"{group}:{index}"] = str(item)
        elif isinstance(value, str) and value.strip():
            catalogue[f"{group}:0"] = value.strip()
    return catalogue
