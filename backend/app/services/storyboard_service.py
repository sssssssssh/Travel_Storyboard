"""TSB-366 — storyboard version read / save / restore and the export adapter.

This module is the **single write path** for a storyboard version. Everything
that changes a plan goes through :func:`save_version` or :func:`restore_version`;
everything that reads one goes through :func:`load_storyboard_version`, which the
JSON/CSV/OTIO exporters and the roughcut job now share. That is what makes the
task book's "唯一数据权威" true in code rather than by convention.

The three guarantees, and how each is enforced:

* **History is not overwritten.** A save INSERTs a new ``StoryboardVersion``.
  ``database._create_storyboard_triggers`` additionally forbids UPDATE on the
  snapshot columns, so even a direct SQL edit cannot rewrite history.
* **Restore moves forward.** :func:`restore_version` copies the target snapshot
  into a *new* version whose ``parent_version_id`` is the version that was
  current. The ``current_version_id`` pointer is then advanced to the new row —
  never rewound (§4.3).
* **Writes are base-version guarded.** :func:`save_version` compares the
  caller's ``base_version_id`` against ``current_version_id`` inside the same
  ``BEGIN IMMEDIATE`` transaction that performs the insert, so two concurrent
  clients cannot both win (§4.2). The loser raises :class:`VersionConflict`.

Reference validation is deliberately *reported*, not repaired: a shot whose
segment no longer exists still lands in the snapshot, with a
``needs_review`` validation summary. Silently swapping a broken reference for a
"best guess" is exactly what §7.6 forbids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import begin_write, session_scope
from ..models import (
    Asset,
    Project,
    Segment,
    Storyboard,
    StoryboardSourceRef,
    StoryboardVersion,
)
from .storyboard_schema import (
    SNAPSHOT_SCHEMA_VERSION,
    ACTORS,
    REASONS,
    BriefSnapshot,
    ConstraintsSnapshot,
    SectionSnapshot,
    ShotSnapshot,
    SnapshotError,
    StoryboardSnapshot,
    ValidationIssue,
    ValidationSummary,
)

logger = logging.getLogger(__name__)

#: Signature recorded when the legacy plan never carried one. Never invented:
#: ``_identity_signature`` returns this instead of hashing something plausible.
LEGACY_SIGNATURE = "legacy"

#: A restore may only be committed from these validation states. ``invalid``
#: means a reference is broken badly enough that promoting it to "current" would
#: present a plan the user cannot execute; it is still readable as history.
RESTORABLE_STATES = frozenset({"ok", "needs_review"})


class StoryboardError(Exception):
    """Base class for storyboard service errors."""


class StoryboardNotFound(StoryboardError):
    """The board / version / project does not exist (or belongs elsewhere).

    Cross-project access raises the *same* error as a missing object so an id
    from another project cannot be probed for existence (§4.1).
    """


class VersionConflict(StoryboardError):
    """The caller's ``base_version_id`` is no longer the current version."""

    def __init__(self, current_version_id: str | None, base_version_id: str | None) -> None:
        super().__init__(
            "PLAN_VERSION_CONFLICT: the storyboard changed since this client read it"
        )
        self.current_version_id = current_version_id
        self.base_version_id = base_version_id


class BaseVersionRequired(StoryboardError):
    """A write arrived without ``base_version_id`` (§5.4 compatibility error)."""


class RestoreNotAllowed(StoryboardError):
    """The target version's references are too broken to promote (§4.3)."""

    def __init__(self, state: str, issues: list[str]) -> None:
        super().__init__(
            f"the target version is {state} and cannot be restored as the current "
            "plan; open it read-only instead"
        )
        self.state = state
        self.issues = issues


@dataclass
class VersionView:
    """A version plus its board context — the read contract of §4.1."""

    board_id: str
    project_id: str
    current_version_id: str | None
    version_id: str
    version_no: int
    parent_version_id: str | None
    schema_version: str
    created_at: datetime
    actor: str
    reason: str
    brief: dict
    sections: list
    shots: list
    constraints: dict
    validation_summary: dict
    job_id: str | None = None
    restore_of_version_id: str | None = None

    @property
    def snapshot(self) -> StoryboardSnapshot:
        return StoryboardSnapshot.from_dict(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "brief": self.brief,
            "sections": self.sections,
            "shots": self.shots,
            "constraints": self.constraints,
            "validation_summary": self.validation_summary,
        }

    def to_api(self) -> dict[str, Any]:
        """The JSON body of the read endpoints (flat, per §4.1's field list)."""
        return {
            "board_id": self.board_id,
            "project_id": self.project_id,
            "current_version_id": self.current_version_id,
            "version_id": self.version_id,
            "version_no": self.version_no,
            "parent_version_id": self.parent_version_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "actor": self.actor,
            "reason": self.reason,
            "job_id": self.job_id,
            "restore_of_version_id": self.restore_of_version_id,
            "brief": self.brief,
            "sections": self.sections,
            "shots": self.shots,
            "constraints": self.constraints,
            "validation_summary": self.validation_summary,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _view(board: Storyboard, version: StoryboardVersion) -> VersionView:
    return VersionView(
        board_id=board.id,
        project_id=board.project_id,
        current_version_id=board.current_version_id,
        version_id=version.id,
        version_no=version.version_no,
        parent_version_id=version.parent_version_id,
        schema_version=version.schema_version,
        created_at=version.created_at,
        actor=version.actor,
        reason=version.reason,
        brief=version.brief or {},
        sections=version.sections or [],
        shots=version.shots or [],
        constraints=version.constraints or {},
        validation_summary=version.validation_summary or {},
        job_id=version.job_id,
        restore_of_version_id=version.restore_of_version_id,
    )


def _project_or_404(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise StoryboardNotFound(f"project {project_id} not found")
    return project


def _board_for(session: Session, project_id: str) -> Storyboard:
    board = session.scalars(
        select(Storyboard).where(Storyboard.project_id == project_id)
    ).first()
    if board is None:
        raise StoryboardNotFound(f"project {project_id} has no storyboard")
    return board


def _next_version_no(session: Session, board_id: str) -> int:
    current = session.scalar(
        select(func.max(StoryboardVersion.version_no)).where(
            StoryboardVersion.board_id == board_id
        )
    )
    return int(current or 0) + 1


# ---------------------------------------------------------------------------
# reference validation (§4.3, §7.6)
# ---------------------------------------------------------------------------

def validate_references(
    session: Session,
    *,
    project_id: str,
    shots: Iterable[ShotSnapshot],
) -> ValidationSummary:
    """Check each shot's Asset/Segment against the project's current state.

    Deliberately read-only and non-repairing: a problem becomes an *issue*, never
    a rewritten id. The snapshot keeps whatever it was, which is what lets a
    historical version stay truthful after a source is re-detected or deleted.
    """
    assets = {
        a.id: a
        for a in session.scalars(select(Asset).where(Asset.project_id == project_id)).all()
    }
    segments = {
        g.id: g
        for g in session.scalars(select(Segment).where(Segment.project_id == project_id)).all()
    }

    issues: list[ValidationIssue] = []
    for shot in shots:
        asset = assets.get(shot.asset_id)
        segment = segments.get(shot.segment_id)

        if asset is None:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="ASSET_MISSING",
                message=f"asset {shot.asset_id} is not in this project",
            ))
        if segment is None:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_MISSING",
                message=f"segment {shot.segment_id} is not in this project",
            ))
        if asset is not None and segment is not None and segment.asset_id != asset.id:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="ASSET_SEGMENT_MISMATCH",
                message=(
                    f"segment {segment.id} belongs to asset {segment.asset_id}, "
                    f"not {asset.id}"
                ),
            ))
        if segment is not None and getattr(segment, "status", "") == "needs_review":
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_NEEDS_REVIEW",
                message="the shot's source changed; its detection needs review",
            ))
        if segment is not None:
            # The shot must sit inside the segment it claims to come from; a
            # range crossing a cut is the audit's P1-05 defect rediscovered.
            if shot.source_in_sec < min(segment.start_sec, segment.end_sec) - 1e-3:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="INVALID_RANGE",
                    message=(
                        f"source_in_sec {shot.source_in_sec} is before the segment "
                        f"start {segment.start_sec}"
                    ),
                ))
            if shot.source_out_sec > segment.end_sec + 1e-3:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="OUT_OF_RANGE",
                    message=(
                        f"source_out_sec {shot.source_out_sec} is after the segment "
                        f"end {segment.end_sec}"
                    ),
                ))
        if shot.target_duration_sec > 0 and shot.source_out_sec - shot.source_in_sec > 0:
            if shot.target_duration_sec > (shot.source_out_sec - shot.source_in_sec) + 1e-3:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="DURATION_TOO_LONG",
                    message=(
                        "target_duration_sec exceeds the source range; a speed "
                        "change is not supported"
                    ),
                ))
        if shot.target_duration_sec < 0:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="INVALID_RANGE",
                message="target_duration_sec must be positive",
            ))
        if asset is not None:
            if shot.source_in_sec < 0:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="INVALID_RANGE",
                    message="source_in_sec is negative",
                ))
            if shot.source_out_sec <= shot.source_in_sec:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="INVALID_RANGE",
                    message="source_out_sec must be greater than source_in_sec",
                ))
            elif shot.source_out_sec > asset.duration_sec + 1e-3:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="OUT_OF_RANGE",
                    message=(
                        f"source_out_sec {shot.source_out_sec} exceeds the asset "
                        f"duration {asset.duration_sec}"
                    ),
                ))

    # Severity: a missing/mismatched reference cannot be executed at all, so it
    # is `invalid`; a needs-review or out-of-range shot is recoverable by a human
    # and stays `needs_review`. Both are surfaced; neither is silently fixed.
    hard = {"ASSET_MISSING", "SEGMENT_MISSING", "ASSET_SEGMENT_MISMATCH", "INVALID_RANGE"}
    if any(i.code in hard for i in issues):
        state = "invalid"
    elif issues:
        state = "needs_review"
    else:
        state = "ok"
    return ValidationSummary(state=state, issues=issues)


def _identity_signature(asset: Asset | None) -> str:
    """The source signature to record for a shot's asset.

    Reuses whatever the repository already carries on the asset. When the asset
    has none (typical of a legacy plan), this returns ``LEGACY_SIGNATURE`` rather
    than hashing anything: an invented signature would look like provenance.
    """
    if asset is None:
        return ""
    for attr in ("analyzed_source_signature", "quick_fingerprint"):
        value = getattr(asset, attr, "") or ""
        if value:
            return str(value)
    return LEGACY_SIGNATURE


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def load_storyboard_version(
    *, project_id: str, version_id: str | None = None, session: Session | None = None
) -> VersionView:
    """The adapter every reader (exports, roughcut) goes through (§5.4).

    ``version_id=None`` means "the current version". Passing an explicit id lets
    an export pin a historical version without disturbing the pointer.
    """

    def _run(s: Session) -> VersionView:
        _project_or_404(s, project_id)
        board = _board_for(s, project_id)
        target = version_id or board.current_version_id
        if not target:
            raise StoryboardNotFound("storyboard has no current version")
        version = s.get(StoryboardVersion, target)
        if version is None or version.board_id != board.id:
            raise StoryboardNotFound(f"version {target} not found in this project")
        return _view(board, version)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def get_current_version(*, project_id: str, session: Session | None = None) -> VersionView:
    """The project's current version. Creates an empty v1 if the board is new."""
    view = None
    try:
        view = load_storyboard_version(project_id=project_id, session=session)
    except StoryboardNotFound:
        view = None
    if view is not None:
        return view
    # A project with no board yet gets one with an empty initial version, so the
    # Export page has something lawful to read (§4.1) and a base to save against.
    return save_version(
        project_id=project_id,
        base_version_id=None,
        shots=[],
        actor="system",
        reason="initial",
        allow_initial=True,
        session=session,
    )


def get_version(
    *, board_id: str, version_id: str, project_id: str | None = None,
    session: Session | None = None,
) -> VersionView:
    """Read one version by id, optionally restricted to a project.

    A version that exists but belongs to another project raises the same
    ``StoryboardNotFound`` as a missing one, so ids cannot be probed (§4.1).
    """

    def _run(s: Session) -> VersionView:
        version = s.get(StoryboardVersion, version_id)
        if version is None or version.board_id != board_id:
            raise StoryboardNotFound(f"version {version_id} not found")
        board = s.get(Storyboard, board_id)
        if board is None:
            raise StoryboardNotFound(f"board {board_id} not found")
        if project_id is not None and board.project_id != project_id:
            raise StoryboardNotFound(f"version {version_id} not found")
        return _view(board, version)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_versions(
    *, project_id: str, session: Session | None = None
) -> list[VersionView]:
    """Version history, newest first (stable ordering, §4.1)."""

    def _run(s: Session) -> list[VersionView]:
        board = _board_for(s, project_id)
        rows = s.scalars(
            select(StoryboardVersion)
            .where(StoryboardVersion.board_id == board.id)
            .order_by(StoryboardVersion.version_no.desc())
        ).all()
        return [_view(board, v) for v in rows]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


@dataclass
class SourceRefView:
    shot_id: str
    asset_id: str
    segment_id: str
    source_signature: str


def get_version_source_refs(
    *, board_id: str, version_id: str, project_id: str | None = None,
    session: Session | None = None,
) -> list[SourceRefView]:
    """The derived index for one version, in snapshot order."""

    def _run(s: Session) -> list[SourceRefView]:
        version = s.get(StoryboardVersion, version_id)
        if version is None or version.board_id != board_id:
            raise StoryboardNotFound(f"version {version_id} not found")
        if project_id is not None:
            board = s.get(Storyboard, board_id)
            if board is None or board.project_id != project_id:
                raise StoryboardNotFound(f"version {version_id} not found")
        rows = s.scalars(
            select(StoryboardSourceRef).where(StoryboardSourceRef.version_id == version_id)
        ).all()
        order = {sid: i for i, sid in enumerate(
            [str(sh.get("shot_id")) for sh in (version.shots or [])]
        )}
        rows.sort(key=lambda r: order.get(r.shot_id, 10**6))
        return [SourceRefView(r.shot_id, r.asset_id, r.segment_id, r.source_signature)
                for r in rows]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

#: Legacy-only fields accepted from pre-TSB-366 clients and deliberately dropped.
#:
#: ``order`` is the important one: the old API accepted it and then ignored it,
#: because array position is the timeline order. Dropping it here keeps that
#: promise while the snapshot stays strict about genuinely unknown fields.
_LEGACY_ONLY_SHOT_FIELDS = frozenset({"order", "requested_order"})


def _renumber_timeline(shots: list[ShotSnapshot]) -> None:
    """Give the shots a contiguous timeline, in array order.

    Preserved from the pre-versioning behaviour: array position is the timeline
    order and ``timeline_start_sec`` is the running total of the planned
    durations. Computing it here (rather than trusting a submitted value) is what
    keeps "sorted by order but accumulated by input order" from reappearing — the
    audit's P1-05 defect.
    """
    running = 0.0
    for shot in shots:
        shot.timeline_start_sec = running
        running += float(shot.target_duration_sec or 0.0)


def _shots_from_input(shots: Iterable[dict | ShotSnapshot]) -> list[ShotSnapshot]:
    """Normalise caller input into validated ``ShotSnapshot`` objects."""
    out: list[ShotSnapshot] = []
    for index, item in enumerate(shots):
        if isinstance(item, ShotSnapshot):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise SnapshotError(f"shots[{index}] must be an object")
        payload = {
            k: v for k, v in item.items() if k not in _LEGACY_ONLY_SHOT_FIELDS
        }
        # Identity is required; a client that omits it gets a fresh stable id
        # rather than an index (which must never be an identity).
        if not payload.get("shot_id"):
            import uuid

            payload["shot_id"] = str(uuid.uuid4())
        try:
            out.append(ShotSnapshot.model_validate(payload))
        except Exception as exc:  # pydantic.ValidationError
            raise SnapshotError(f"shots[{index}]: {exc}") from None
    _renumber_timeline(out)
    return out


def _write_version_and_refs(
    session: Session,
    *,
    board: Storyboard,
    version_no: int,
    parent_version_id: str | None,
    snapshot: StoryboardSnapshot,
    actor: str,
    reason: str,
    job_id: str | None,
    restore_of_version_id: str | None,
    idempotency_key: str | None,
) -> StoryboardVersion:
    """INSERT the version row and its source refs (same transaction).

    Split out so the migration can be exercised with a failure injected between
    the board row and the version row (§7.2-8).
    """
    version = StoryboardVersion(
        board_id=board.id,
        version_no=version_no,
        parent_version_id=parent_version_id,
        schema_version=snapshot.schema_version or SNAPSHOT_SCHEMA_VERSION,
        brief=snapshot.brief.model_dump(),
        sections=[s.model_dump() for s in snapshot.sections],
        shots=[s.model_dump() for s in snapshot.shots],
        constraints=snapshot.constraints.model_dump(),
        validation_summary=snapshot.validation_summary.model_dump(),
        actor=actor,
        reason=reason,
        job_id=job_id,
        restore_of_version_id=restore_of_version_id,
        idempotency_key=idempotency_key,
    )
    session.add(version)
    session.flush()

    assets = {
        a.id: a
        for a in session.scalars(
            select(Asset).where(Asset.project_id == board.project_id)
        ).all()
    }
    for shot in snapshot.shots:
        session.add(StoryboardSourceRef(
            version_id=version.id,
            shot_id=shot.shot_id,
            asset_id=shot.asset_id,
            segment_id=shot.segment_id,
            source_signature=shot.source_signature
            or _identity_signature(assets.get(shot.asset_id)),
        ))
    session.flush()
    return version


def save_version(
    *,
    project_id: str,
    base_version_id: str | None,
    shots: Iterable[dict | ShotSnapshot] | None = None,
    snapshot: StoryboardSnapshot | None = None,
    actor: str = "user",
    reason: str = "manual_edit",
    job_id: str | None = None,
    idempotency_key: str | None = None,
    restore_of_version_id: str | None = None,
    sections: list[SectionSnapshot] | None = None,
    brief: BriefSnapshot | None = None,
    constraints: ConstraintsSnapshot | None = None,
    allow_initial: bool = False,
    session: Session | None = None,
) -> VersionView:
    """Create a new version from ``snapshot`` (or from ``shots``).

    Concurrency (§4.2): the base-version comparison, the ``version_no``
    allocation, the INSERT and the pointer update all happen inside one
    ``BEGIN IMMEDIATE`` transaction, so two clients on the same base cannot both
    succeed. The second raises :class:`VersionConflict`.

    ``allow_initial`` is the one road through a ``None`` base: it is used only to
    create version 1 of a brand-new board.
    """
    if actor not in ACTORS:
        raise StoryboardError(f"unknown actor {actor!r}")
    if reason not in REASONS:
        raise StoryboardError(f"unknown reason {reason!r}")

    def _run(s: Session) -> VersionView:
        # Writer reservation first: this is the project-lock-then-write ordering
        # the rest of the app uses (AGENTS.md §5.4).
        begin_write(s)
        _project_or_404(s, project_id)

        board = s.scalars(
            select(Storyboard).where(Storyboard.project_id == project_id)
        ).first()

        # Build the document *before* touching any row, so an invalid body
        # writes nothing at all. (Named `document` rather than `snapshot`
        # because the parameter of the same name is in scope here.)
        if snapshot is None:
            parsed_shots = _shots_from_input(shots or [])
            document = StoryboardSnapshot(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                brief=brief or BriefSnapshot(),
                sections=list(sections or []),
                shots=parsed_shots,
                constraints=constraints or ConstraintsSnapshot(),
            )
        else:
            document = snapshot
        validation = validate_references(s, project_id=project_id, shots=document.shots)
        document.validation_summary = validation

        if board is None:
            board = Storyboard(project_id=project_id, title="")
            s.add(board)
            s.flush()
            current_version_id = None
        else:
            current_version_id = board.current_version_id

        # Retry of the same request: return the version this key already made.
        if idempotency_key:
            existing = s.scalars(
                select(StoryboardVersion).where(
                    StoryboardVersion.board_id == board.id,
                    StoryboardVersion.idempotency_key == idempotency_key,
                )
            ).first()
            if existing is not None:
                return _view(board, existing)

        if current_version_id is None:
            if not allow_initial:
                raise BaseVersionRequired(
                    "base_version_id is required to save a storyboard"
                )
        elif base_version_id is None:
            raise BaseVersionRequired(
                "base_version_id is required: send the version you read"
            )
        elif base_version_id != current_version_id:
            raise VersionConflict(current_version_id, base_version_id)

        version = _write_version_and_refs(
            s, board=board,
            version_no=_next_version_no(s, board.id),
            parent_version_id=current_version_id,
            snapshot=document,
            actor=actor, reason=reason, job_id=job_id,
            restore_of_version_id=restore_of_version_id,
            idempotency_key=idempotency_key,
        )
        board.current_version_id = version.id
        s.flush()
        s.refresh(board)
        s.refresh(version)
        return _view(board, version)

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def restore_version(
    *,
    project_id: str,
    target_version_id: str,
    base_version_id: str | None,
    actor: str = "user",
    job_id: str | None = None,
    idempotency_key: str | None = None,
    session: Session | None = None,
) -> VersionView:
    """Restore ``target_version_id`` by appending a new version (§4.3).

    The old versions are untouched: this copies the target's snapshot into a new
    row whose parent is the version that was current a moment ago.
    """
    view = get_version(
        board_id=_board_id_for(project_id, session),
        version_id=target_version_id,
        project_id=project_id,
        session=session,
    )
    snapshot = StoryboardSnapshot.from_dict(view.to_dict())

    return save_version(
        project_id=project_id,
        base_version_id=base_version_id,
        snapshot=snapshot,
        actor=actor,
        reason="restore",
        job_id=job_id,
        idempotency_key=idempotency_key,
        restore_of_version_id=target_version_id,
        session=session,
    )


def _board_id_for(project_id: str, session: Session | None) -> str:
    def _run(s: Session) -> str:
        _project_or_404(s, project_id)
        return _board_for(s, project_id).id

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


# ---------------------------------------------------------------------------
# exports (single-version consistency, §5.3)
# ---------------------------------------------------------------------------

@dataclass
class ExportContext:
    """What an exporter needs: the pinned version plus its resolved sources."""

    view: VersionView
    shots: list[dict]
    assets: dict[str, Asset]
    segments: dict[str, Segment]


def load_export_context(
    *, project_id: str, version_id: str | None = None
) -> ExportContext:
    """Read one version and resolve its shot sources for an exporter.

    All of JSON / CSV / OTIO / roughcut call this, so a single export can never
    mix rows from two versions.
    """
    with session_scope() as s:
        view = load_storyboard_version(project_id=project_id, version_id=version_id, session=s)
        assets = {
            a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()
        }
        segments = {
            g.id: g
            for g in s.scalars(select(Segment).where(Segment.project_id == project_id)).all()
        }
        return ExportContext(view=view, shots=list(view.shots), assets=assets, segments=segments)
