"""M1 — media asset indexing.

Walks source roots, probes video files with ffprobe, and persists stable
`asset_id`s. Incremental: files are re-probed only when size/mtime change.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from sqlalchemy import select

from ..config import ANALYSIS_VERSION, VIDEO_EXTENSIONS
from ..database import session_scope
from ..ffmpeg import probe_media
from ..models import Asset, Job, Project, utcnow
from .runner import JobFailures, raise_if_cancelled, register_task, update_job

logger = logging.getLogger(__name__)


def iter_video_files(roots: list[str]):
    seen: set[str] = set()
    for root in roots:
        base = Path(root)
        if not base.exists() or not base.is_dir():
            continue
        for path in base.rglob("*"):
            try:
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
            except OSError:
                continue
            resolved = str(path.resolve())
            if resolved not in seen:
                seen.add(resolved)
                yield path.resolve()


def quick_fingerprint(path: Path, size: int) -> str:
    h = hashlib.sha256()
    h.update(str(size).encode("utf-8"))
    try:
        with open(path, "rb") as f:
            h.update(f.read(65536))
            if size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
    except OSError:
        pass
    return h.hexdigest()


def _apply_probe_fields(asset: Asset, path: Path, size: int, mtime: int) -> None:
    info = probe_media(path)
    if not info.has_video:
        raise RuntimeError("no_video_stream")
    v = info.video
    asset.file_size_bytes = size
    asset.mtime_ns = mtime
    asset.quick_fingerprint = quick_fingerprint(path, size)
    asset.duration_sec = info.duration_sec
    asset.fps_num = v.fps_num
    asset.fps_den = v.fps_den
    asset.vfr_flag = v.vfr
    asset.width = v.width
    asset.height = v.height
    asset.rotation = v.rotation
    asset.codec = v.codec
    asset.pixel_format = v.pixel_format
    asset.color_primaries = v.color_primaries
    asset.color_transfer = v.color_transfer
    asset.color_space = v.color_space
    asset.has_audio = info.has_audio
    asset.audio_codec = info.audio_codec
    asset.scan_status = "succeeded"
    asset.error_code = ""
    asset.error_message = ""
    asset.analysis_version = ANALYSIS_VERSION
    asset.scanned_at = utcnow()


def index_one(project_id: str, path: Path) -> tuple[str, str]:
    """Index a single file. Returns (asset_id, action).

    Exactly one Asset row exists per (project_id, source_path): the record is
    created first and then *reused* on probe failure, so a broken file cannot
    leave a duplicate behind (audit P0-02).
    """
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime_ns

    with session_scope() as s:
        existing = s.scalars(
            select(Asset).where(
                Asset.project_id == project_id, Asset.source_path == str(path)
            )
        ).first()

        if (
            existing
            and existing.file_size_bytes == size
            and existing.mtime_ns == mtime
            and existing.scan_status == "succeeded"
        ):
            return existing.id, "skipped"

        if existing is None:
            asset = Asset(
                project_id=project_id,
                source_path=str(path),
                display_name=path.name,
            )
            s.add(asset)
            s.flush()
            action = "created"
        else:
            asset = existing
            action = "updated"

        try:
            _apply_probe_fields(asset, path, size, mtime)
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            # Reuse the SAME row: never insert a second Asset for this path.
            asset.file_size_bytes = size
            asset.mtime_ns = mtime
            asset.scan_status = "failed"
            asset.error_code = "probe_failed"
            asset.error_message = str(exc)[:2000]
            asset.scanned_at = utcnow()
            s.flush()
            return asset.id, "failed"

        s.flush()
        return asset.id, action


@register_task("scan")
def scan_task(job_id: str) -> None:
    """Scan all project roots and index every video file."""
    failures = JobFailures(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        project = s.get(Project, project_id)
        if project is None:
            raise RuntimeError("Project not found")
        roots = list(project.source_roots or [])
        last_index = int((job.resume_token or {}).get("last_index", -1))

    files = sorted(iter_video_files(roots), key=lambda p: str(p).lower())
    total = len(files)
    done = 0
    failed = 0

    for i, path in enumerate(files):
        if i <= last_index:
            done += 1
            continue
        raise_if_cancelled(job_id)
        try:
            _, action = index_one(project_id, path)
        except Exception as exc:  # noqa: BLE001 - continue other files
            logger.exception("Index failed: %s", path)
            failed += 1
            failures.record(kind="file", ref=str(path), message=str(exc))
        else:
            if action == "failed":
                failed += 1
                failures.record(
                    kind="file", ref=str(path), message="probe failed (see asset.error_message)"
                )
        done += 1
        update_job(
            job_id,
            stats={"total": total, "done": done, "failed": failed},
            resume_token={"last_index": i},
        )
