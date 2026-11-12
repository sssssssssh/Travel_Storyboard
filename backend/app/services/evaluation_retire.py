"""TSB-371 — retire a source the user no longer has.

When a file is gone for good, its shots should stop appearing as usable candidates.
The user asked for exactly this: "不要了".

What retiring does:

* marks the asset ``retired``, with when and why;
* makes its shots ineligible for planning (with a stable reason), so they cannot
  quietly keep being proposed;
* leaves every other row alone.

What it must **not** do — and what the tests pin down:

* delete the asset, its segments, its model readings, its annotations, or any
  storyboard reference. A retired file is history, not garbage: the readings it
  produced are paid-for evidence and the audit trail of what was once proposed;
* treat "retired" as "the user rejected these shots". They were never judged —
  the source vanished. Conflating the two would poison the rating metrics;
* touch a segment that already carries a human opinion. If someone rated or
  annotated a shot, retiring the file must not erase that.

Reversal: :func:`unretire_asset` exists because the user might find the file.
Both directions are recorded on the asset, so the state is explainable later.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, Segment, UserAnnotation

#: Set on the asset when the user says the file is not coming back.
RETIRED = "retired"
ACTIVE = "active"

#: Reason surfaced by the planner for a shot whose source was retired.
REASON_SOURCE_RETIRED = "source_retired_by_user"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retire_asset(session: Session, project_id: str, asset_id: str, *,
                 note: str = "") -> dict[str, Any]:
    """Mark an asset as gone for good. Never deletes anything."""
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id:
        raise ValueError("asset not found in this project")

    already = getattr(asset, "source_state", ACTIVE) or ACTIVE
    segments = session.scalars(
        select(Segment).where(Segment.asset_id == asset_id)).all()

    # Count what a careless implementation would destroy, so the caller can see
    # that nothing was.
    preserved = {
        "segments": len(segments),
        "model_readings": _count_model_readings(session, [s.id for s in segments]),
        "annotations": _count_annotations(session, [s.id for s in segments]),
    }

    asset.source_state = RETIRED
    asset.source_retired_at = _now()
    asset.source_retired_note = note[:200]
    session.flush()

    return {
        "asset_id": asset_id,
        "display_name": asset.display_name,
        "previous_state": already,
        "source_state": RETIRED,
        "preserved": preserved,
        "note": note[:200],
        "reversible": True,
    }


def unretire_asset(session: Session, project_id: str, asset_id: str) -> dict[str, Any]:
    """Undo a retirement (for when the file turns up again)."""
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id:
        raise ValueError("asset not found in this project")
    previous = getattr(asset, "source_state", ACTIVE) or ACTIVE
    asset.source_state = ACTIVE
    asset.source_retired_note = ""
    session.flush()
    return {
        "asset_id": asset_id,
        "display_name": asset.display_name,
        "previous_state": previous,
        "source_state": ACTIVE,
        "reversible": True,
    }


def _count_model_readings(session: Session, segment_ids: list[str]) -> int:
    if not segment_ids:
        return 0
    from ..models import SegmentSemanticResult

    return len(session.scalars(
        select(SegmentSemanticResult.id).where(
            SegmentSemanticResult.segment_id.in_(segment_ids))).all())


def _count_annotations(session: Session, segment_ids: list[str]) -> int:
    if not segment_ids:
        return 0
    return len(session.scalars(
        select(UserAnnotation.id).where(
            UserAnnotation.segment_id.in_(segment_ids))).all())


def retired_asset_ids(session: Session, project_id: str) -> set[str]:
    """Ids of assets the user retired in this project."""
    rows = session.scalars(
        select(Asset.id).where(
            Asset.project_id == project_id,
            Asset.source_state == RETIRED,
        )).all()
    return set(rows)


def is_retired(session: Session, asset_id: str) -> bool:
    asset = session.get(Asset, asset_id)
    return bool(asset is not None and (getattr(asset, "source_state", ACTIVE) == RETIRED))
