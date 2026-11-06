"""TSB-367 — the pure operation executor (§6.1, §6.2 step 8–9).

Given a snapshot, a batch of already-schema-validated operations and the resolved
project media, this module returns a **new** snapshot plus the server-computed
diff and validation summary. It is a pure function on purpose:

* it never opens a session, never writes a file, never calls a model;
* it never mutates its input, so the base version stays byte-identical no matter
  what the batch does;
* the same input yields the same output, so a replay is comparable.

That purity is what makes "a failed batch writes nothing" a structural fact
rather than a promise: there is nowhere for a partial result to live. The
caller (:mod:`app.services.plan_operation_service`) is the only thing that
persists anything, and it does so in one transaction.

Lock semantics follow the §5.3 matrix exactly; see :data:`LOCK_MATRIX`. A test
(`test_tsb367_lock_matrix.py`) walks every cell, so the matrix cannot drift away
from the code without a failure.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from .storyboard_schema import (
    LOCK_ALL,
    LOCK_DIMENSIONS,
    LOCK_POSITION,
    LOCK_RANGE,
    LOCK_REFERENCE,
    SectionSnapshot,
    ShotSnapshot,
    StoryboardSnapshot,
    ValidationIssue,
    ValidationSummary,
)

#: §5.3 lock matrix: (lock dimension) -> operations it forbids.
#:
#: ``update_constraints`` is allowed under every dimension because it never
#: changes a shot; the task book's matrix says the same and it would be a bug to
#: let a constraint edit fail merely because some shot is locked.
LOCK_MATRIX: dict[str, frozenset[str]] = {
    LOCK_REFERENCE: frozenset({"replace_shot", "remove_shot"}),
    LOCK_RANGE: frozenset({"trim_shot", "replace_shot", "remove_shot"}),
    LOCK_POSITION: frozenset({"move_shot", "remove_shot"}),
}

#: What ``all`` forbids: the union of the concrete dimensions.
LOCK_ALL_FORBIDS = frozenset().union(*LOCK_MATRIX.values())


class OperationExecutionError(Exception):
    """One operation could not be applied; the whole batch is abandoned.

    ``code`` is a stable machine-readable reason (``LOCK_CONFLICT``,
    ``SHOT_NOT_FOUND``, ``INVALID_REFERENCE``, ``INVALID_RANGE``,
    ``CONSTRAINT_CONFLICT`` …) so the API layer can map it onto the documented
    HTTP table without parsing prose. ``index`` names the offending operation so
    a client can point at what it sent.
    """

    def __init__(self, code: str, message: str, *, index: int = -1,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.index = index
        self.detail = dict(detail or {})


@dataclass(frozen=True)
class ProjectContext:
    """The resolved project media an operation is allowed to reference.

    Built by the service layer from the database, and passed in as plain data so
    the executor never has to know what an ORM is. Reading it here rather than
    inside the executor also means a test can describe a whole project in a dict.
    """

    project_id: str
    #: asset_id -> {duration_sec, source_signature, status?}
    assets: dict[str, dict[str, Any]]
    #: segment_id -> {asset_id, start_sec, end_sec, status}
    segments: dict[str, dict[str, Any]]


@dataclass
class ExecutionResult:
    """What the executor produced. Nothing here has been persisted yet."""

    snapshot: StoryboardSnapshot
    diff: dict[str, Any]
    validation: ValidationSummary
    created_shot_ids: list[str] = field(default_factory=list)
    affected_shot_ids: list[str] = field(default_factory=list)
    affected_section_ids: list[str] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# lock helpers
# ---------------------------------------------------------------------------

def forbidden_operations(locked: Iterable[str] | None) -> frozenset[str]:
    """The operations a shot's lock set prohibits (§5.3)."""
    dims = set(locked or ())
    if not dims:
        return frozenset()
    if LOCK_ALL in dims:
        return LOCK_ALL_FORBIDS
    forbidden: set[str] = set()
    for dim in dims:
        forbidden |= LOCK_MATRIX.get(dim, frozenset())
    return frozenset(forbidden)


def _check_lock(shot: ShotSnapshot, operation: str, index: int) -> None:
    if operation in forbidden_operations(shot.locked):
        raise OperationExecutionError(
            "LOCK_CONFLICT",
            f"shot {shot.shot_id} is locked ({', '.join(shot.locked or [])}); "
            f"{operation} is not permitted",
            index=index,
            detail={"shot_id": shot.shot_id, "operation": operation,
                    "locked": list(shot.locked or [])},
        )


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _find_shot(shots: list[ShotSnapshot], shot_id: str, index: int) -> ShotSnapshot:
    for shot in shots:
        if shot.shot_id == shot_id:
            return shot
    raise OperationExecutionError(
        "SHOT_NOT_FOUND",
        f"shot {shot_id} is not in this version",
        index=index, detail={"shot_id": shot_id},
    )


def _find_section(sections: list[SectionSnapshot], section_id: str,
                  index: int) -> SectionSnapshot:
    for section in sections:
        if section.section_id == section_id:
            return section
    raise OperationExecutionError(
        "SECTION_NOT_FOUND",
        f"section {section_id} is not in this version",
        index=index, detail={"section_id": section_id},
    )


#: Name of the server-side field that carries a newly minted identity. It is
#: written into `PlanOperation.operations` at proposal time and read back at
#: apply time, never accepted from a client (see `plan_operations_schema`).
RESOLVED_ID_FIELD = "resolved_shot_id"


def _resolved_identity(op: dict, snapshot: StoryboardSnapshot, index: int) -> str:
    """The identity for an insert/replace — decided once, at proposal time (R1-01).

    The first execution (`propose`) mints an id and the caller persists it.
    Every later execution (`apply`) **must** find that id and reuse it; a
    proposal without one is a proposal this build cannot honour, so it fails
    loudly instead of minting a different identity than the user was shown.
    """
    recorded = op.get(RESOLVED_ID_FIELD)
    if recorded:
        if not isinstance(recorded, str):
            raise OperationExecutionError(
                "INVALID_IDENTITY",
                "resolved shot identity must be a string",
                index=index,
            )
        if any(s.shot_id == recorded for s in snapshot.shots):
            # The recorded id already exists in this snapshot: reusing it would
            # silently overwrite another shot.
            raise OperationExecutionError(
                "DUPLICATE_SHOT_ID",
                f"recorded identity {recorded} already exists in this plan",
                index=index, detail={"shot_id": recorded},
            )
        return recorded

    if op.get("_identity_required"):
        raise OperationExecutionError(
            "MISSING_SHOT_IDENTITY",
            "this proposal has no recorded shot identity; it was created by an "
            "older build and cannot be applied safely — please propose again",
            index=index,
        )

    # First execution for this batch (proposal): mint and let the caller persist.
    return _new_shot_id({s.shot_id for s in snapshot.shots})


def _new_shot_id(existing: set[str]) -> str:
    """Mint an identity that cannot collide with anything already in the plan."""
    while True:
        candidate = str(uuid.uuid4())
        if candidate not in existing:
            return candidate


def _validate_source(context: ProjectContext, *, asset_id: str, segment_id: str,
                     source_in: float, source_out: float, target: float,
                     index: int) -> None:
    """Project-ownership and range checks for a shot's media (§5.1 items 6–13).

    Rejects rather than repairs: an unknown asset or a segment belonging to a
    different asset is never substituted or re-bound (task book §5.2).
    """
    asset = context.assets.get(asset_id)
    if asset is None:
        raise OperationExecutionError(
            "INVALID_REFERENCE",
            f"asset {asset_id} does not exist in this project",
            index=index, detail={"asset_id": asset_id},
        )
    segment = context.segments.get(segment_id)
    if segment is None:
        raise OperationExecutionError(
            "INVALID_REFERENCE",
            f"segment {segment_id} does not exist in this project",
            index=index, detail={"segment_id": segment_id},
        )
    if segment.get("asset_id") != asset_id:
        raise OperationExecutionError(
            "INVALID_REFERENCE",
            f"segment {segment_id} belongs to asset {segment.get('asset_id')}, "
            f"not {asset_id}",
            index=index,
            detail={"asset_id": asset_id, "segment_id": segment_id},
        )
    if segment.get("status") == "needs_review":
        # Recorded, not fatal: the plan may keep the shot and the summary says so.
        return

    if source_in < 0 or source_out <= source_in:
        raise OperationExecutionError(
            "INVALID_RANGE",
            "source_out_sec must be greater than source_in_sec and both >= 0",
            index=index,
        )
    seg_start = float(segment.get("start_sec") or 0.0)
    seg_end = float(segment.get("end_sec") or 0.0)
    if source_in < seg_start - 1e-3 or source_out > seg_end + 1e-3:
        raise OperationExecutionError(
            "INVALID_RANGE",
            f"range [{source_in}, {source_out}] is outside segment "
            f"[{seg_start}, {seg_end}]",
            index=index,
            detail={"segment_id": segment_id},
        )
    duration = float(asset.get("duration_sec") or 0.0)
    if duration and source_out > duration + 1e-3:
        raise OperationExecutionError(
            "INVALID_RANGE",
            f"range exceeds the asset duration {duration}",
            index=index, detail={"asset_id": asset_id},
        )
    if source_out - source_in + 1e-3 < target:
        raise OperationExecutionError(
            "INVALID_RANGE",
            "target_duration_sec cannot exceed the source range (no speed change)",
            index=index,
        )


def _renumber(shots: list[ShotSnapshot]) -> None:
    """Give the shots a contiguous timeline in array order.

    Array position is the timeline order (the documented rule since TSB-366), so
    this is recomputed rather than trusted from the request.
    """
    running = 0.0
    for shot in shots:
        shot.timeline_start_sec = running
        running += float(shot.target_duration_sec or 0.0)


def _index_of(shots: list[ShotSnapshot], shot_id: str) -> int:
    for position, shot in enumerate(shots):
        if shot.shot_id == shot_id:
            return position
    raise OperationExecutionError("SHOT_NOT_FOUND", f"shot {shot_id} is not present")


# ---------------------------------------------------------------------------
# individual operations
# ---------------------------------------------------------------------------

def _apply_move_shot(shots: list[ShotSnapshot], op: dict, index: int,
                     sections: list[SectionSnapshot]) -> None:
    """Move a shot, optionally into another section (R1-06).

    Contract for the anchor:

    * ``after_shot_id == shot_id`` / ``before_shot_id == shot_id`` is a
      **positional no-op** — the request is already satisfied. It is not an error
      (refusing would be hostile) and it must never raise: the earlier code
      removed the shot first and then looked it up as the anchor, which crashed
      with ``SHOT_NOT_FOUND`` from ``_index_of``.
    * A self-anchor may still carry a ``section_id``: the position does not
      change, but the section assignment is real intent and is applied.

    ``section_id`` is written onto the shot when supplied, and validated against
    the snapshot's sections **before** any mutation, so the result can never hold
    a dangling section reference and a failed move leaves nothing half-applied.
    """
    shot = _find_shot(shots, op["shot_id"], index)
    _check_lock(shot, "move_shot", index)

    anchor_id = op.get("after_shot_id") or op.get("before_shot_id")
    anchor = _find_shot(shots, anchor_id, index)

    target_section = op.get("section_id")
    if target_section:
        _find_section(sections, target_section, index)

    if anchor.shot_id == shot.shot_id:
        # Positional no-op; only the section assignment can change.
        if target_section:
            shot.section_id = target_section
        return

    shots.remove(shot)
    anchor_position = _index_of(shots, anchor.shot_id)
    target = anchor_position + 1 if op.get("after_shot_id") else anchor_position
    if target_section:
        shot.section_id = target_section
    shots.insert(target, shot)


def _apply_move_section(sections: list[SectionSnapshot], op: dict, index: int) -> None:
    section = _find_section(sections, op["section_id"], index)
    anchor_id = op.get("after_section_id") or op.get("before_section_id")
    anchor = _find_section(sections, anchor_id, index)

    sections.remove(section)
    anchor_position = next(
        i for i, s in enumerate(sections) if s.section_id == anchor.section_id
    )
    target = anchor_position + 1 if op.get("after_section_id") else anchor_position
    sections.insert(target, section)


def _apply_insert_shot(snapshot: StoryboardSnapshot, op: dict, context: ProjectContext,
                       index: int) -> str:
    _validate_source(
        context,
        asset_id=op["asset_id"], segment_id=op["segment_id"],
        source_in=float(op["source_in_sec"]), source_out=float(op["source_out_sec"]),
        target=float(op.get("target_duration_sec") or 0.0), index=index,
    )
    if op.get("section_id"):
        _find_section(snapshot.sections, op["section_id"], index)

    shot_id = _resolved_identity(op, snapshot, index)
    span = float(op["source_out_sec"]) - float(op["source_in_sec"])
    target = float(op.get("target_duration_sec") or 0.0) or span

    shot = ShotSnapshot(
        shot_id=shot_id,
        section_id=op.get("section_id"),
        asset_id=op["asset_id"], segment_id=op["segment_id"],
        source_in_sec=float(op["source_in_sec"]),
        source_out_sec=float(op["source_out_sec"]),
        target_duration_sec=target,
        story_role=str(op.get("story_role") or "")[:32],
        review_status="pending",
        reason=str(op.get("reason") or "") or None,
    )

    anchor_id = op.get("after_shot_id") or op.get("before_shot_id")
    if anchor_id:
        anchor = _find_shot(snapshot.shots, anchor_id, index)
        position = _index_of(snapshot.shots, anchor.shot_id)
        target_position = position + 1 if op.get("after_shot_id") else position
    else:
        target_position = len(snapshot.shots)
    snapshot.shots.insert(target_position, shot)
    return shot_id


def _apply_remove_shot(shots: list[ShotSnapshot], op: dict, index: int) -> None:
    shot = _find_shot(shots, op["shot_id"], index)
    _check_lock(shot, "remove_shot", index)
    shots.remove(shot)


def _apply_replace_shot(snapshot: StoryboardSnapshot, op: dict,
                        context: ProjectContext, index: int) -> str:
    target_shot = _find_shot(snapshot.shots, op["target_shot_id"], index)
    _check_lock(target_shot, "replace_shot", index)

    _validate_source(
        context,
        asset_id=op["asset_id"], segment_id=op["segment_id"],
        source_in=float(op["source_in_sec"]), source_out=float(op["source_out_sec"]),
        target=float(op.get("target_duration_sec") or 0.0), index=index,
    )

    position = _index_of(snapshot.shots, target_shot.shot_id)
    shot_id = _resolved_identity(op, snapshot, index)
    span = float(op["source_out_sec"]) - float(op["source_in_sec"])
    target = float(op.get("target_duration_sec") or 0.0) or span

    # In-place at the same position, but with a *new* identity: the new media must
    # not inherit the old shot's traceability.
    snapshot.shots[position] = ShotSnapshot(
        shot_id=shot_id,
        section_id=target_shot.section_id,
        asset_id=op["asset_id"], segment_id=op["segment_id"],
        source_in_sec=float(op["source_in_sec"]),
        source_out_sec=float(op["source_out_sec"]),
        target_duration_sec=target,
        story_role=str(op.get("story_role") or target_shot.story_role or "")[:32],
        review_status="pending",
        reason=str(op.get("reason") or "") or None,
    )
    return shot_id


def _apply_trim_shot(shots: list[ShotSnapshot], op: dict, context: ProjectContext,
                     index: int) -> None:
    shot = _find_shot(shots, op["shot_id"], index)
    _check_lock(shot, "trim_shot", index)

    source_in = float(op["source_in_sec"])
    source_out = float(op["source_out_sec"])
    # A trim cannot jump to a different source: the segment is fixed by the shot.
    _validate_source(
        context,
        asset_id=shot.asset_id, segment_id=shot.segment_id,
        source_in=source_in, source_out=source_out,
        target=0.0, index=index,
    )
    shot.source_in_sec = source_in
    shot.source_out_sec = source_out
    explicit = op.get("target_duration_sec")
    if explicit is None:
        shot.target_duration_sec = source_out - source_in
    else:
        shot.target_duration_sec = float(explicit)
        if shot.target_duration_sec > (source_out - source_in) + 1e-3:
            raise OperationExecutionError(
                "INVALID_RANGE",
                "target_duration_sec cannot exceed the trimmed range",
                index=index,
            )


def _apply_lock_shot(shots: list[ShotSnapshot], op: dict, index: int) -> None:
    """Lock or unlock lock dimensions (R1-07).

    Stored form is **canonical**: either ``None`` (unlocked), the single element
    ``["all"]``, or a sorted, unique list of concrete dimensions.

    Unlocking one dimension while ``all`` is set used to be silently ignored —
    ``current -= {"range"}`` left ``{"all"}`` untouched, so the other dimensions
    could never be released. The fix expands ``all`` into the concrete dimensions
    first, removes the requested one, and re-canonicalises: if everything is still
    locked it collapses back to ``["all"]``.
    """
    shot = _find_shot(shots, op["shot_id"], index)
    dims = {str(d).lower() for d in (op.get("dimensions") or [LOCK_ALL])}
    concrete = {d for d in LOCK_DIMENSIONS if d != LOCK_ALL}

    # Normalise the current state to a concrete set first.
    current = set(shot.lock_set())  # already expands `all`

    if op.get("locked"):
        if LOCK_ALL in dims:
            current |= concrete
        else:
            current |= (dims & concrete)
    else:
        if LOCK_ALL in dims:
            current = set()
        else:
            current -= (dims & concrete)

    shot.locked = _canonical_lock(current)


def _canonical_lock(dims: set[str]) -> list[str] | None:
    """Canonical stored form: ``None`` / ``["all"]`` / sorted concrete list."""
    concrete = {d for d in LOCK_DIMENSIONS if d != LOCK_ALL}
    effective = dims & concrete
    if not effective:
        return None
    if effective == concrete:
        return [LOCK_ALL]
    return sorted(effective)


def _apply_update_constraints(snapshot: StoryboardSnapshot, op: dict, index: int) -> list[ValidationIssue]:
    """Replace the constraints block; never edits shots (§4.2 G)."""
    hard = op.get("hard") or {}
    soft = op.get("soft") or {}

    snapshot.constraints.must_include = list(hard.get("must_include") or [])
    snapshot.constraints.exclude = list(hard.get("exclude") or [])
    snapshot.constraints.max_total_duration_sec = hard.get("max_total_duration_sec")

    conflicts: list[ValidationIssue] = []
    overlap = set(snapshot.constraints.must_include) & set(snapshot.constraints.exclude)
    if overlap:
        conflicts.append(ValidationIssue(
            shot_id="", code="CONSTRAINT_CONFLICT",
            message=(
                "a role cannot be both required and excluded: "
                + ", ".join(sorted(overlap))
            ),
        ))

    total = sum(float(s.target_duration_sec or 0.0) for s in snapshot.shots)
    limit = snapshot.constraints.max_total_duration_sec
    if limit is not None and total > float(limit) + 1e-6:
        conflicts.append(ValidationIssue(
            shot_id="", code="CONSTRAINT_CONFLICT",
            message=f"the plan is {total:.2f}s but the limit is {float(limit):.2f}s",
        ))

    # Soft preferences are recorded on the brief so a later reader can see what
    # was asked for; they never fail a save.
    if soft:
        brief = snapshot.brief.model_dump()
        brief["notes"] = json_note(soft)
        from .storyboard_schema import BriefSnapshot

        snapshot.brief = BriefSnapshot.model_validate(brief)
    return conflicts


def json_note(payload: dict[str, Any]) -> str:
    """A compact, human-readable rendering of soft preferences."""
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _shot_brief(shot: ShotSnapshot) -> dict[str, Any]:
    return {
        "shot_id": shot.shot_id,
        "asset_id": shot.asset_id,
        "segment_id": shot.segment_id,
        "source_in_sec": shot.source_in_sec,
        "source_out_sec": shot.source_out_sec,
        "target_duration_sec": shot.target_duration_sec,
        "story_role": shot.story_role,
        "locked": list(shot.locked or []),
    }


def _build_diff(before: StoryboardSnapshot, after: StoryboardSnapshot,
                ops: list[dict]) -> dict[str, Any]:
    """A server-computed summary of what changed.

    Structural and deterministic: it reports *what* changed, never why (reasons
    are user text, not machine inference). A client never supplies this (§4.1).
    """
    before_by_id = {s.shot_id: s for s in before.shots}
    after_by_id = {s.shot_id: s for s in after.shots}

    added = [sid for sid in after_by_id if sid not in before_by_id]
    removed = [sid for sid in before_by_id if sid not in after_by_id]
    reordered = [
        {"from": before_by_id[sid].timeline_start_sec,
         "to": after_by_id[sid].timeline_start_sec,
         "shot_id": sid}
        for sid in after_by_id
        if sid in before_by_id
        and before_by_id[sid].timeline_start_sec != after_by_id[sid].timeline_start_sec
    ]

    trimmed: list[dict[str, Any]] = []
    locked: list[dict[str, Any]] = []
    replaced: list[dict[str, Any]] = []
    for sid, shot in after_by_id.items():
        old = before_by_id.get(sid)
        if old is None:
            continue
        if (old.source_in_sec, old.source_out_sec,
                old.target_duration_sec) != (
                shot.source_in_sec, shot.source_out_sec, shot.target_duration_sec):
            trimmed.append({
                "shot_id": sid,
                "before": {"in": old.source_in_sec, "out": old.source_out_sec,
                           "duration": old.target_duration_sec},
                "after": {"in": shot.source_in_sec, "out": shot.source_out_sec,
                          "duration": shot.target_duration_sec},
            })
        if list(old.locked or []) != list(shot.locked or []):
            locked.append({"shot_id": sid, "before": list(old.locked or []),
                           "after": list(shot.locked or [])})

    # A replace pairs the old shot with the identity recorded for THIS operation.
    # Guessing the partner from `asset_id` is wrong the moment a batch replaces two
    # shots with media from the same asset (R1-01): both would resolve to the same
    # id. The recorded identity is the only reliable link.
    for op in ops:
        if op.get("type") == "replace_shot":
            replaced.append({
                "old_shot_id": op.get("target_shot_id"),
                "new_shot_id": op.get(RESOLVED_ID_FIELD),
                "new_asset_id": op.get("asset_id"),
                "new_segment_id": op.get("segment_id"),
            })

    sections_before = [s.section_id for s in before.sections]
    sections_after = [s.section_id for s in after.sections]

    return {
        "shots": {
            "added": added,
            "removed": removed,
            "reordered": reordered,
            "trimmed": trimmed,
            "locked": locked,
            "replaced": replaced,
        },
        "sections": {
            "added": [s for s in sections_after if s not in sections_before],
            "removed": [s for s in sections_before if s not in sections_after],
            "reordered": sections_before != sections_after,
        },
        "constraints": {
            "changed": before.constraints.model_dump() != after.constraints.model_dump(),
            "before": before.constraints.model_dump(),
            "after": after.constraints.model_dump(),
        },
        "totals": {
            "shots_before": len(before.shots),
            "shots_after": len(after.shots),
            "duration_before": round(sum(s.target_duration_sec for s in before.shots), 6),
            "duration_after": round(sum(s.target_duration_sec for s in after.shots), 6),
        },
    }


def _summarise(diff: dict[str, Any]) -> str:
    parts: list[str] = []
    shots = diff["shots"]
    if shots["added"]:
        parts.append(f"新增 {len(shots['added'])} 个镜头")
    if shots["removed"]:
        parts.append(f"移除 {len(shots['removed'])} 个镜头")
    if shots["trimmed"]:
        parts.append(f"裁剪 {len(shots['trimmed'])} 个镜头")
    if shots["locked"]:
        parts.append(f"锁定变更 {len(shots['locked'])} 个镜头")
    if shots["reordered"]:
        parts.append(f"重排 {len(shots['reordered'])} 个镜头")
    if diff["constraints"]["changed"]:
        parts.append("更新方案约束")
    return "；".join(parts) or "方案无变化"


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def execute_operations(
    *,
    snapshot: StoryboardSnapshot,
    operations: Iterable[dict[str, Any]],
    context: ProjectContext,
) -> ExecutionResult:
    """Apply a whole batch to a copy of ``snapshot`` and report the result.

    Everything is applied to an in-memory copy; the caller's snapshot is never
    touched, and any failure discards the copy. That is the batch atomicity rule
    of §4.4 expressed structurally.
    """
    operations = list(operations)
    working = copy.deepcopy(snapshot)
    before = copy.deepcopy(snapshot)

    created: list[str] = []
    affected_shots: list[str] = []
    affected_sections: list[str] = []
    extra_issues: list[ValidationIssue] = []

    for index, op in enumerate(operations):
        op_type = op.get("type")
        try:
            if op_type == "move_shot":
                _apply_move_shot(working.shots, op, index, working.sections)
                affected_shots.append(op["shot_id"])
            elif op_type == "move_section":
                _apply_move_section(working.sections, op, index)
                affected_sections.append(op["section_id"])
            elif op_type == "insert_shot":
                new_id = _apply_insert_shot(working, op, context, index)
                created.append(new_id)
                affected_shots.append(new_id)
                if op.get("section_id"):
                    affected_sections.append(op["section_id"])
            elif op_type == "remove_shot":
                _apply_remove_shot(working.shots, op, index)
                affected_shots.append(op["shot_id"])
            elif op_type == "replace_shot":
                new_id = _apply_replace_shot(working, op, context, index)
                created.append(new_id)
                affected_shots.extend([op["target_shot_id"], new_id])
            elif op_type == "trim_shot":
                _apply_trim_shot(working.shots, op, context, index)
                affected_shots.append(op["shot_id"])
            elif op_type == "lock_shot":
                _apply_lock_shot(working.shots, op, index)
                affected_shots.append(op["shot_id"])
            elif op_type == "update_constraints":
                extra_issues.extend(_apply_update_constraints(working, op, index))
            else:  # pragma: no cover - the schema already rejects unknown types
                raise OperationExecutionError(
                    "UNKNOWN_OPERATION", f"unknown operation type {op_type!r}", index=index
                )
        except OperationExecutionError as exc:
            # Attach the batch position so the API can point at the bad element.
            if exc.index < 0:
                exc.index = index
            raise

    _renumber(working.shots)

    # Final whole-snapshot validation (§4.4: 批次结束后对最终快照再做完整校验).
    issues, state = _validate_final(working, context, extra_issues)
    working.validation_summary = ValidationSummary(state=state, issues=issues)

    diff = _build_diff(before, working, operations)
    return ExecutionResult(
        snapshot=working,
        diff=diff,
        validation=working.validation_summary,
        created_shot_ids=created,
        affected_shot_ids=sorted(set(affected_shots)),
        affected_section_ids=sorted(set(affected_sections)),
        summary=_summarise(diff),
    )


def _validate_final(snapshot: StoryboardSnapshot, context: ProjectContext,
                    extra: list[ValidationIssue]) -> tuple[list[ValidationIssue], str]:
    """Re-check the *whole* final snapshot, not just what the batch touched.

    A batch can leave the plan in a state no individual operation could detect —
    for instance a section emptied by a move, or a shot whose segment went away
    while the batch was running. This is the last line of defence before a version
    is written.
    """
    issues: list[ValidationIssue] = list(extra)
    seen: set[str] = set()
    section_ids = {s.section_id for s in snapshot.sections}

    for shot in snapshot.shots:
        # 1. identity is unique
        if shot.shot_id in seen:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="DUPLICATE_SHOT_ID",
                message="two shots share the same shot_id",
            ))
        seen.add(shot.shot_id)

        # 2. the section exists (an absent section is allowed: unfiled shot)
        if shot.section_id and shot.section_id not in section_ids:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="INVALID_SECTION",
                message=f"section {shot.section_id} does not exist",
            ))

        # 3-4. the asset and the segment both belong to THIS project
        asset = context.assets.get(shot.asset_id)
        if asset is None:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="ASSET_MISSING",
                message=f"asset {shot.asset_id} is not in this project",
            ))

        segment = context.segments.get(shot.segment_id)
        if segment is None:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_MISSING",
                message=f"segment {shot.segment_id} is not in this project",
            ))
            continue

        # 5. the segment belongs to the shot's asset
        if asset is not None and segment.get("asset_id") != shot.asset_id:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="ASSET_SEGMENT_MISMATCH",
                message=(
                    f"segment {shot.segment_id} belongs to asset "
                    f"{segment.get('asset_id')}, not {shot.asset_id}"
                ),
            ))

        # 10. a changed source that needs review is reported, not hidden
        if segment.get("status") == "needs_review":
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_NEEDS_REVIEW",
                message="the shot's source changed and needs review",
            ))

        # 7-8. the range is valid and inside the segment
        start = float(segment.get("start_sec") or 0.0)
        end = float(segment.get("end_sec") or 0.0)
        if shot.source_in_sec < 0 or shot.source_out_sec <= shot.source_in_sec:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="OUT_OF_RANGE",
                message="source_in_sec / source_out_sec are not a valid range",
            ))
        elif shot.source_in_sec < start - 1e-3 or shot.source_out_sec > end + 1e-3:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="OUT_OF_RANGE",
                message="the shot range is outside its segment",
            ))

        # 9. the planned duration cannot exceed the available range
        if asset is not None:
            available = float(segment.get("end_sec") or 0.0) - \
                float(segment.get("start_sec") or 0.0)
            if shot.target_duration_sec > available + 1e-3:
                issues.append(ValidationIssue(
                    shot_id=shot.shot_id, code="DURATION_EXCEEDS_SOURCE",
                    message="the planned duration exceeds the available source span",
                ))

    # R1-08: section order is metadata; a timeline that interleaves sections is
    # legal but worth flagging so the UI does not mistake section order for the
    # edit order.
    if _sections_discontiguous(snapshot):
        issues.append(ValidationIssue(
            shot_id="", code="SECTIONS_DISCONTIGUOUS",
            message=(
                "镜头在时间线上跨章节交错；章节顺序仅为元数据，实际剪辑顺序以时间线为准"
            ),
        ))

    hard = {
        "DUPLICATE_SHOT_ID", "SEGMENT_MISSING", "INVALID_SECTION",
        "ASSET_MISSING", "ASSET_SEGMENT_MISMATCH",
    }
    if any(i.code in hard for i in issues):
        return issues, "invalid"
    if issues:
        return issues, "needs_review"
    return issues, "ok"


def _sections_discontiguous(snapshot: StoryboardSnapshot) -> bool:
    """True when the same section appears in more than one run of shots.

    Section order does **not** drive the timeline (documented contract): the shots
    array is the edit order. This check only reports the situation so nobody
    mistakes one for the other.
    """
    runs: list[str] = []
    for shot in snapshot.shots:
        section = shot.section_id or ""
        if not runs or runs[-1] != section:
            runs.append(section)
    return len(runs) != len(set(runs))
