"""TSB-366 — storyboard version API.

The routes here are the version-aware surface:

* ``GET  /projects/{p}/storyboard``                      current version
* ``GET  /projects/{p}/storyboard/versions``             history, newest first
* ``GET  /projects/{p}/storyboard/versions/{v}``         one immutable snapshot
* ``POST /projects/{p}/storyboard/versions``             save a new version
* ``POST /projects/{p}/storyboard/restore``              restore → new version
* ``GET  /projects/{p}/storyboard/versions/{v}/source-refs``  derived index

Error semantics (§4.2, §5.4), deliberately stable and machine-readable:

======================  =======================================================
HTTP                    meaning
======================  =======================================================
404 ``STRORYBOARD_NOT_FOUND``  project/board/version missing, or the id belongs
                        to another project (the two are indistinguishable on
                        purpose, so an id cannot be probed across projects).
409 ``PLAN_VERSION_CONFLICT``  the client's ``base_version_id`` is stale; the
                        response carries the current version so the UI can offer
                        a reload. Nothing was written.
428 ``BASE_VERSION_REQUIRED``  a write arrived with no ``base_version_id``. This
                        is the compatibility gate: it stops a pre-versioning
                        client from replacing the plan unprotected.
422 ``INVALID_SNAPSHOT``       the submitted body did not validate; nothing was
                        written.
======================  =======================================================
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.storyboard_schema import SnapshotError
from ..services.storyboard_service import (
    BaseVersionRequired,
    RestoreNotAllowed,
    StoryboardNotFound,
    VersionConflict,
    get_current_version,
    get_version,
    get_version_source_refs,
    list_versions,
    restore_version,
    save_version,
)
from .deps import get_db

router = APIRouter(tags=["storyboard"])


class ShotIn(BaseModel):
    """One shot submitted by the editor.

    ``shot_id`` is optional on input: a brand-new shot gets a fresh stable id,
    while an edited existing shot sends its id back so identity survives the
    round trip (never the array index).
    """

    shot_id: str | None = None
    section_id: str | None = None
    asset_id: str
    segment_id: str
    source_in_sec: float
    source_out_sec: float
    target_duration_sec: float = 0.0
    story_role: str = ""
    transition_hint: str = ""
    music_cue: str = ""
    audio_hint: str = ""
    color_hint: str = ""
    evidence: str = ""
    confidence: float = 0.0
    review_status: str = "pending"


class SaveVersionRequest(BaseModel):
    shots: list[ShotIn] = Field(default_factory=list)
    #: §4.2: the version this edit was based on. Required.
    base_version_id: str | None = None
    #: §4.2: the same key twice returns the same version instead of creating v+2.
    idempotency_key: str | None = None
    title: str | None = None
    reason: str = "manual_edit"


class RestoreRequest(BaseModel):
    target_version_id: str
    base_version_id: str | None = None
    idempotency_key: str | None = None


def _http(exc: Exception) -> HTTPException:
    """Map service errors onto the stable HTTP contract."""
    if isinstance(exc, StoryboardNotFound):
        return HTTPException(status_code=404, detail={
            "code": "STORYBOARD_NOT_FOUND", "message": str(exc),
        })
    if isinstance(exc, VersionConflict):
        return HTTPException(status_code=409, detail={
            "code": "PLAN_VERSION_CONFLICT",
            "message": str(exc),
            "current_version_id": exc.current_version_id,
            "base_version_id": exc.base_version_id,
        })
    if isinstance(exc, BaseVersionRequired):
        return HTTPException(status_code=428, detail={
            "code": "BASE_VERSION_REQUIRED", "message": str(exc),
        })
    if isinstance(exc, RestoreNotAllowed):
        return HTTPException(status_code=409, detail={
            "code": "RESTORE_NOT_ALLOWED",
            "message": str(exc),
            "state": exc.state,
            "issues": exc.issues,
        })
    if isinstance(exc, SnapshotError):
        return HTTPException(status_code=422, detail={
            "code": "INVALID_SNAPSHOT", "message": str(exc),
            "issues": exc.issues,
        })
    raise exc


@router.get("/projects/{project_id}/storyboard")
def read_current_storyboard(project_id: str, db: Session = Depends(get_db)):
    """The project's current storyboard version."""
    try:
        return get_current_version(project_id=project_id).to_api()
    except StoryboardNotFound as exc:
        raise _http(exc) from None


@router.get("/projects/{project_id}/storyboard/versions")
def read_version_history(project_id: str, db: Session = Depends(get_db)):
    """Version history, newest first; each item is the full snapshot."""
    try:
        return [v.to_api() for v in list_versions(project_id=project_id)]
    except StoryboardNotFound as exc:
        raise _http(exc) from None


@router.get("/projects/{project_id}/storyboard/versions/{version_id}")
def read_version(project_id: str, version_id: str, db: Session = Depends(get_db)):
    """One version's immutable snapshot. Reading never moves the pointer."""
    try:
        current = get_current_version(project_id=project_id)
        return get_version(
            board_id=current.board_id, version_id=version_id, project_id=project_id
        ).to_api()
    except StoryboardNotFound as exc:
        raise _http(exc) from None


@router.get("/projects/{project_id}/storyboard/versions/{version_id}/source-refs")
def read_version_source_refs(
    project_id: str, version_id: str, db: Session = Depends(get_db)
):
    """The derived Asset/Segment index for one version (§2.3)."""
    try:
        current = get_current_version(project_id=project_id)
        refs = get_version_source_refs(
            board_id=current.board_id, version_id=version_id, project_id=project_id
        )
        return [r.__dict__ for r in refs]
    except StoryboardNotFound as exc:
        raise _http(exc) from None


@router.post("/projects/{project_id}/storyboard/versions")
def create_version(project_id: str, body: SaveVersionRequest, db: Session = Depends(get_db)):
    """Save a new version. Requires ``base_version_id`` (428 if absent)."""
    try:
        view = save_version(
            project_id=project_id,
            base_version_id=body.base_version_id,
            shots=[s.model_dump() for s in body.shots],
            actor="user",
            reason=body.reason if body.reason in {"manual_edit"} else "manual_edit",
            idempotency_key=body.idempotency_key,
        )
        return view.to_api()
    except (StoryboardNotFound, VersionConflict, BaseVersionRequired, SnapshotError) as exc:
        raise _http(exc) from None


@router.post("/projects/{project_id}/storyboard/restore")
def restore(project_id: str, body: RestoreRequest, db: Session = Depends(get_db)):
    """Restore an old version by appending a new one (§4.3)."""
    try:
        view = restore_version(
            project_id=project_id,
            target_version_id=body.target_version_id,
            base_version_id=body.base_version_id,
            idempotency_key=body.idempotency_key,
        )
        return view.to_api()
    except (
        StoryboardNotFound, VersionConflict, BaseVersionRequired,
        RestoreNotAllowed, SnapshotError,
    ) as exc:
        raise _http(exc) from None
