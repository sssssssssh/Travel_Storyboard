"""TSB-367 — plan operation lifecycle: proposal, apply, reject (§6.2–6.4, §7).

This is the only module that turns a validated operation batch into a persisted
version. The order matters and is the task book's §6.2 sequence:

1. read the board; 2. take the writer lock; 3. begin; 4. check the idempotency
key; 5. check the base version is still current; 6. read the immutable base
snapshot; 7. resolve project media; 8. execute the batch in memory; 9. validate
the final snapshot; 10–13. in the *same* transaction write the version, its source
refs, mark the operation applied and advance the pointer; 14. commit.

Anything that raises before step 14 leaves no version, no ref, no ``applied`` row
and an unchanged pointer — there is a single transaction, so this is structural
rather than a series of compensating actions.

Concurrency (``§6.3``): the base-version comparison and the pointer update happen
inside the same ``BEGIN IMMEDIATE`` transaction as the insert, so two clients on
the same base cannot both commit. There is **no automatic rebase**: a stale
proposal is reported, never silently replayed onto the newer version.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy import func as sa_func
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from ..database import begin_write, session_scope
from ..models import utcnow
from ..models import (
    Asset,
    PlanOperation,
    Project,
    Segment,
    Storyboard,
    StoryboardVersion,
)
from .plan_operations import (
    OperationExecutionError,
    ProjectContext,
    execute_operations,
)
from .plan_operations_schema import OperationEnvelope, OperationSchemaError
from .storyboard_schema import StoryboardSnapshot

logger = logging.getLogger(__name__)

#: Statuses from which nothing further may happen.
TERMINAL_STATUSES = frozenset({"applied", "rejected"})


class PlanOperationError(Exception):
    """Base class for operation-lifecycle failures with a stable ``code``."""

    code = "OPERATION_FAILED"
    http_status = 500

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


class StoryboardMissing(PlanOperationError):
    code = "STORYBOARD_NOT_FOUND"
    http_status = 404


class OperationNotFound(PlanOperationError):
    """Unknown operation, or one belonging to a different project/board.

    Deliberately indistinguishable from "does not exist" so an id cannot be probed
    across projects (§7).
    """

    code = "OPERATION_NOT_FOUND"
    http_status = 404


class PlanVersionConflict(PlanOperationError):
    code = "PLAN_VERSION_CONFLICT"
    http_status = 409


class StaleProposal(PlanOperationError):
    code = "STALE_PROPOSAL"
    http_status = 409


class IdempotencyKeyReused(PlanOperationError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409


class OperationAlreadyFinal(PlanOperationError):
    code = "OPERATION_ALREADY_FINAL"
    http_status = 409


class InvalidSnapshot(PlanOperationError):
    """The resulting plan is structurally invalid; it may not be saved (R1-03)."""

    code = "INVALID_SNAPSHOT"
    http_status = 422


class MissingShotIdentity(PlanOperationError):
    """A proposal cannot be applied because it records no shot identity (R1-01)."""

    code = "MISSING_SHOT_IDENTITY"
    http_status = 409


class OperationFailed(PlanOperationError):
    """The executor refused the batch; nothing was written."""

    code = "OPERATION_FAILED"
    http_status = 422

    def __init__(self, message: str, *, code: str | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, detail=detail)
        if code:
            self.code = code
        # Map the executor's codes onto HTTP statuses from §7's table.
        self.http_status = _EXECUTOR_STATUS.get(self.code, 422)


#: Executor code -> HTTP status (task book §7 error table).
_EXECUTOR_STATUS = {
    "SHOT_NOT_FOUND": 404,
    "SECTION_NOT_FOUND": 404,
    "LOCK_CONFLICT": 409,
    "INVALID_REFERENCE": 422,
    "INVALID_RANGE": 422,
    "CONSTRAINT_CONFLICT": 422,
    "INSUFFICIENT_MATERIAL": 422,
    "UNKNOWN_OPERATION": 400,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _board_for(session: Session, project_id: str, board_id: str | None = None) -> Storyboard:
    if session.get(Project, project_id) is None:
        raise StoryboardMissing(f"project {project_id} was not found")
    board = session.scalars(
        select(Storyboard).where(Storyboard.project_id == project_id)
    ).first()
    if board is None:
        raise StoryboardMissing(f"project {project_id} has no storyboard")
    if board_id is not None and board.id != board_id:
        # A board id that does not belong to this project must look like a miss,
        # never like a permission hint.
        raise StoryboardMissing("storyboard not found in this project")
    return board


def _context_for(session: Session, project_id: str) -> ProjectContext:
    """Resolve the project's media into the plain data the executor takes."""
    assets = {
        a.id: {"duration_sec": float(a.duration_sec or 0.0),
               "source_signature": getattr(a, "analyzed_source_signature", "") or ""}
        for a in session.scalars(select(Asset).where(Asset.project_id == project_id)).all()
    }
    segments = {
        g.id: {
            "asset_id": g.asset_id,
            "start_sec": float(g.start_sec or 0.0),
            "end_sec": float(g.end_sec or 0.0),
            "status": getattr(g, "status", "") or "",
        }
        for g in session.scalars(select(Segment).where(Segment.project_id == project_id)).all()
    }
    return ProjectContext(project_id=project_id, assets=assets, segments=segments)


def _operation_or_404(session: Session, *, project_id: str, board_id: str,
                      operation_id: str) -> PlanOperation:
    row = session.get(PlanOperation, operation_id)
    if row is None or row.project_id != project_id or row.board_id != board_id:
        raise OperationNotFound("operation not found")
    return row


def _snapshot_of(version: StoryboardVersion) -> StoryboardSnapshot:
    return StoryboardSnapshot.from_dict({
        "schema_version": version.schema_version,
        "brief": version.brief or {},
        "sections": version.sections or [],
        "shots": version.shots or [],
        "constraints": version.constraints or {},
        "validation_summary": version.validation_summary or {},
    })


def _require_identities(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark insert/replace operations as needing a recorded identity (R1-01).

    Applying re-runs the executor. Without this flag a proposal that recorded no
    identity (one written by an older build, or hand-inserted) would silently mint
    a *new* id at apply time — precisely the mismatch R1-01 removes. Marking them
    makes the executor refuse instead.
    """
    from .plan_operations import RESOLVED_ID_FIELD

    out: list[dict[str, Any]] = []
    for operation in operations:
        copy = dict(operation)
        if operation.get("type") in ("insert_shot", "replace_shot") and \
                not operation.get(RESOLVED_ID_FIELD):
            copy["_identity_required"] = True
        out.append(copy)
    return out


def _plan_identities(operations: list[dict[str, Any]],
                     snapshot: Any) -> list[dict[str, Any]]:
    """Mint one identity per insert/replace, **before** execution (R1-01).

    Deciding identity up front - rather than after the run - is what makes the
    preview diff, the stored proposal and the saved version agree on the same ids.
    Each operation gets its own id, and none may collide with an id already in the
    snapshot or with another id minted in this batch.
    """
    from .plan_operations import RESOLVED_ID_FIELD, _new_shot_id

    taken = {shot.shot_id for shot in snapshot.shots}
    out: list[dict[str, Any]] = []
    for operation in operations:
        copy = dict(operation)
        if operation.get("type") in ("insert_shot", "replace_shot"):
            if not operation.get(RESOLVED_ID_FIELD):
                minted = _new_shot_id(taken)
                taken.add(minted)
                copy[RESOLVED_ID_FIELD] = minted
        out.append(copy)
    return out


def _set_status_if_proposed(operation_id: str, new_status: str,
                            error_code: str = "") -> bool:
    """Persist a non-success status in its **own** transaction (R1-02).

    The failing business transaction is rolled back, so a status written inside it
    would vanish. This runs afterwards, on a fresh connection, and is
    **conditional on the row still being `proposed`** — it must never overwrite a
    terminal state another request already committed.

    Returns whether the row actually moved.
    """
    with session_scope() as s:
        begin_write(s)
        # ONE statement. A second UPDATE on the same row would hit the
        # terminal-immutability trigger and fail — the row is already terminal by
        # then, which is exactly what that trigger is for.
        result = s.execute(
            sa_text(
                "UPDATE plan_operations "
                "SET status = :status, error_code = :code, updated_at = :now "
                "WHERE id = :id AND status = 'proposed'"
            ),
            {"status": new_status, "code": error_code, "id": operation_id,
             "now": utcnow()},
        )
        if result.rowcount != 1:
            # Another request already committed a terminal state; never overwrite it.
            logger.debug("status write skipped for %s: row is no longer proposed",
                         operation_id)
            return False
        return True


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

@dataclass
class ProposalResult:
    operation: PlanOperation


def propose_operation(*, project_id: str, envelope: dict[str, Any] | OperationEnvelope,
                      board_id: str | None = None,
                      actor: str = "user") -> PlanOperation:
    """Validate and record an operation batch without changing the plan.

    The preview diff is computed here by running the batch against the base
    snapshot in memory, so the client sees exactly what would change. Nothing is
    written except the ``proposed`` operation row.
    """
    parsed = envelope if isinstance(envelope, OperationEnvelope) else (
        OperationEnvelope.from_dict(envelope)
    )
    digest = parsed.payload_digest()

    with session_scope() as s:
        begin_write(s)
        board = _board_for(s, project_id, board_id)

        if parsed.board_id and parsed.board_id != board.id:
            raise StoryboardMissing("storyboard not found in this project")

        # Idempotency first (§6.2 step 4): a replay must not even re-validate.
        existing = s.scalars(
            select(PlanOperation).where(
                PlanOperation.board_id == board.id,
                PlanOperation.idempotency_key == parsed.idempotency_key,
            )
        ).first()
        if existing is not None:
            if existing.payload_digest != digest:
                raise IdempotencyKeyReused(
                    "this idempotency key was already used with a different request",
                    detail={"operation_id": existing.id},
                )
            return existing

        # The base version must still be the current one (§6.2 step 5).
        if board.current_version_id != parsed.base_version_id:
            raise PlanVersionConflict(
                "the plan changed since this request was prepared",
                detail={"current_version_id": board.current_version_id,
                        "base_version_id": parsed.base_version_id},
            )

        base_version = s.get(StoryboardVersion, parsed.base_version_id)
        if base_version is None or base_version.board_id != board.id:
            raise PlanVersionConflict("base version does not belong to this board")

        snapshot = _snapshot_of(base_version)
        context = _context_for(s, project_id)

        # R1-01: mint identities *before* executing, so the executor, the preview
        # diff and the stored proposal all agree on the same ids.
        planned_operations = _plan_identities(parsed.to_operation_dicts(), snapshot)

        try:
            result = execute_operations(
                snapshot=snapshot, operations=planned_operations,
                context=context,
            )
        except OperationExecutionError as exc:
            # A batch that cannot execute is never recorded as a proposal; the
            # client gets the precise reason instead of an unusable row.
            raise OperationFailed(
                str(exc), code=exc.code,
                detail={"operation_index": exc.index, **exc.detail},
            ) from None

        # R1-03: an invalid final snapshot must never become a proposal.
        if result.validation.state == "invalid":
            raise InvalidSnapshot(
                "the resulting plan is invalid and cannot be proposed",
                detail={"issues": [i.model_dump() for i in result.validation.issues]},
            )

        # The identities are already on the operations; store them verbatim so
        # apply reuses exactly what the user was shown.
        operations_with_identity = planned_operations

        op = PlanOperation(
            id=str(uuid.uuid4()),
            project_id=project_id,
            board_id=board.id,
            base_version_id=parsed.base_version_id,
            idempotency_key=parsed.idempotency_key,
            schema_version=parsed.schema_version,
            operations=operations_with_identity,
            diff=result.diff,
            status="proposed",
            reason=parsed.reason,
            actor=actor,
            payload_digest=digest,
            error_detail={"summary": result.summary},
        )
        s.add(op)
        s.flush()
        s.refresh(op)
        return op


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

class _Stage(Exception):
    """Carry a pending non-success status out of a doomed transaction (R1-02).

    ``session_scope`` rolls back on any exception, so a status assigned inside the
    transaction disappears. Raising ``_Stage`` lets ``apply_operation`` catch the
    rollback, write the status in its **own** conditional transaction, and only then
    surface the original error to the caller.
    """

    def __init__(self, status: str, error: Exception, *, error_code: str = "") -> None:
        super().__init__(str(error))
        self.status = status
        self.error = error
        self.error_code = error_code


def _apply_once(*, project_id: str, operation_id: str,
                board_id: str | None) -> PlanOperation:
    """One attempt at applying; raises ``_Stage`` to report a refused attempt."""
    with session_scope() as s:
        begin_write(s)
        board = _board_for(s, project_id, board_id)
        op = _operation_or_404(s, project_id=project_id, board_id=board.id,
                               operation_id=operation_id)

        if op.status == "applied":
            return op                       # replay: report the same outcome
        if op.status == "rejected":
            raise OperationAlreadyFinal("this operation was rejected")
        if op.status in ("stale", "failed"):
            raise OperationAlreadyFinal(
                f"this operation is {op.status} and cannot be applied again"
            )

        # Re-validate at apply time (§7): the world may have moved since propose.
        #
        # R1-02: a status written inside this transaction would be rolled back
        # together with the failure, leaving the row at ``proposed`` while the
        # request was in fact refused. So the failure is recorded *after* the
        # rollback, through ``_persist_failure`` — outside the ``with`` block.
        if board.current_version_id != op.base_version_id:
            raise _Stage(
                "stale", PlanVersionConflict(
                    "the plan changed after this proposal was created",
                    detail={"current_version_id": board.current_version_id,
                            "base_version_id": op.base_version_id},
                ),
            )

        base_version = s.get(StoryboardVersion, op.base_version_id)
        if base_version is None:
            raise _Stage("stale", StaleProposal("the base version no longer exists"))

        snapshot = _snapshot_of(base_version)
        context = _context_for(s, project_id)

        try:
            result = execute_operations(
                snapshot=snapshot,
                operations=_require_identities(op.operations or []),
                context=context,
            )
        except OperationExecutionError as exc:
            raise _Stage(
                "failed",
                OperationFailed(str(exc), code=exc.code,
                                detail={"operation_index": exc.index, **exc.detail}),
                error_code=exc.code,
            ) from None

        # R1-03: an invalid final snapshot must never be published.
        if result.validation.state == "invalid":
            raise _Stage(
                "failed",
                InvalidSnapshot(
                    "the resulting plan is invalid and cannot be saved",
                    detail={"issues": [i.model_dump() for i in result.validation.issues]},
                ),
                error_code="INVALID_SNAPSHOT",
            )

        # --- same transaction: version, refs, operation, pointer ------------
        from .storyboard_service import _write_version_and_refs

        next_no = int(s.scalar(
            select(sa_func.max(StoryboardVersion.version_no))
            .where(StoryboardVersion.board_id == board.id)
        ) or 0) + 1

        version = _write_version_and_refs(
            s, board=board, version_no=next_no,
            parent_version_id=board.current_version_id,
            snapshot=result.snapshot,
            actor=op.actor or "user",
            reason="manual_edit",
            job_id=None,
            restore_of_version_id=None,
            idempotency_key=None,
        )

        op.status = "applied"
        op.result_version_id = version.id
        op.diff = result.diff
        board.current_version_id = version.id

        s.flush()
        s.refresh(op)
        return op


def apply_operation(*, project_id: str, operation_id: str,
                    board_id: str | None = None) -> PlanOperation:
    """Apply a proposal, creating exactly one new immutable version.

    A refused attempt is recorded as ``stale`` or ``failed`` in its own committed
    transaction (R1-02) and then surfaced as the original error, so the database
    always agrees with what the caller was told.
    """
    try:
        return _apply_once(project_id=project_id, operation_id=operation_id,
                           board_id=board_id)
    except _Stage as staged:
        _set_status_if_proposed(operation_id, staged.status,
                                error_code=staged.error_code)
        raise staged.error from None


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------

def reject_operation(*, project_id: str, operation_id: str,
                     board_id: str | None = None,
                     reason: str = "") -> PlanOperation:
    """Mark a proposal rejected. Creates no version (§7)."""
    with session_scope() as s:
        begin_write(s)
        board = _board_for(s, project_id, board_id)
        op = _operation_or_404(s, project_id=project_id, board_id=board.id,
                               operation_id=operation_id)

        if op.status == "rejected":
            return op                       # idempotent
        if op.status in TERMINAL_STATUSES:
            raise OperationAlreadyFinal(
                f"this operation is {op.status} and cannot be rejected"
            )

        op.status = "rejected"
        if reason:
            op.reason = reason[:2000]
        s.flush()
        s.refresh(op)
        return op


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def read_operation(*, project_id: str, operation_id: str,
                   board_id: str | None = None) -> PlanOperation:
    with session_scope() as s:
        board = _board_for(s, project_id, board_id)
        op = _operation_or_404(s, project_id=project_id, board_id=board.id,
                               operation_id=operation_id)
        s.expunge(op)
        return op


def list_operations(*, project_id: str, board_id: str | None = None,
                    limit: int = 50) -> list[PlanOperation]:
    with session_scope() as s:
        board = _board_for(s, project_id, board_id)
        rows = s.scalars(
            select(PlanOperation)
            .where(PlanOperation.board_id == board.id)
            .order_by(PlanOperation.created_at.desc())
            .limit(limit)
        ).all()
        for row in rows:
            s.expunge(row)
        return list(rows)


def operation_to_api(op: PlanOperation) -> dict[str, Any]:
    """The JSON view of an operation (§7)."""
    return {
        "operation_id": op.id,
        "project_id": op.project_id,
        "board_id": op.board_id,
        "base_version_id": op.base_version_id,
        "result_version_id": op.result_version_id,
        "status": op.status,
        "schema_version": op.schema_version,
        "operations": op.operations or [],
        "diff": op.diff or {},
        "reason": op.reason or "",
        "actor": op.actor or "user",
        "error_code": op.error_code or "",
        "error_detail": op.error_detail or {},
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "requires_confirmation": op.status == "proposed",
    }
