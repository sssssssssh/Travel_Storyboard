"""M1 — media asset indexing.

Walks source roots, probes video files with ffprobe, and persists stable
`asset_id`s. Incremental: every scan samples head/middle/tail; changed signatures re-probe.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from sqlalchemy import select

from ..config import ANALYSIS_VERSION, VIDEO_EXTENSIONS
from ..database import session_scope
from ..ffmpeg import (
    long_path,
    probe_media,
    safe_is_file,
    safe_open,
    safe_scandir,
    safe_stat,
    walk_files,
)
from ..models import Asset, Job, Project, utcnow
from .runner import JobFailures, raise_if_cancelled, register_task, update_job

logger = logging.getLogger(__name__)


def _is_dir(path: Path) -> bool:
    """``Path.is_dir`` that survives a >MAX_PATH path.

    Permission problems propagate: an unreadable root must be reported as
    ``source_unavailable``, not quietly treated as "not a directory".
    """
    try:
        if path.is_dir():
            return True
    except PermissionError:
        raise
    except OSError:
        pass
    if os.name != "nt":
        return False
    try:
        return os.path.isdir(long_path(path))
    except (OSError, ValueError):
        return False


def iter_video_files(roots: list[str], check_cancel=None):
    """Yield every video file under *roots*, de-duplicated.

    Uses :func:`walk_files` rather than ``Path.rglob``. ``rglob`` raises
    ``FileNotFoundError`` for a directory at ~271+ characters and **swallows it**,
    silently skipping that whole subtree — a deep source root lost every file
    under it while the scan still reported success (TSB-254).

    Directory symlinks are not followed (``walk_files`` passes
    ``follow_symlinks=False``), so a link cycle cannot hang a scan.
    """
    seen: set[str] = set()
    for root in roots:
        base = Path(root)
        # safe_is_file / a tolerant is_dir: a deep root must still be walked.
        try:
            if not base.is_dir():
                continue
        except OSError:
            if not safe_is_file(base):
                continue

        for number, path in enumerate(walk_files(base)):
            if check_cancel and number % 128 == 0:
                check_cancel()
            try:
                if path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                # safe_is_file keeps deep (>MAX_PATH) files discoverable.
                if not safe_is_file(path):
                    continue
            except OSError:
                # An unreadable video still needs a manifest entry/failure. It
                # must not disappear from an otherwise successful empty scan.
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    yield path.absolute()
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
    # safe_open retries through the extended-length form so a >MAX_PATH source
    # file is still fingerprinted (TSB-241).
    with safe_open(path, "rb") as f:
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
    # safe_stat tolerates >MAX_PATH source paths (TSB-241): a deep file FFmpeg can
    # read must not look missing to the indexer.
    stat = safe_stat(path)
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
            from .checkpoints import file_identity, SourceChanged
            # Savepoint restores existing derived metadata if the original changes
            # while FFprobe runs. The stable Asset row and failure remain durable.
            with s.begin_nested():
                _apply_probe_fields(asset, path, size, mtime)
                if (file_identity(safe_stat(path)) != file_identity(stat)
                        or quick_fingerprint(path, size) != fingerprint):
                    raise SourceChanged('源文件已变化：探测期间修改，请再次扫描/检测')
        except Exception as exc:  # noqa: BLE001 - per-file isolation
            # Reuse the SAME row: never insert a second Asset for this path.
            if existing is None:
                asset.file_size_bytes = size
                asset.mtime_ns = mtime
            asset.scan_status = "failed"
            asset.error_code = getattr(exc, "code", "probe_failed")
            asset.error_message = str(exc)[:2000]
            asset.scanned_at = utcnow()
            s.flush()
            return asset.id, "failed"

        s.flush()
        return asset.id, action


def scan_manifest(roots: list[str], full_hash: bool = False, *, job_id=None) -> list[dict]:
    from .checkpoints import manifest_entry
    check = (lambda: raise_if_cancelled(job_id)) if job_id else None
    files = set()
    result = []
    for root in roots:
        path = Path(root)
        try:
            # A deep source root is still a valid root: plain is_dir()/iterdir()
            # answer "no such directory" past MAX_PATH, which used to make the
            # whole root look unavailable (TSB-254).
            if not _is_dir(path):
                raise OSError('素材根目录不存在或无法读取，请检查路径后再次扫描')
            # Force one directory read so a denied root cannot look empty.
            next(iter(safe_scandir(path)), None)
            files.update(iter_video_files([root], check_cancel=check))
        except OSError as exc:
            result.append({'key': 'root:' + str(path.resolve()), 'source': str(path),
                           'size': 0, 'identity': {}, 'scan_signature': 'missing', 'quick_signature': 'missing',
                           'signature_mode': 'full' if full_hash else 'quick', 'status': 'pending',
                           'source_error': str(exc), 'error_code': 'source_unavailable'})
    for path in sorted(files, key=str):
        if check:
            check()
        result.append(manifest_entry(str(path), str(path), full_hash=full_hash))
    return result


@register_task("scan")
def scan_task(job_id: str) -> None:
    from .checkpoints import reconcile, persist, prepare_item, verify_after_processing, fail_item
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
    items, token = reconcile(job_id, scan_manifest(roots, full_hash, job_id=job_id))
    force = bool(config.get("force", False))
    for item in items:
        raise_if_cancelled(job_id)
        if item['status'] in ('completed', 'skipped'):
            continue
        path = Path(item['source'])
        try:
            prepare_item(job_id, item, items, token, config)
            aid, action = index_one(project_id, path, force=force)
            item['asset_id'] = aid
            item['forced_reprobe'] = force
            if action == 'failed':
                with session_scope() as s:
                    failed_asset = s.get(Asset, aid)
                    error = RuntimeError(failed_asset.error_message if failed_asset else 'probe failed')
                    error.code = failed_asset.error_code if failed_asset else 'probe_failed'
                raise error
            verify_after_processing(job_id, item, items, token, config)
            item['status'] = 'skipped' if action == 'skipped' else 'completed'
            failures.resolve(kind='file', ref=str(path))
        except JobCancelled:
            raise
        except Exception as exc:
            fail_item(item, exc, token)
            failures.record(
                kind='file',
                ref=str(path),
                message=str(exc),
                # Carry the classification through so the failure list is
                # reconcilable by code (TSB-254), not just by prose.
                error_code=item.get('error_code') or getattr(exc, 'code', None),
            )
        persist(job_id, items, token)
