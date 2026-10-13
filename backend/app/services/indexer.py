"""M1 — media asset indexing.

Walks source roots, probes video files with ffprobe, and persists stable
`asset_id`s. Incremental: every scan samples head/middle/tail; changed signatures re-probe.
"""

from __future__ import annotations

import hashlib
import logging
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
    # Include sample offsets so the signature has an explicit sampling protocol.
    offsets = sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)})
    with open(path, "rb") as f:
        for offset in offsets:
            h.update(str(offset).encode("ascii"))
            f.seek(offset)
            h.update(f.read(65536))
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


def index_one(project_id: str, path: Path, *, force: bool = False) -> tuple[str, str]:
    """Index a single file. Returns (asset_id, action).

    Exactly one Asset row exists per (project_id, source_path): the record is
    created first and then *reused* on probe failure, so a broken file cannot
    leave a duplicate behind (audit P0-02).
    """
    path = path.resolve()
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime_ns
    fingerprint = quick_fingerprint(path, size)

    with session_scope() as s:
        existing = s.scalars(
            select(Asset).where(
                Asset.project_id == project_id, Asset.source_path == str(path)
            )
        ).first()

        if (
            existing
            and not force
            and existing.file_size_bytes == size
            and existing.mtime_ns == mtime
            and existing.quick_fingerprint == fingerprint
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


def scan_manifest(roots: list[str], full_hash: bool = False) -> list[dict]:
    from .checkpoints import manifest_entry
    return [manifest_entry(str(path), str(path), full_hash=full_hash)
            for path in sorted(iter_video_files(roots), key=lambda p: str(p))]


@register_task("scan")
def scan_task(job_id: str) -> None:
    from .checkpoints import reconcile, persist, source_signature
    from .runner import JobCancelled
    failures = JobFailures(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        project = s.get(Project, project_id)
        if project is None:
            raise RuntimeError("Project not found")
        config = dict(job.config_snapshot or {})
        roots = list(config.get("source_roots", project.source_roots or []))
    full_hash = bool(config.get("full_hash", False))
    items, token = reconcile(job_id, scan_manifest(roots, full_hash))
    for item in items:
        raise_if_cancelled(job_id)
        if item['status'] in ('completed', 'skipped'):
            continue
        path = Path(item['source'])
        try:
            if item.get('source_error'):
                raise RuntimeError(item['source_error'])
            aid, action = index_one(project_id, path)
            item['asset_id'] = aid
            if action == 'failed':
                raise RuntimeError('probe failed (see asset.error_message)')
            if source_signature(path, full_hash=full_hash) != item['scan_signature']:
                raise RuntimeError('源文件已变化：扫描期间内容改变，请重试')
            item['status'] = 'skipped' if action == 'skipped' else 'completed'
            failures.resolve(kind='file', ref=str(path))
        except JobCancelled:
            raise
        except Exception as exc:
            item['status'] = 'failed'
            failures.record(kind='file', ref=str(path), message=str(exc))
        persist(job_id, items, token)
