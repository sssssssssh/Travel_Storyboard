"""Protected media serving (representative frames, proxies, rough cuts).

Security model (audit P0-01): arbitrary relative paths are NOT accepted. A file
is served only when it
1. resolves inside that project's cache directory (no ``..`` escapes, and a
   project's own artifacts only — never another project's),
2. sits under a whitelisted artifact sub-directory (``frames``, ``proxies``,
   ``packages``, ``roughcut``, ``contact_sheets``), and
3. has an extension in the allow-list below.

The check is done on the *resolved* path, so it keeps working when a project
uses a custom ``output_dir`` (audit P1-02) and stores absolute references.

The SQLite database and ``token.txt`` sit at the data root, outside every
project cache directory, so they can never be reached through these routes.
Video/audio responses support HTTP Range so ``<video>`` scrubbing works.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..models import Frame, Project, ReviewRequest
from ..paths import project_cache_dir, rel_to_data, resolve_in_project
from .deps import get_db

router = APIRouter(tags=["media"])

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}
ARCHIVE_TYPES = {".zip": "application/zip"}

ALLOWED_TYPES = {**IMAGE_TYPES, **VIDEO_TYPES, **ARCHIVE_TYPES}

# Only these sub-directories of a project cache may ever be served.
ALLOWED_ARTIFACT_DIRS = {"frames", "proxies", "packages", "roughcut", "contact_sheets"}


def _media_type(path: Path) -> str:
    return (
        ALLOWED_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(str(path))[0]
        or "application/octet-stream"
    )


def _artifact_dir_ok(resolved: Path, cache_root: Path) -> bool:
    """True when *resolved* is inside an allowed artifact dir of *cache_root*."""
    try:
        rel = resolved.relative_to(cache_root)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in ALLOWED_ARTIFACT_DIRS


def _resolve_artifact(db: Session, project_id: str, rel: str) -> Path:
    """Validate + resolve a stored artifact path for *project_id*."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    cache_root = project_cache_dir(project_id).resolve()
    try:
        resolved = resolve_in_project(project_id, rel)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not _artifact_dir_ok(resolved, cache_root):
        raise HTTPException(status_code=400, detail="Artifact directory not allowed")
    if resolved.suffix.lower() not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="File type not allowed")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


@router.get("/projects/{project_id}/media")
def get_project_media(
    project_id: str,
    path: str = Query(..., description="Stored artifact path under this project"),
    db: Session = Depends(get_db),
):
    """Serve a whitelisted project artifact (frames / proxies / packages)."""
    resolved = _resolve_artifact(db, project_id, path)
    return FileResponse(resolved, media_type=_media_type(resolved))


@router.get("/frames/{frame_id}/image")
def get_frame_image(frame_id: str, db: Session = Depends(get_db)):
    """Serve a representative frame by its database id."""
    frame = db.get(Frame, frame_id)
    if frame is None or not frame.image_path:
        raise HTTPException(status_code=404, detail="Frame not found")
    # The frame's own asset determines the owning project.
    from ..models import Asset  # local import avoids a cycle at module load

    asset = db.get(Asset, frame.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Frame asset not found")
    resolved = _resolve_artifact(db, asset.project_id, frame.image_path)
    return FileResponse(resolved, media_type=_media_type(resolved))


@router.get("/projects/{project_id}/review-requests/{request_id}/proxy")
def get_review_proxy(project_id: str, request_id: str, db: Session = Depends(get_db)):
    """Serve the round-2 low-res proxy for one review request."""
    req = db.get(ReviewRequest, request_id)
    if req is None or req.project_id != project_id:
        raise HTTPException(status_code=404, detail="Review request not found")
    if not req.proxy_ref:
        raise HTTPException(status_code=404, detail="Proxy not generated yet")
    resolved = _resolve_artifact(db, project_id, req.proxy_ref)
    return FileResponse(resolved, media_type=_media_type(resolved))


@router.get("/projects/{project_id}/roughcut")
def get_roughcut(project_id: str, db: Session = Depends(get_db)):
    """Serve the generated proxy rough cut for a project (if any).

    A cut missing shots is stored as ``roughcut_INCOMPLETE.mp4`` so it can never
    be mistaken for the finished edit (audit P1-08); it is served just the same,
    with the filename carrying the warning.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    work = project_cache_dir(project_id) / "roughcut"
    for name in ("roughcut.mp4", "roughcut_INCOMPLETE.mp4"):
        if (work / name).is_file():
            resolved = _resolve_artifact(db, project_id, rel_to_data(work / name))
            return FileResponse(resolved, media_type="video/mp4", filename=name,
                                content_disposition_type="inline",
                                headers={"X-Roughcut-Incomplete": str(name != "roughcut.mp4").lower()})
    raise HTTPException(status_code=404, detail="Rough cut not generated yet")
