"""TSB-367 — the plan-operation API surface (§7).

| method | path | purpose |
|---|---|---|
| POST | ``/projects/{p}/storyboards/{b}/operations`` | create a proposal |
| GET  | ``/projects/{p}/storyboards/{b}/operations/{o}`` | status, diff, receipt |
| POST | ``…/operations/{o}/apply`` | confirm and apply |
| POST | ``…/operations/{o}/reject`` | reject a proposal |
| GET  | ``…/versions/{v}/diff`` | difference between two versions |

Every route sits under the existing ``/api`` token middleware and resolves the
board through the project, so a ``board_id`` from another project is reported as
"not found" rather than "forbidden" — an id must not be probeable (§7, §9.7).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..services.plan_operation_service import (
    IdempotencyKeyReused,
    OperationAlreadyFinal,
    OperationFailed,
    OperationNotFound,
    PlanOperationError,
    PlanVersionConflict,
    StaleProposal,
    StoryboardMissing,
    apply_operation,
    list_operations,
    operation_to_api,
    propose_operation,
    read_operation,
    reject_operation,
)
from ..services.plan_operations_schema import OperationSchemaError
from ..services.storyboard_service import (
    StoryboardNotFound,
    load_storyboard_version,
)
from .deps import get_db

router = APIRouter(tags=["plan-operations"])


class ProposeRequest(BaseModel):
    """The request body of §4.1, plus the two server-side fields are REFUSED.

    ``model_config`` with ``extra="forbid"`` is what stops a client from sending
    ``status`` / ``diff`` / ``result_version_id``: they are not declared, so they
    are a 422 at the FastAPI layer, before any business logic sees them.
    """

    model_config = {"extra": "forbid"}

    schema_version: int
    board_id: str | None = None
    base_version_id: str
    idempotency_key: str
    operations: list[dict[str, Any]]
    reason: str = ""


class RejectRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = ""


def _translate(exc: Exception) -> HTTPException:
    """Map service errors onto §7's table without leaking internals."""
    if isinstance(exc, OperationSchemaError):
        status = 400
        return HTTPException(status_code=status, detail={
            "code": exc.code,
            "message": str(exc),
            "issues": exc.issues,
        })
    if isinstance(exc, PlanOperationError):
        return HTTPException(status_code=exc.http_status, detail={
            "code": exc.code,
            "message": str(exc),
            **({"detail": exc.detail} if exc.detail else {}),
        })
    if isinstance(exc, StoryboardNotFound):
        return HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        })
    raise exc


@router.post("/projects/{project_id}/storyboards/{board_id}/operations")
def create_operation(project_id: str, board_id: str, body: ProposeRequest,
                     db: Session = Depends(get_db)):
    """Validate a batch and record it as a proposal (no version yet)."""
    try:
        op = propose_operation(
            project_id=project_id,
            board_id=board_id,
            envelope=body.model_dump(),
        )
    except (OperationSchemaError, PlanOperationError, StoryboardNotFound) as exc:
        raise _translate(exc) from None
    return operation_to_api(op)


@router.get("/projects/{project_id}/storyboards/{board_id}/operations")
def get_operation_list(project_id: str, board_id: str, limit: int = 50,
                       db: Session = Depends(get_db)):
    """Recent operations for this board, newest first."""
    try:
        rows = list_operations(project_id=project_id, board_id=board_id, limit=limit)
    except (PlanOperationError, StoryboardNotFound) as exc:
        raise _translate(exc) from None
    return [operation_to_api(r) for r in rows]


@router.get("/projects/{project_id}/storyboards/{board_id}/operations/{operation_id}")
def get_operation(project_id: str, board_id: str, operation_id: str,
                  db: Session = Depends(get_db)):
    try:
        op = read_operation(project_id=project_id, board_id=board_id,
                            operation_id=operation_id)
    except (PlanOperationError, StoryboardNotFound) as exc:
        raise _translate(exc) from None
    return operation_to_api(op)


@router.post("/projects/{project_id}/storyboards/{board_id}/operations/{operation_id}/apply")
def apply(project_id: str, board_id: str, operation_id: str,
          db: Session = Depends(get_db)):
    """Confirm a proposal: creates exactly one new version."""
    try:
        op = apply_operation(project_id=project_id, board_id=board_id,
                             operation_id=operation_id)
    except (OperationSchemaError, PlanOperationError, StoryboardNotFound) as exc:
        raise _translate(exc) from None
    return operation_to_api(op)


@router.post("/projects/{project_id}/storyboards/{board_id}/operations/{operation_id}/reject")
def reject(project_id: str, board_id: str, operation_id: str,
           body: RejectRequest | None = None, db: Session = Depends(get_db)):
    """Reject a proposal. Never creates a version."""
    try:
        op = reject_operation(
            project_id=project_id, board_id=board_id, operation_id=operation_id,
            reason=(body.reason if body else ""),
        )
    except (PlanOperationError, StoryboardNotFound) as exc:
        raise _translate(exc) from None
    return operation_to_api(op)


@router.get("/projects/{project_id}/storyboards/{board_id}/versions/{version_id}/diff")
def version_diff(project_id: str, board_id: str, version_id: str,
                 base: str | None = None, db: Session = Depends(get_db)):
    """Structural difference between two versions of the same board.

    With no ``base`` this compares the current version against its parent, which
    is the question a reviewer usually has ("what did the last change do?").
    """
    from ..services.plan_operations import _build_diff  # shared structural diff
    from ..services.storyboard_schema import StoryboardSnapshot

    try:
        target = load_storyboard_version(project_id=project_id, version_id=version_id)
    except StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        }) from None

    if target.board_id != board_id:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": "version not in this board",
        })

    base_id = base or target.parent_version_id
    if not base_id:
        # A first version has nothing to compare against: an empty diff is the
        # honest answer, not an error.
        return {"version_id": version_id, "base_version_id": None,
                "diff": {"shots": {"added": [s["shot_id"] for s in target.shots],
                                   "removed": [], "reordered": [], "trimmed": [],
                                   "locked": [], "replaced": []},
                         "sections": {"added": [], "removed": [], "reordered": False},
                         "constraints": {"changed": False},
                         "totals": {"shots_before": 0,
                                    "shots_after": len(target.shots)}}}

    try:
        base_view = load_storyboard_version(project_id=project_id, version_id=base_id)
    except StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        }) from None
    if base_view.board_id != target.board_id:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": "versions are from different boards",
        })

    before = StoryboardSnapshot.from_dict(base_view.to_dict())
    after = StoryboardSnapshot.from_dict(target.to_dict())
    return {
        "version_id": version_id,
        "base_version_id": base_id,
        "diff": _build_diff(before, after, []),
    }
