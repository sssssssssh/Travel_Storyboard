"""Edit plan and timeline export endpoints (M4, versioned by TSB-366).

Read/write contract after TSB-366:

* **Writes** go through the storyboard version service and therefore require a
  ``base_version_id``. A client that predates versioning gets ``428`` instead of
  silently replacing the plan — this is the compatibility gate the task book
  asks for (§5.4).
* **Reads** (``GET /edit-plan`` and all three downloads) resolve the *current
  version* through the same adapter and keep their historical response shapes,
  so existing callers keep working unchanged apart from the new
  ``X-Storyboard-Version-Id`` response header.
* Every export pins exactly one ``version_id`` (§5.3): JSON, CSV and OTIO are
  rendered from a single ``ExportContext``, so they cannot disagree.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..models import Project
from ..schemas import EditPlanBuild
from ..services.storyboard_schema import SnapshotError
from ..services.storyboard_service import (
    BaseVersionRequired,
    StoryboardNotFound,
    VersionConflict,
    get_current_version,
    load_storyboard_version,
    save_version,
)
from ..services.timeline_export import export_csv, export_json, export_otio
from .deps import get_db

router = APIRouter(tags=["export"])


def _require_project(project_id: str, db: Session) -> None:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")


def _version_header(view) -> dict[str, str]:
    """Record which board/version an export came from (§5.3)."""
    return {
        "X-Storyboard-Board-Id": view.board_id,
        "X-Storyboard-Version-Id": view.version_id,
        "X-Storyboard-Version-No": str(view.version_no),
    }


@router.post("/projects/{project_id}/edit-plan")
def save_edit_plan(project_id: str, payload: EditPlanBuild, db: Session = Depends(get_db)):
    """Save the plan as a new storyboard version.

    Kept at the same path and with the same request body shape for existing
    clients, plus a now-mandatory ``base_version_id``.
    """
    _require_project(project_id, db)
    try:
        view = save_version(
            project_id=project_id,
            base_version_id=payload.base_version_id,
            shots=[s.model_dump() for s in payload.shots],
            actor="user",
            reason="manual_edit",
            idempotency_key=payload.idempotency_key,
        )
    except BaseVersionRequired as exc:
        raise HTTPException(status_code=428, detail={
            "code": "BASE_VERSION_REQUIRED",
            "message": str(exc),
            "hint": (
                "read GET /projects/{id}/storyboard first and send its version_id "
                "as base_version_id"
            ),
        }) from None
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail={
            "code": "PLAN_VERSION_CONFLICT",
            "message": str(exc),
            "current_version_id": exc.current_version_id,
            "base_version_id": exc.base_version_id,
        }) from None
    except SnapshotError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_SNAPSHOT", "message": str(exc), "issues": exc.issues,
        }) from None
    except StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        }) from None

    timeline = sum(float(s.get("target_duration_sec") or 0.0) for s in view.shots)
    return {
        # Historical shape (§5.4): created/errors/total_duration_sec.
        "created": len(view.shots),
        "errors": [],
        "total_duration_sec": round(timeline, 3),
        # TSB-366 additions.
        "board_id": view.board_id,
        "version_id": view.version_id,
        "version_no": view.version_no,
        "parent_version_id": view.parent_version_id,
        "validation_summary": view.validation_summary,
    }


def _resolve_version(project_id: str, version_id: str | None):
    """Pin the version an export reads.

    With no explicit id this is "the current version", and a project that has
    no board yet gets an empty version 1 — the Export page must be able to load
    before the user has saved anything (§4.1 / §5.2).
    """
    try:
        if version_id is None:
            return get_current_version(project_id=project_id)
        return load_storyboard_version(project_id=project_id, version_id=version_id)
    except StoryboardNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        }) from None


@router.get("/projects/{project_id}/edit-plan")
def get_edit_plan(
    project_id: str,
    version_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """The plan document (legacy shape). Reads the current version by default.

    TSB-366-R1 (P0-01): the resolved version must be passed *into* the renderer.
    The previous form built the document from the current version and then
    relabelled it with the requested ``version_id``, so asking for a historical
    version returned that version's id with the current version's shots.
    """
    _require_project(project_id, db)
    view = _resolve_version(project_id, version_id)
    # One version id flows all the way down: resolve → render → respond.
    data = export_json(project_id, version_id=view.version_id)
    data["version_id"] = view.version_id
    data["board_id"] = view.board_id
    data["version_no"] = view.version_no
    return data


@router.get("/projects/{project_id}/export/json")
def download_json(
    project_id: str,
    version_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_project(project_id, db)
    view = _resolve_version(project_id, version_id)
    data = export_json(project_id, version_id=view.version_id)
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="edit_plan.json"',
            **_version_header(view),
        },
    )


@router.get("/projects/{project_id}/export/csv")
def download_csv(
    project_id: str,
    version_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_project(project_id, db)
    view = _resolve_version(project_id, version_id)
    return Response(
        content=export_csv(project_id, version_id=view.version_id),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="timeline.csv"',
            **_version_header(view),
        },
    )


@router.get("/projects/{project_id}/export/otio")
def download_otio(
    project_id: str,
    version_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    _require_project(project_id, db)
    view = _resolve_version(project_id, version_id)
    return Response(
        content=export_otio(project_id, version_id=view.version_id),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="edit.otio"',
            **_version_header(view),
        },
    )
