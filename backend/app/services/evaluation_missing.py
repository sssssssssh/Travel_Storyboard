"""TSB-371 — name the files that are no longer where we recorded them.

Why this exists: a reviewer was told "2 items unavailable" and could not tell
whether the tool was broken or their library had changed. In fact one file of six
had gone. That is the user's footage, and they deserve to know which one — without
the API publishing where they keep it.

Design rules:

* **name the file** and its recorded size, so it is recognisable;
* **hint at the folder by name only** — enough for the owner, not a full path;
* **never say "deleted"**. We only know it is not at the recorded location. A
  rename, a re-mounted drive or a moved folder would make a stronger claim false;
* **never return an absolute path** (C5). The check is on the *response*, and the
  path is only used server-side to test existence.
"""

from __future__ import annotations

from datetime import timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ffmpeg import safe_is_file
from ..models import Asset, Segment

STATEMENT = "file_not_found_at_the_recorded_location"

#: How many names to inline before deferring to the endpoint.
MAX_INLINE_NAMES = 8


def _folder_hint(path: str) -> str:
    """The parent folder *name* — never the path."""
    if not path:
        return ""
    # Accept either separator so a Windows path processed on POSIX still works.
    windows = PureWindowsPath(path)
    if windows.parent.name:
        return windows.parent.name
    return PurePosixPath(path).parent.name


def _size_mb(size_bytes: int | None) -> float:
    if not size_bytes:
        return 0.0
    return round(float(size_bytes) / (1024 * 1024), 1)


def missing_sources(session: Session, project_id: str) -> dict[str, Any]:
    """Every asset whose file is not at its recorded location."""
    assets = session.scalars(
        select(Asset).where(Asset.project_id == project_id)
        .order_by(Asset.display_name)
    ).all()

    segment_counts = dict(session.execute(
        select(Segment.asset_id, func.count()).where(
            Segment.project_id == project_id).group_by(Segment.asset_id)
    ).all())

    items: list[dict[str, Any]] = []
    for asset in assets:
        try:
            present = bool(safe_is_file(asset.source_path))
        except OSError:
            present = False
        if present:
            continue

        recorded = asset.scanned_at or asset.created_at
        items.append({
            "asset_id": asset.id,
            "display_name": asset.display_name,
            "status": "not_found",
            "available": False,
            "size_mb": _size_mb(asset.file_size_bytes),
            "folder_hint": _folder_hint(asset.source_path),
            "recorded_at": (
                recorded.astimezone(timezone.utc).isoformat(timespec="seconds")
                if recorded else None
            ),
            "affected_segments": int(segment_counts.get(asset.id, 0)),
        })

    return {
        "project_id": project_id,
        "statement": STATEMENT,
        "count": len(items),
        "items": items,
        "summary": {
            "assets_total": len(assets),
            "assets_missing": len(items),
            "segments_affected": sum(i["affected_segments"] for i in items),
        },
        "notes": [
            "只说明在记录位置没找到，不代表文件被删除——改名、移动或磁盘未挂载都会这样。",
            "为保护隐私不返回完整路径，只给上一级文件夹名。",
            "这些镜头无法核对画面，因此不计入准确率。",
        ],
    }


def missing_names(session: Session, project_id: str,
                  limit: int = MAX_INLINE_NAMES) -> list[str]:
    """Just the names, for embedding in the sample summary."""
    return [i["display_name"] for i in missing_sources(session, project_id)["items"]][:limit]
