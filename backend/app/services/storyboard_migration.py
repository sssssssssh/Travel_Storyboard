"""TSB-366 — migrate legacy ``EditPlanShot`` rows into storyboard version 1.

The legacy plan was a flat table that every save replaced wholesale. This module
freezes the *current* state of that table as an immutable version 1 per project,
so the versioning work starts from a truthful baseline instead of a blank slate.

Design rules (task book §3.1), and how each is met:

1. **Back up first.** ``init_db`` already snapshots the SQLite file before any
   schema change; the migration additionally refuses to run against a database
   it cannot read.
2. **At most one board per project.** Guaranteed by the unique index on
   ``storyboards.project_id``.
3. **Non-empty plan → version 1, reason ``legacy_import``.** Empty plan → an
   *empty* version, never invented shots (§3.1-4).
4. **Array order preserved.** ``EditPlanShot.order`` is the legacy ordering and
   is copied as the snapshot order.
5. **All legacy fields kept**, including hints and export-relevant values.
6. **Nothing invented.** Brief/sections/constraints stay empty except for one
   section explicitly marked ``legacy_import`` so no reader mistakes a migrated
   plan for an AI-designed one. Scores and confirmed states are copied, not
   guessed.
7. **Old rows survive.** ``edit_plan_shots`` is never dropped or emptied here.
8. **Idempotent.** A project that already owns a board is skipped; re-running
   creates nothing.

Failure containment: each project runs in its own transaction. A project that
raises is rolled back in full — no board, no version, no refs — and is counted
as ``failed`` in the report so the next run can retry it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import begin_write, session_scope
from ..models import (
    Asset,
    EditPlanShot,
    LegacyPlanMigration,
    Project,
    Segment,
    Storyboard,
    StoryboardSourceRef,
    StoryboardVersion,
)
from .storyboard_schema import (
    SNAPSHOT_SCHEMA_VERSION,
    BriefSnapshot,
    ConstraintsSnapshot,
    SectionSnapshot,
    ShotSnapshot,
    StoryboardSnapshot,
)
from .storyboard_service import validate_references

logger = logging.getLogger(__name__)

#: The single section a migrated plan gets. Its ``kind`` says plainly that no
#: designer wrote it (§2.4 / §3.1-7).
LEGACY_SECTION_ID = "legacy-import"
LEGACY_SECTION = SectionSnapshot(
    section_id=LEGACY_SECTION_ID,
    title="旧计划",
    kind="legacy_import",
    purpose="从旧版手工计划迁移；未包含章节叙事或选择理由",
)


def _shot_snapshot(row: EditPlanShot) -> ShotSnapshot:
    """Map one legacy row to a snapshot shot, field for field.

    Every value is copied. Where the legacy schema had no equivalent the
    optional snapshot field is left ``None`` — an explicit "not stated" rather
    than a plausible default that would read as provenance.
    """
    return ShotSnapshot(
        # Stability: the legacy row id becomes the shot identity, so a re-run and
        # any future edit can still address the same shot.
        shot_id=row.id,
        section_id=LEGACY_SECTION_ID,
        asset_id=row.asset_id,
        segment_id=row.segment_id,
        source_in_sec=row.source_in_sec,
        source_out_sec=row.source_out_sec,
        target_duration_sec=row.target_duration_sec,
        timeline_start_sec=row.timeline_start_sec,
        story_role=row.story_role or "",
        transition_hint=row.transition_hint or "",
        music_cue=row.music_cue or "",
        audio_hint=row.audio_hint or "",
        color_hint=row.color_hint or "",
        evidence=row.evidence or "",
        confidence=float(row.confidence or 0.0),
        review_status=row.review_status or "pending",
        # Fields the legacy plan never had stay unknown.
        reason=None,
        locked=None,
        source_signature=None,
    )


def _build_snapshot(session: Session, project_id: str) -> StoryboardSnapshot:
    rows = session.scalars(
        select(EditPlanShot)
        .where(EditPlanShot.project_id == project_id)
        .order_by(EditPlanShot.order, EditPlanShot.created_at)
    ).all()

    shots = [_shot_snapshot(r) for r in rows]
    validation = validate_references(session, project_id=project_id, shots=shots)

    return StoryboardSnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        brief=BriefSnapshot(),            # nothing was ever stated: stay empty
        sections=[LEGACY_SECTION] if shots else [],
        shots=shots,
        constraints=ConstraintsSnapshot(),
        validation_summary=validation,
    )


def _write_version_and_refs(
    session: Session,
    *,
    board: Storyboard,
    snapshot: StoryboardSnapshot,
    assets: dict[str, Asset],
) -> StoryboardVersion:
    """Write version 1 and its source refs.

    Kept as its own function so the atomicity test can inject a failure *after*
    the board row is staged and prove the whole project rolls back (§7.2-8).
    """
    version = StoryboardVersion(
        board_id=board.id,
        version_no=1,
        parent_version_id=None,
        schema_version=snapshot.schema_version,
        brief=snapshot.brief.model_dump(),
        sections=[s.model_dump() for s in snapshot.sections],
        shots=[s.model_dump() for s in snapshot.shots],
        constraints=snapshot.constraints.model_dump(),
        validation_summary=snapshot.validation_summary.model_dump(),
        actor="migration",
        reason="legacy_import",
        job_id=None,
        idempotency_key=None,
    )
    session.add(version)
    session.flush()

    for shot in snapshot.shots:
        asset = assets.get(shot.asset_id)
        signature = ""
        if asset is not None:
            signature = (
                getattr(asset, "analyzed_source_signature", "")
                or getattr(asset, "quick_fingerprint", "")
                or "legacy"
            )
        session.add(StoryboardSourceRef(
            version_id=version.id,
            shot_id=shot.shot_id,
            asset_id=shot.asset_id,
            segment_id=shot.segment_id,
            source_signature=signature,
        ))
    session.flush()
    return version


def migrate_project(session: Session, project_id: str) -> dict[str, Any]:
    """Migrate one project inside the caller's transaction.

    Returns ``{"action": "migrated"|"empty"|"skipped", ...}``. Raising is
    reserved for genuine failures; an already-migrated project is a skip.
    """
    existing = session.scalars(
        select(Storyboard).where(Storyboard.project_id == project_id)
    ).first()
    if existing is not None:
        return {"action": "skipped", "reason": "storyboard already exists"}

    snapshot = _build_snapshot(session, project_id)

    board = Storyboard(project_id=project_id, title="")
    session.add(board)
    session.flush()

    assets = {
        a.id: a
        for a in session.scalars(
            select(Asset).where(Asset.project_id == project_id)
        ).all()
    }
    version = _write_version_and_refs(
        session, board=board, snapshot=snapshot, assets=assets
    )

    board.current_version_id = version.id
    session.flush()

    return {
        "action": "migrated" if snapshot.shots else "empty",
        "board_id": board.id,
        "version_id": version.id,
        "shots": len(snapshot.shots),
        "validation_state": snapshot.validation_summary.state,
    }


def _legacy_shot_count(session: Session, project_id: str) -> int:
    """How many legacy rows this project has (the "is there anything to migrate")."""
    return int(
        session.scalar(
            select(func.count()).select_from(EditPlanShot).where(
                EditPlanShot.project_id == project_id
            )
        )
        or 0
    )


def _upsert_status(
    session: Session,
    *,
    project_id: str,
    state: str,
    error: str = "",
    legacy_shot_count: int = 0,
) -> None:
    """Record the migration outcome for one project (idempotent, in-transaction)."""
    row = session.scalars(
        select(LegacyPlanMigration).where(LegacyPlanMigration.project_id == project_id)
    ).first()
    if row is None:
        row = LegacyPlanMigration(
            project_id=project_id, state=state, error=error,
            legacy_shot_count=legacy_shot_count, attempts=1,
        )
        session.add(row)
    else:
        row.state = state
        row.error = error
        row.legacy_shot_count = legacy_shot_count
        row.attempts = int(row.attempts or 0) + 1
    session.flush()


def migrate_legacy_edit_plans(project_ids: list[str] | None = None) -> dict[str, int]:
    """Migrate every project's legacy plan, one transaction per project.

    Returns the report §3.2 asks for: projects / migrated / empty / invalid /
    skipped / failed. Every project is accounted for exactly once.

    TSB-366-R1 (P1-04): each attempt also writes a :class:`LegacyPlanMigration`
    row, **including failures**, so a later read can tell "this project still
    needs migrating" from "this project simply has no plan". A failure is
    therefore queryable rather than only logged.
    """
    report = {
        "projects": 0,
        "migrated": 0,
        "empty": 0,
        "invalid": 0,
        "skipped": 0,
        "failed": 0,
    }
    with session_scope() as s:
        ids = project_ids or [
            pid for pid in s.scalars(select(Project.id).order_by(Project.created_at)).all()
        ]
    report["projects"] = len(ids)

    for project_id in ids:
        try:
            # Each project gets its own transaction: a failure cannot take an
            # already-migrated project down with it (§3.2).
            with session_scope() as s:
                begin_write(s)
                legacy_count = _legacy_shot_count(s, project_id)
                outcome = migrate_project(s, project_id)
                if outcome["action"] != "skipped":
                    _upsert_status(
                        s, project_id=project_id, state="done",
                        legacy_shot_count=legacy_count,
                    )
                else:
                    # Already migrated: make sure the status row agrees.
                    row = s.scalars(select(LegacyPlanMigration).where(
                        LegacyPlanMigration.project_id == project_id)).first()
                    if row is None:
                        _upsert_status(
                            s, project_id=project_id, state="done",
                            legacy_shot_count=legacy_count,
                        )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed silently
            logger.exception("legacy plan migration failed for project %s", project_id)
            report["failed"] += 1
            # The failure record is written in its OWN transaction: the failed
            # project's transaction rolled back, so it cannot carry this row.
            try:
                with session_scope() as s2:
                    begin_write(s2)
                    _upsert_status(
                        s2, project_id=project_id, state="failed",
                        error=f"{type(exc).__name__}: {exc}"[:800],
                        legacy_shot_count=_legacy_shot_count(s2, project_id),
                    )
            except Exception:  # noqa: BLE001 - a status write must not mask the cause
                logger.exception("could not record migration failure for %s", project_id)
            continue

        action = outcome["action"]
        if action == "skipped":
            report["skipped"] += 1
        elif action == "empty":
            report["empty"] += 1
        else:
            report["migrated"] += 1
            if outcome.get("validation_state") == "invalid":
                report["invalid"] += 1

    logger.info("legacy plan migration report: %s", report)
    return report


def migration_status(*, project_id: str) -> dict[str, Any]:
    """Queryable migration state for one project (P1-04 requirement 4).

    ``state`` is ``done`` / ``failed`` / ``pending``. A project that was never
    seen by the migrator and has no legacy rows reports ``not_applicable``.
    """
    with session_scope() as s:
        row = s.scalars(
            select(LegacyPlanMigration).where(
                LegacyPlanMigration.project_id == project_id
            )
        ).first()
        if row is not None:
            return {
                "project_id": project_id,
                "state": row.state,
                "error": row.error or "",
                "attempts": int(row.attempts or 0),
                "legacy_shot_count": int(row.legacy_shot_count or 0),
            }
        has_board = s.scalars(
            select(Storyboard.id).where(Storyboard.project_id == project_id)
        ).first() is not None
        legacy = _legacy_shot_count(s, project_id)
        if has_board:
            return {"project_id": project_id, "state": "done", "error": "",
                    "attempts": 0, "legacy_shot_count": legacy}
        if legacy:
            return {"project_id": project_id, "state": "pending", "error": "",
                    "attempts": 0, "legacy_shot_count": legacy}
        return {"project_id": project_id, "state": "not_applicable", "error": "",
                "attempts": 0, "legacy_shot_count": 0}


def retry_migration(*, project_id: str) -> dict[str, Any]:
    """Re-run the migration for one project, safely and idempotently.

    Safe because :func:`migrate_project` skips a project that already owns a
    board, so retrying a migrated (or since-edited) project cannot create a
    second v1 or move the current pointer.
    """
    report = migrate_legacy_edit_plans(project_ids=[project_id])
    return {"report": report, "status": migration_status(project_id=project_id)}


def reconcile_project(session: Session, project_id: str) -> list[str]:
    """Compare the legacy rows with version 1 and list any drift (§3.3).

    Returns a list of human-readable differences; an empty list means the
    migration reproduced the legacy plan exactly. Used by the tests and by the
    acceptance tooling — never to "fix" anything.
    """
    problems: list[str] = []
    board = session.scalars(
        select(Storyboard).where(Storyboard.project_id == project_id)
    ).first()
    if board is None:
        return [f"{project_id}: no storyboard"]

    # TSB-366-R1 (P1-05): always compare against the *migrated* version, never
    # ``current_version_id``. After the user edits the plan the pointer holds v2
    # or v3, so using it would reconcile "legacy rows vs. a later edit" — a
    # comparison that says nothing about migration quality (and reports drift on
    # every edited project).
    version = session.scalars(
        select(StoryboardVersion)
        .where(
            StoryboardVersion.board_id == board.id,
            StoryboardVersion.reason == "legacy_import",
        )
        .order_by(StoryboardVersion.version_no)
    ).first()
    if version is None:
        return [f"{project_id}: no legacy_import version to reconcile against"]

    legacy = session.scalars(
        select(EditPlanShot)
        .where(EditPlanShot.project_id == project_id)
        .order_by(EditPlanShot.order, EditPlanShot.created_at)
    ).all()
    shots = version.shots or []

    if len(legacy) != len(shots):
        problems.append(
            f"{project_id}: shot count {len(legacy)} -> {len(shots)}"
        )
        return problems

    fields = (
        "asset_id", "segment_id", "source_in_sec", "source_out_sec",
        "target_duration_sec", "story_role", "transition_hint", "music_cue",
        "audio_hint", "color_hint", "evidence", "confidence", "review_status",
    )
    for index, (row, shot) in enumerate(zip(legacy, shots)):
        for field in fields:
            want = getattr(row, field)
            got = shot.get(field)
            if isinstance(want, float):
                if got is None or abs(float(got) - want) > 1e-9:
                    problems.append(
                        f"{project_id}[{index}].{field}: {want} -> {got}"
                    )
            elif got != want:
                problems.append(f"{project_id}[{index}].{field}: {want!r} -> {got!r}")
    return problems
