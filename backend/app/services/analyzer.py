"""M2 — segment detection + representative frames + quality, per asset.

The pipeline for one asset:
1. detect boundaries (detector.py)
2. select representative frame times (frames.py)
3. extract full-res thumbnails via ffmpeg
4. compute quality metrics + perceptual hash
5. flag near-duplicates within the asset
6. publish verified run-specific JPEGs, commit new references, then reclaim
   previous unreferenced JPEGs (crash orphans use a safe grace period).

Manual state survives re-runs: segments are matched on their immutable detected
bounds (not the user-editable in/out), so trimming a clip does not orphan its
rating, note, review requests or edit-plan references.
"""

from __future__ import annotations

import logging
import math
import shutil
import uuid
import time
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select

from ..config import ANALYSIS_VERSION, get_settings
from ..database import begin_write, session_scope
from ..ffmpeg import (
    FFmpegCancelled,
    extract_frame,
    safe_image_open,
    safe_is_file,
    safe_mkdir,
    safe_replace,
    safe_stat,
)
from ..models import Asset, EditPlanShot, Frame, Job, ReviewRequest, Segment, UserAnnotation, utcnow
from ..paths import frames_dir, rel_to_data, resolve_in_project, staging_dir
from .detector import detect_segments
from .frames import select_rep_times
from .quality import analyze_frame, hamming, phash
from .runner import (
    JobCancelled,
    JobFailures,
    cancel_check_for,
    raise_if_cancelled,
    register_task,
    update_job,
)

logger = logging.getLogger(__name__)

DUPLICATE_HAMMING_THRESHOLD = 6

# Shortest acceptable trim intersection; disjoint trims are preserved for review.
MIN_SEGMENT_SEC = 0.2


def _time_key(start: float, end: float) -> tuple[int, int]:
    return (int(round(start, 1) * 10), int(round(end, 1) * 10))


def _detected_key(segment: Segment) -> tuple[int, int]:
    """Identity of a segment as produced by the detector (never user-edited).

    An explicit ``is None`` check is required: ``0.0`` is a valid detection
    start (a cut at the very beginning of the clip), so a truthiness test would
    wrongly fall back to the user-editable bound and orphan trimmed segments.
    """
    start = segment.detected_start_sec
    end = segment.detected_end_sec
    if start is None:
        start = segment.start_sec
    if end is None:
        end = segment.end_sec
    return _time_key(start, end)


def _detect_config_key(settings) -> str:
    """Fingerprint of the detection knobs, so changing them forces a re-run."""
    return (
        f"v{ANALYSIS_VERSION}:"
        f"fps={settings.detect_sample_fps}:"
        f"thr={settings.detect_threshold}:"
        f"max={settings.detect_max_segment_sec}:"
        f"thumb={settings.thumb_width}"
    )


def _referenced_frame_paths(project_id: str) -> set[Path]:
    with session_scope() as s:
        references = list(s.scalars(select(Frame.image_path)).all())
        for refs in s.scalars(select(Segment.frame_refs)).all():
            references.extend(refs or [])
    kept: set[Path] = set()
    for ref in references:
        try:
            kept.add(resolve_in_project(project_id, ref))
        except ValueError:
            continue
    return kept


def cleanup_orphan_frames(project_id: str, grace_sec: float = 3600) -> int:
    """Remove only aged, unreferenced JPEGs within this project's frame cache.

    Safe after an interrupted publish, and retryable after cleanup errors.
    Symlinks and external files are never followed for deletion.
    """
    from ..paths import project_cache_dir
    project_root = project_cache_dir(project_id).resolve()
    roots = [(project_root / name).resolve() for name in ("frames", "staging")]
    kept = _referenced_frame_paths(project_id)
    freed = 0
    for root in roots:
        if not root.is_dir() or not root.is_relative_to(project_root):
            continue
        for path in root.rglob("*.jpg"):
            try:
                resolved = resolve_in_project(project_id, rel_to_data(path))
                if (path.is_symlink() or not resolved.is_relative_to(root)
                        or resolved in kept or time.time() - path.stat().st_mtime < grace_sec):
                    continue
                path.unlink()
                freed += 1
            except (OSError, ValueError):
                logger.warning("Could not clean orphan frame", exc_info=True)
    stage_root = roots[1]
    if stage_root.is_relative_to(project_root) and stage_root.is_dir():
        for directory in sorted(stage_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            try:
                if (directory.is_dir() and not directory.is_symlink()
                        and time.time() - directory.stat().st_mtime >= grace_sec):
                    directory.rmdir()  # refuses nonempty folders, never recurses
            except OSError:
                pass
    return freed


# Only these fields belong to analysis. User rating, role, tags, proxy, status,
# annotation rows and the latest trim are deliberately absent.
ALGORITHM_FIELDS = ("detected_start_sec", "detected_end_sec", "boundary_reason",
                    "algorithm_version", "frame_refs")


def _apply_algorithm_result(row: Segment, result: Segment) -> bool:
    """Apply derived values to the freshly locked row; audit any trim clamp."""
    metrics = dict(result.quality_metrics or {})
    previous = dict(row.quality_metrics or {})
    audit = list(previous.get('manual_trim_audit', []))
    warning = False
    if row.is_trimmed:
        low = max(row.start_sec, result.detected_start_sec)
        high = min(row.end_sec, result.detected_end_sec)
        if low != row.start_sec or high != row.end_sec:
            audit.append({'original_in_sec': row.start_sec, 'original_out_sec': row.end_sec,
                          'detected_start_sec': result.detected_start_sec,
                          'detected_end_sec': result.detected_end_sec,
                          'at': utcnow().isoformat(), 'action': 'clamped' if high - low >= MIN_SEGMENT_SEC else 'preserved_for_review'})
            row.status = 'needs_review'
            warning = True
            metrics['analysis_warning'] = '检测边界变化影响人工裁剪，原选择已留审计记录，请复核。'
            if high - low >= MIN_SEGMENT_SEC:
                row.start_sec, row.end_sec = low, high
            # A disjoint trim stays intact; never silently clear the user's work.
    else:
        row.start_sec, row.end_sec = result.detected_start_sec, result.detected_end_sec
    if audit:
        metrics['manual_trim_audit'] = audit
    if previous.get('analysis_warning') and row.status == 'needs_review' and not warning:
        metrics['analysis_warning'] = previous['analysis_warning']
    for field in ALGORITHM_FIELDS:
        setattr(row, field, getattr(result, field))
    row.quality_metrics = metrics
    return warning


def _analyze_asset(project_id: str, asset: Asset, settings=None, job_id: str | None = None) -> dict:
    settings = settings or get_settings()
    source = asset.source_path
    # safe_is_file tolerates >MAX_PATH source paths (TSB-241): a deep file that
    # FFmpeg can read must not be reported as "missing" here.
    if not safe_is_file(source):
        raise RuntimeError(f"Source file missing: {source}")

    from .checkpoints import source_info, verify_source, verify_after_processing
    context = getattr(settings, 'signature_context', None)
    source_state = context['item'] if context else source_info(Path(source))
    verify_source(source_state)
    signature_before = source_state.get('scan_signature', source_state['quick_signature'])
    cleanup_orphan_frames(project_id)
    detection = detect_segments(
        source,
        asset.duration_sec,
        sample_fps=settings.detect_sample_fps,
        threshold=settings.detect_threshold,
        max_segment_sec=settings.detect_max_segment_sec,
        # Cancelling a detect job must stop the sampling ffmpeg, not wait for it.
        cancel_check=cancel_check_for(job_id),
    )

    # Load existing segments to preserve IDs / manual marks across re-runs.
    # Matching uses the immutable detected bounds, so a segment whose in/out was
    # trimmed by the user is still recognised and its annotations survive
    # (audit P0-03).
    with session_scope() as s:
        old_segments = s.scalars(
            select(Segment).where(Segment.asset_id == asset.id)
        ).all()
        old_by_key = {_detected_key(seg): seg for seg in old_segments}
        old_ids = {seg.id for seg in old_segments}

    work_dir = resolve_in_project(project_id, rel_to_data(frames_dir(project_id, asset.id)))
    run_id = str(uuid.uuid4())
    # Paths use a compact run token: Windows caps paths at 260 chars by default,
    # and three full UUIDs plus a 36-char filename pushed real deep workspaces
    # over that limit (ffmpeg wrote the file, Python could not see it). The full
    # run_id is still stored and reported as `analysis_run_id`.
    run_token = run_id.replace("-", "")[:12]
    stage = resolve_in_project(
        project_id, rel_to_data(staging_dir(project_id, asset.id) / run_token)
    )
    # Each run owns a unique stage and unique published filenames. A crash
    # cannot overwrite the JPEGs referenced by the previous committed version.
    #
    # Create both directories here rather than relying on `extract_frame` to do
    # it: the frame extractor is replaceable (tests, future backends) and the
    # analysis step must not depend on that side effect.
    safe_mkdir(stage)
    safe_mkdir(work_dir)

    new_segments: list[Segment] = []
    new_frames: list[Frame] = []
    reused_ids: set[str] = set()
    phash_pairs: list[tuple[str, str]] = []

    for boundary in detection.boundaries:
        if job_id:
            raise_if_cancelled(job_id)
        rep_times = select_rep_times(
            boundary.start_sec,
            boundary.end_sec,
            detection.sample_times,
            detection.sample_diffs,
        )
        key = _time_key(boundary.start_sec, boundary.end_sec)
        prior = old_by_key.get(key)
        # Compute a fresh algorithm result, never mutate/merge a stale ORM row.
        segment = Segment(
            id=prior.id if prior is not None else str(uuid.uuid4()),
            asset_id=asset.id, project_id=project_id,
            detected_start_sec=boundary.start_sec, detected_end_sec=boundary.end_sec,
            start_sec=boundary.start_sec, end_sec=boundary.end_sec,
            boundary_reason=boundary.reason, algorithm_version=ANALYSIS_VERSION,
        )
        if prior is not None:
            reused_ids.add(prior.id)

        # Extract representative frames.
        frame_refs: list[str] = []
        quality_metrics: list[dict] = []
        for t in rep_times:
            if job_id:
                raise_if_cancelled(job_id)
            # Accurate seek returns the first frame at/after t. In a CFR tail,
            # no frame exists past the final presentation timestamp (e.g. 1fps).
            if not asset.vfr_flag and asset.fps_num and asset.fps_den:
                fps = asset.fps_num / asset.fps_den
                final_time = max(0., (math.ceil(asset.duration_sec * fps - 1e-6) - 1) / fps)
                if final_time >= boundary.start_sec:
                    t = min(t, final_time)
            frame_id = str(uuid.uuid4())
            # Compact but still collision-free: 12 hex chars of run + 12 of frame.
            frame_token = frame_id.replace("-", "")[:12]
            frame_name = f"{run_token}_{frame_token}.jpg"
            stage_path = stage / frame_name
            final_path = work_dir / frame_name
            # Re-assert the staging directory: the extractor is a replaceable
            # callable and a concurrent cleanup must never turn a missing
            # directory into a confusing mid-run failure.
            safe_mkdir(stage_path.parent)
            extract_frame(source, t, stage_path, width=settings.thumb_width,
                          cancel_check=cancel_check_for(job_id))
            with safe_image_open(stage_path) as im:
                im.load()
                fw, fh = im.size
            metrics = analyze_frame(stage_path)
            ph = phash(stage_path)
            # Store the *final* location; the file is moved there on success.
            rel = rel_to_data(final_path)
            frame_refs.append(rel)
            quality_metrics.append(metrics)
            new_frames.append(
                Frame(
                    id=frame_id,
                    segment_id=segment.id,
                    asset_id=asset.id,
                    time_sec=t,
                    image_path=rel,
                    phash=ph,
                    is_representative=True,
                    width=fw,
                    height=fh,
                    file_size_bytes=safe_stat(stage_path).st_size,
                )
            )
            phash_pairs.append((segment.id, ph))

        segment.frame_refs = frame_refs
        segment.quality_metrics = {
            "blur_avg": round(sum(m["blur"] for m in quality_metrics) / max(1, len(quality_metrics)), 2),
            "blur_min": round(min((m["blur"] for m in quality_metrics), default=0.0), 2),
            "brightness_avg": round(sum(m["brightness"] for m in quality_metrics) / max(1, len(quality_metrics)), 2),
            "overexposed_ratio": round(max((m["overexposed_ratio"] for m in quality_metrics), default=0.0), 4),
            "underexposed_ratio": round(max((m["underexposed_ratio"] for m in quality_metrics), default=0.0), 4),
            "frame_count": len(rep_times),
            "near_duplicate_segments": [],
        }
        new_segments.append(segment)

    # Near-duplicate detection within this asset.
    duplicate_map: dict[str, list[str]] = {}
    for i in range(len(phash_pairs)):
        for j in range(i + 1, len(phash_pairs)):
            a_id, a_hash = phash_pairs[i]
            b_id, b_hash = phash_pairs[j]
            if a_id == b_id:
                continue
            if hamming(a_hash, b_hash) <= DUPLICATE_HAMMING_THRESHOLD:
                duplicate_map.setdefault(a_id, []).append(b_id)
                duplicate_map.setdefault(b_id, []).append(a_id)
    for segment in new_segments:
        dups = sorted(set(duplicate_map.get(segment.id, [])))
        if dups:
            segment.quality_metrics["near_duplicate_segments"] = dups

    # Publish verified files before the DB transaction. Old files remain until
    # commit. A process crash here leaves reclaimable orphans, not bad DB refs.
    if context:
        verify_after_processing(job_id, context['item'], context['items'], context['token'], context['config'])
    else:
        verify_source(source_state)
    if not new_frames:
        raise RuntimeError("Analysis produced no readable frames")
    if job_id:
        raise_if_cancelled(job_id)
    safe_mkdir(work_dir)
    with session_scope() as s:
        previous_paths = [
            f.image_path
            for f in s.scalars(select(Frame).where(Frame.asset_id == asset.id)).all()
            if f.image_path
        ]

    # Derive the published names from what was actually recorded on each frame
    # rather than rebuilding them, so the naming scheme can change without this
    # list silently going empty.
    produced = {Path(frame.image_path).name for frame in new_frames if frame.image_path}
    try:
        for name in sorted(produced):
            src = stage / name
            if not safe_is_file(src):
                raise RuntimeError("Staged frame is missing")
            safe_replace(src, work_dir / name)
            with safe_image_open(work_dir / name) as image:
                image.load()
        shutil.rmtree(stage, ignore_errors=True)

        # Persist.
        if job_id:
            raise_if_cancelled(job_id)
        with session_scope() as s:
            begin_write(s)
            verify_source(source_state)
            if job_id:
                job = s.get(Job, job_id)
                if job is not None and job.status == 'cancelled':
                    from .runner import JobCancelled
                    raise JobCancelled('Cancelled before frame commit')
            latest = {row.id: row for row in s.scalars(select(Segment).where(Segment.asset_id == asset.id))}
            referenced = set(s.scalars(select(EditPlanShot.segment_id).where(EditPlanShot.segment_id.in_(old_ids))))
            referenced.update(s.scalars(select(ReviewRequest.segment_id).where(ReviewRequest.segment_id.in_(old_ids))))
            referenced.update(s.scalars(select(UserAnnotation.segment_id).where(UserAnnotation.segment_id.in_(old_ids))))
            removed_ids = old_ids - reused_ids
            protected = {sid for sid in removed_ids if sid in latest and (
                sid in referenced or latest[sid].user_rating != 'pending' or latest[sid].is_trimmed
                or latest[sid].story_role or latest[sid].content_tags
                or latest[sid].status not in ('candidate', 'pending'))}
            # Retain old frame rows and all external/manual references of unmatched
            # protected shots. Explicit user review is required, never remap IDs.
            needs_review = len(protected)
            for sid in protected:
                latest[sid].status = 'needs_review'
                latest[sid].quality_metrics = {**(latest[sid].quality_metrics or {}),
                    'analysis_warning': '检测边界已变化；旧镜头和人工状态已保留，请重新复核源区间。'}
            deletable = removed_ids - protected
            replaced = reused_ids | deletable
            if replaced:
                s.execute(delete(Frame).where(Frame.asset_id == asset.id, Frame.segment_id.in_(replaced)))
            if deletable:
                s.execute(delete(Segment).where(Segment.id.in_(deletable)))
            for result in new_segments:
                if result.id in reused_ids:
                    row = latest.get(result.id)
                    if row is None:
                        raise RuntimeError('Segment removed during analysis; retry detection')
                    needs_review += int(_apply_algorithm_result(row, result))
                else:
                    s.add(result)
            s.flush()
            for frame in new_frames:
                s.add(frame)
            row_asset = s.get(Asset, asset.id)
            if row_asset is None:
                raise RuntimeError('Asset removed during analysis')
            row_asset.analysis_version = ANALYSIS_VERSION
            row_asset.analyzed_fingerprint = signature_before
            row_asset.analyzed_source_signature = source_state['quick_signature']
            row_asset.analyzed_detect_config = _detect_config_key(settings)

    except Exception:
        # A failed transaction retains the old rows. Reclaim only this run's
        # newly published files, leaving crash leftovers to the grace cleaner.
        for name in produced:
            try:
                (work_dir / name).unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove failed publication", exc_info=True)
        shutil.rmtree(stage, ignore_errors=True)
        raise

    # Garbage-collect JPEGs from the previous run that are no longer referenced
    # (audit P1-03): repeated detection now converges to a stable disk footprint.
    kept = _referenced_frame_paths(project_id)
    freed = 0
    for rel_path in previous_paths:
        try:
            candidate = resolve_in_project(project_id, rel_path)
        except ValueError:
            continue
        if candidate in kept:
            continue
        try:
            if safe_is_file(candidate) and candidate.parent == work_dir.resolve():
                candidate.unlink()
                freed += 1
        except OSError:
            logger.debug("Could not remove stale frame %s", candidate)

    freed += cleanup_orphan_frames(project_id)

    return {"segments": len(new_segments), "frames": len(new_frames), "frames_reclaimed": freed, "analysis_run_id": run_id, "needs_review": needs_review}


def detect_settings(config: dict):
    from types import SimpleNamespace
    defaults = get_settings()
    return SimpleNamespace(
        detect_sample_fps=config.get('sample_fps', defaults.detect_sample_fps),
        detect_threshold=config.get('threshold', defaults.detect_threshold),
        detect_max_segment_sec=config.get('max_segment_sec', defaults.detect_max_segment_sec),
        thumb_width=config.get('thumb_width', defaults.thumb_width),
        full_hash=bool(config.get('full_hash', False)),
        strict_full_hash=bool(config.get('strict_full_hash', False)),
    )


def detect_manifest(project_id: str, config: dict, *, job_id=None) -> list[dict]:
    from .checkpoints import manifest_entry
    with session_scope() as s:
        assets = s.scalars(select(Asset).where(
            Asset.project_id == project_id, Asset.scan_status == 'succeeded')
            .order_by(Asset.id)).all()
        result = []
        for asset in assets:
            if job_id:
                raise_if_cancelled(job_id)
            result.append(manifest_entry(asset.id, asset.source_path, full_hash=bool(config.get('full_hash'))))
        return result


@register_task("detect")
def detect_task(job_id: str) -> None:
    from .checkpoints import reconcile, persist, prepare_item, fail_item
    from .runner import JobCancelled
    failures = JobFailures(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        config = dict(job.config_snapshot or {})
    settings = detect_settings(config)
    config_key = _detect_config_key(settings)
    # Freeze missing settings for pre-v2.0.1 jobs on their first recovery.
    config.update(sample_fps=settings.detect_sample_fps, threshold=settings.detect_threshold,
                  max_segment_sec=settings.detect_max_segment_sec, thumb_width=settings.thumb_width)
    update_job(job_id, config_snapshot=config)
    items, token = reconcile(job_id, detect_manifest(project_id, config, job_id=job_id))
    force = bool(config.get('force', False))
    for item in items:
        raise_if_cancelled(job_id)
        if item['status'] in ('completed', 'skipped'):
            continue
        aid = item['key']
        try:
            prepare_item(job_id, item, items, token, config)
            item['detect_signature'] = item['scan_signature'] + ':' + config_key
            with session_scope() as s:
                asset = s.get(Asset, aid)
                if asset is None:
                    raise RuntimeError('Source asset removed')
                s.expunge(asset)
            # Refresh duration/codec metadata too when an indexed source changed.
            # Legacy/mocked assets with no completed scan retain their old path.
            from .indexer import quick_fingerprint
            stat = safe_stat(asset.source_path)
            metadata_changed = (asset.file_size_bytes != stat.st_size
                                or asset.mtime_ns != stat.st_mtime_ns
                                or asset.quick_fingerprint != quick_fingerprint(Path(asset.source_path), stat.st_size))
            if (asset.scanned_at is not None and (metadata_changed or
                    (asset.analyzed_fingerprint and asset.analyzed_fingerprint != item['scan_signature']))):
                from .indexer import index_one
                _, action = index_one(project_id, Path(asset.source_path), force=True)
                if action == 'failed':
                    raise RuntimeError('Changed source could not be probed')
                with session_scope() as s:
                    asset = s.get(Asset, aid)
                    s.expunge(asset)
            unchanged = (not force and asset.analysis_version == ANALYSIS_VERSION
                         and asset.analyzed_fingerprint == item['scan_signature']
                         and asset.analyzed_detect_config == config_key)
            if unchanged:
                if not asset.analyzed_source_signature:
                    with session_scope() as s:
                        begin_write(s)
                        row = s.get(Asset, aid)
                        if row.analyzed_fingerprint == item['scan_signature']:
                            row.analyzed_source_signature = item['quick_signature']
                item['status'] = 'skipped'
            else:
                settings.signature_context = {'item': item, 'items': items, 'token': token, 'config': config}
                result = _analyze_asset(project_id, asset, settings=settings, job_id=job_id)
                if result and result.get('needs_review'):
                    item['needs_review'] = result['needs_review']
                    token['warning'] = '检测边界变化影响已有人工镜头或引用，请在分镜审阅中复核；旧镜头已保留'
                with session_scope() as s:
                    row = s.get(Asset, aid)
                    row.analyzed_at = utcnow()
                item['status'] = 'completed'
            failures.resolve(kind='asset', ref=aid)
        except JobCancelled:
            raise
        except FFmpegCancelled:
            # TSB-253: the sampling/extraction ffmpeg was stopped because the job
            # was cancelled. Treat it as a cancellation and abort the detect run
            # rather than recording a bogus per-asset failure and moving on.
            raise JobCancelled('Detect cancelled by user') from None
        except Exception as exc:
            logger.exception('Analyze failed for asset %s', aid)
            fail_item(item, exc, token)
            failures.record(kind='asset', ref=aid, message=str(exc))
        persist(job_id, items, token)
