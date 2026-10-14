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
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ffmpeg import safe_is_file, safe_serving_path
from ..models import Frame, Project, ReviewRequest, Job
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

# Media routes are registered with ``@router.api_route(..., methods=["GET", "HEAD"])``
# rather than ``@router.get``. FastAPI's ``get`` decorator registers only GET, so a
# HEAD request to a media URL answered 404 (TSB-251 requires HEAD compatibility).
# This is a media-serving concern only; the rest of the API keeps its existing
# method surface untouched.


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
    return (bool(rel.parts) and rel.parts[0] in ALLOWED_ARTIFACT_DIRS
            and 'staging' not in rel.parts)


def _resolve_artifact(db: Session, project_id: str, rel: str) -> Path:
    """Validate + resolve a stored artifact path for *project_id*.

    Authorization is unchanged and always decided on the ordinary resolved path
    (containment, whitelisted sub-directory, allow-listed extension). Only the
    *existence* check goes through :func:`safe_is_file`, because plain
    ``Path.is_file()`` returns ``False`` for a >MAX_PATH file that FFmpeg really
    wrote — which used to answer 404 for a perfectly good artifact (TSB-251).
    """
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
    # A permission problem must not masquerade as "missing" (404).
    try:
        present = safe_is_file(resolved)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Artifact is not readable") from exc
    if not present:
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def _serve(resolved: Path, *, media_type: str | None = None, **kwargs) -> FileResponse:
    """Return a FileResponse that also works for a deep (>MAX_PATH) artifact.

    ``FileResponse`` stats the path itself and fails on Windows past MAX_PATH,
    so it receives the extended-length form. The response still reports the
    ordinary filename, so no internal path leaks to the client.
    """
    return FileResponse(
        safe_serving_path(resolved),
        media_type=media_type or _media_type(resolved),
        **kwargs,
    )


@router.api_route("/projects/{project_id}/media", methods=["GET", "HEAD"])
def get_project_media(
    project_id: str,
    path: str = Query(..., description="Stored artifact path under this project"),
    db: Session = Depends(get_db),
):
    """Serve a whitelisted project artifact (frames / proxies / packages)."""
    resolved = _resolve_artifact(db, project_id, path)
    return _serve(resolved)


@router.api_route("/frames/{frame_id}/image", methods=["GET", "HEAD"])
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
    return _serve(resolved)


@router.api_route("/projects/{project_id}/review-requests/{request_id}/proxy",
                  methods=["GET", "HEAD"])
def get_review_proxy(project_id: str, request_id: str, db: Session = Depends(get_db)):
    """Serve the round-2 low-res proxy for one review request."""
    req = db.get(ReviewRequest, request_id)
    if req is None or req.project_id != project_id:
        raise HTTPException(status_code=404, detail="Review request not found")
    if not req.proxy_ref:
        raise HTTPException(status_code=404, detail="Proxy not generated yet")
    resolved = _resolve_artifact(db, project_id, req.proxy_ref)
    return _serve(resolved)


@router.api_route("/projects/{project_id}/roughcut", methods=["GET", "HEAD"])
def get_roughcut(project_id: str, db: Session = Depends(get_db)):
    """Serve the generated proxy rough cut for a project (if any).

    A cut missing shots is stored as ``roughcut_INCOMPLETE.mp4`` so it can never
    be mistaken for the finished edit (audit P1-08); it is served just the same,
    with the filename carrying the warning.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    # New publications are immutable; choose the latest committed Job reference.
    # Legacy fixed filenames remain readable without any data migration.
    jobs = db.scalars(select(Job).where(Job.project_id == project_id,
                                       Job.job_type == 'roughcut').order_by(Job.created_at.desc(), Job.id.desc()))
    candidates = [(j.resume_token['path'], bool(j.resume_token.get('incomplete')))
                  for j in jobs if (j.resume_token or {}).get('path')]
    work = project_cache_dir(project_id) / 'roughcut'
    candidates.extend([(rel_to_data(work / 'roughcut.mp4'), False),
                       (rel_to_data(work / 'roughcut_INCOMPLETE.mp4'), True)])
    for ref, incomplete in candidates:
        resolved = resolve_in_project(project_id, ref)
        try:
            present = safe_is_file(resolved)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail='Rough cut is not readable') from exc
        if present:
            resolved = _resolve_artifact(db, project_id, ref)
            return _serve(resolved, media_type='video/mp4', filename=resolved.name,
                          content_disposition_type='inline',
                          headers={'X-Roughcut-Incomplete': str(incomplete).lower()})
    raise HTTPException(status_code=404, detail="Rough cut not generated yet")
