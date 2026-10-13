"""Protected media file serving (thumbnails, proxies)."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..models import Frame
from ..paths import resolve_from_data
from .deps import get_db

router = APIRouter(tags=["media"])

_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_TYPES:
        return _IMAGE_TYPES[suffix]
    if suffix in _VIDEO_TYPES:
        return _VIDEO_TYPES[suffix]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


@router.get("/media/file")
def get_media_file(path: str = Query(..., description="Relative path under data dir")):
    try:
        resolved = resolve_from_data(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved, media_type=_media_type(resolved))


@router.get("/frames/{frame_id}/image")
def get_frame_image(frame_id: str, db: Session = Depends(get_db)):
    frame = db.get(Frame, frame_id)
    if frame is None or not frame.image_path:
        raise HTTPException(status_code=404, detail="Frame not found")
    try:
        resolved = resolve_from_data(frame.image_path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Frame path invalid") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Frame file missing")
    return FileResponse(resolved, media_type="image/jpeg")
