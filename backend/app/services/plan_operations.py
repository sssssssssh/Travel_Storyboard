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

def _apply_move_shot(shots: list[ShotSnapshot], op: dict, index: int) -> None:
    shot = _find_shot(shots, op["shot_id"], index)
    _check_lock(shot, "move_shot", index)

    anchor_id = op.get("after_shot_id") or op.get("before_shot_id")
    anchor = _find_shot(shots, anchor_id, index)

    shots.remove(shot)
    anchor_position = _index_of(shots, anchor.shot_id)
    target = anchor_position + 1 if op.get("after_shot_id") else anchor_position
    # Moving a shot "after itself" is a no-op rather than an error: the client's
    # intent is already satisfied and refusing would be hostile.
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

    existing = {s.shot_id for s in snapshot.shots}
    shot_id = _new_shot_id(existing)
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
    existing = {s.shot_id for s in snapshot.shots}
    shot_id = _new_shot_id(existing)
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
    shot = _find_shot(shots, op["shot_id"], index)
    dims = sorted({str(d).lower() for d in op.get("dimensions") or ["all"]})
    if op.get("locked"):
        current = set(shot.locked or ())
        current |= set(dims)
        # Storing `all` alone is the canonical form; keeping ["all","range"] would
        # make two identical lock states compare unequal.
        shot.locked = [LOCK_ALL] if LOCK_ALL in current else sorted(current)
    else:
        current = set(shot.locked or ())
        if LOCK_ALL in set(dims):
            shot.locked = None
        else:
            current -= set(dims)
            shot.locked = sorted(current) or None


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

    # A replace is expressed as one removal + one addition at the same position.
    for op in ops:
        if op.get("type") == "replace_shot":
            old_id = op.get("target_shot_id")
            position = next(
                (i for i, s in enumerate(after.shots)
                 if s.shot_id in added and s.asset_id == op.get("asset_id")),
                None,
            )
            replaced.append({
                "old_shot_id": old_id,
                "new_shot_id": after.shots[position].shot_id if position is not None else None,
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
                _apply_move_shot(working.shots, op, index)
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
        if shot.shot_id in seen:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="DUPLICATE_SHOT_ID",
                message="two shots share the same shot_id",
            ))
        seen.add(shot.shot_id)

        if shot.section_id and shot.section_id not in section_ids:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="INVALID_SECTION",
                message=f"section {shot.section_id} does not exist",
            ))

        segment = context.segments.get(shot.segment_id)
        if segment is None:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_MISSING",
                message=f"segment {shot.segment_id} is not in this project",
            ))
            continue
        if segment.get("status") == "needs_review":
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="SEGMENT_NEEDS_REVIEW",
                message="the shot's source changed and needs review",
            ))
        start = float(segment.get("start_sec") or 0.0)
        end = float(segment.get("end_sec") or 0.0)
        if shot.source_in_sec < start - 1e-3 or shot.source_out_sec > end + 1e-3:
            issues.append(ValidationIssue(
                shot_id=shot.shot_id, code="OUT_OF_RANGE",
                message="the shot range is outside its segment",
            ))

    hard = {"DUPLICATE_SHOT_ID", "SEGMENT_MISSING", "INVALID_SECTION"}
    if any(i.code in hard for i in issues):
        return issues, "invalid"
    if issues:
        return issues, "needs_review"
    return issues, "ok"
