"""M2 — segment detection + representative frames + quality, per asset.

The pipeline for one asset:
1. detect boundaries (detector.py)
2. select representative frame times (frames.py)
3. extract full-res thumbnails via ffmpeg
4. compute quality metrics + perceptual hash
5. flag near-duplicates within the asset
6. persist segments/frames, then atomically swap staging into place and
   garbage-collect the previous run's JPEGs.

Manual state survives re-runs: segments are matched on their immutable detected
bounds (not the user-editable in/out), so trimming a clip does not orphan its
rating, note, review requests or edit-plan references.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select

from ..config import ANALYSIS_VERSION, get_settings
from ..database import session_scope
from ..ffmpeg import extract_frame
from ..models import Asset, Frame, Job, Project, Segment, utcnow
from ..paths import frames_dir, rel_to_data, resolve_in_project, staging_dir
from .detector import detect_segments
from .frames import select_rep_times
from .quality import analyze_frame, hamming, phash
from .runner import (
    JobFailures,
    raise_if_cancelled,
    register_task,
    update_job,
)

logger = logging.getLogger(__name__)

DUPLICATE_HAMMING_THRESHOLD = 6

# Shortest acceptable trimmed range; below this a stale trim is discarded.
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


def _analyze_asset(project_id: str, asset: Asset, settings=None) -> dict:
    settings = settings or get_settings()
    source = asset.source_path
    if not Path(source).exists():
        raise RuntimeError(f"Source file missing: {source}")

    detection = detect_segments(
        source,
        asset.duration_sec,
        sample_fps=settings.detect_sample_fps,
        threshold=settings.detect_threshold,
        max_segment_sec=settings.detect_max_segment_sec,
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

    work_dir = frames_dir(project_id, asset.id)
    stage = staging_dir(project_id, asset.id)
    # Start from a clean staging area; frames are moved into work_dir only after
    # the whole asset has been analysed successfully (audit P1-03), so a failure
    # leaves the previous, still-referenced JPEGs untouched.
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    new_segments: list[Segment] = []
    new_frames: list[Frame] = []
    reused_ids: set[str] = set()
    phash_pairs: list[tuple[str, str]] = []

    for boundary in detection.boundaries:
        rep_times = select_rep_times(
            boundary.start_sec,
            boundary.end_sec,
            detection.sample_times,
            detection.sample_diffs,
        )
        key = _time_key(boundary.start_sec, boundary.end_sec)
        prior = old_by_key.get(key)
        if prior is not None:
            segment = prior
            reused_ids.add(prior.id)
            # Refresh derived/algorithmic fields…
            segment.detected_start_sec = boundary.start_sec
            segment.detected_end_sec = boundary.end_sec
            segment.boundary_reason = boundary.reason
            segment.algorithm_version = ANALYSIS_VERSION
            segment.proxy_ref = ""
            # …but keep the user's trim. Only re-clamp it to the new bounds.
            if not segment.is_trimmed:
                segment.start_sec = boundary.start_sec
                segment.end_sec = boundary.end_sec
            else:
                low = max(boundary.start_sec, min(segment.start_sec, boundary.end_sec))
                high = min(boundary.end_sec, max(segment.end_sec, boundary.start_sec))
                if high - low < MIN_SEGMENT_SEC:
                    # The trim no longer fits inside the detected window; fall
                    # back to the detected bounds rather than keeping a
                    # degenerate range.
                    segment.start_sec = boundary.start_sec
                    segment.end_sec = boundary.end_sec
                    segment.is_trimmed = False
                else:
                    segment.start_sec = low
                    segment.end_sec = high
        else:
            segment = Segment(
                id=str(uuid.uuid4()),
                asset_id=asset.id,
                project_id=project_id,
                detected_start_sec=boundary.start_sec,
                detected_end_sec=boundary.end_sec,
                start_sec=boundary.start_sec,
                end_sec=boundary.end_sec,
                boundary_reason=boundary.reason,
                algorithm_version=ANALYSIS_VERSION,
            )

        # Extract representative frames.
        frame_refs: list[str] = []
        quality_metrics: list[dict] = []
        for t in rep_times:
            frame_id = str(uuid.uuid4())
            stage_path = stage / f"{frame_id}.jpg"
            final_path = work_dir / f"{frame_id}.jpg"
            extract_frame(source, t, stage_path, width=settings.thumb_width)
            with Image.open(stage_path) as im:
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
                    file_size_bytes=stage_path.stat().st_size,
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

    # Promote staged frames into their final location, then persist. Doing the
    # filesystem swap before the DB write means a crash mid-swap leaves either
    # the old or the new set complete — never a half-written view.
    work_dir.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        previous_paths = [
            f.image_path
            for f in s.scalars(select(Frame).where(Frame.asset_id == asset.id)).all()
            if f.image_path
        ]

    produced = {f"{frame.id}.jpg" for frame in new_frames}
    for name in produced:
        src = stage / name
        if src.exists():
            src.replace(work_dir / name)
    shutil.rmtree(stage, ignore_errors=True)

    # Persist.
    with session_scope() as s:
        # Delete frames for this asset (frames carry no manual marks).
        s.execute(delete(Frame).where(Frame.asset_id == asset.id))
        # Delete old segments that were not reused.
        removed_ids = old_ids - reused_ids
        if removed_ids:
            s.execute(delete(Segment).where(Segment.id.in_(removed_ids)))
        for segment in new_segments:
            s.merge(segment)
        # Flush segments first so the frames' foreign keys resolve.
        s.flush()
        for frame in new_frames:
            s.add(frame)
        asset.analysis_version = ANALYSIS_VERSION
        from .checkpoints import source_signature
        asset.analyzed_fingerprint = source_signature(Path(source), full_hash=getattr(settings, "full_hash", False))
        asset.analyzed_detect_config = _detect_config_key(settings)
        s.merge(asset)

    # Garbage-collect JPEGs from the previous run that are no longer referenced
    # (audit P1-03): repeated detection now converges to a stable disk footprint.
    kept = {str((work_dir / name).resolve()) for name in produced}
    freed = 0
    for rel_path in previous_paths:
        try:
            candidate = resolve_in_project(project_id, rel_path)
        except ValueError:
            continue
        if str(candidate) in kept:
            continue
        try:
            if candidate.is_file() and candidate.parent == work_dir.resolve():
                candidate.unlink()
                freed += 1
        except OSError:
            logger.debug("Could not remove stale frame %s", candidate)

    # Remove any other orphaned JPEGs in the frames dir.
    for leftover in work_dir.glob("*.jpg"):
        if leftover.name not in produced:
            try:
                leftover.unlink()
                freed += 1
            except OSError:
                pass

    return {"segments": len(new_segments), "frames": len(new_frames), "frames_reclaimed": freed}


def detect_settings(config: dict):
    from types import SimpleNamespace
    defaults = get_settings()
    return SimpleNamespace(
        detect_sample_fps=config.get('sample_fps', defaults.detect_sample_fps),
        detect_threshold=config.get('threshold', defaults.detect_threshold),
        detect_max_segment_sec=config.get('max_segment_sec', defaults.detect_max_segment_sec),
        thumb_width=config.get('thumb_width', defaults.thumb_width),
        full_hash=bool(config.get('full_hash', False)),
    )


def detect_manifest(project_id: str, config: dict) -> list[dict]:
    from .checkpoints import manifest_entry
    with session_scope() as s:
        assets = s.scalars(select(Asset).where(
            Asset.project_id == project_id, Asset.scan_status == 'succeeded')
            .order_by(Asset.id)).all()
        return [manifest_entry(a.id, a.source_path, full_hash=bool(config.get('full_hash')))
                for a in assets]


@register_task("detect")
def detect_task(job_id: str) -> None:
    from .checkpoints import reconcile, persist, source_signature
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
    items, token = reconcile(job_id, detect_manifest(project_id, config))
    force = bool(config.get('force', False))
    for item in items:
        raise_if_cancelled(job_id)
        if item['status'] in ('completed', 'skipped'):
            continue
        aid = item['key']
        item['detect_signature'] = item['scan_signature'] + ':' + config_key
        try:
            if item.get('source_error'):
                raise RuntimeError(item['source_error'])
            with session_scope() as s:
                asset = s.get(Asset, aid)
                if asset is None:
                    raise RuntimeError('Source asset removed')
                s.expunge(asset)
            unchanged = (not force and asset.analysis_version == ANALYSIS_VERSION
                         and asset.analyzed_fingerprint == item['scan_signature']
                         and asset.analyzed_detect_config == config_key)
            if unchanged:
                item['status'] = 'skipped'
            else:
                _analyze_asset(project_id, asset, settings=settings)
                if source_signature(Path(asset.source_path), full_hash=settings.full_hash) != item['scan_signature']:
                    raise RuntimeError('源文件已变化：检测期间内容改变，请重试')
                with session_scope() as s:
                    row = s.get(Asset, aid)
                    row.analyzed_at = utcnow()
                item['status'] = 'completed'
            failures.resolve(kind='asset', ref=aid)
        except JobCancelled:
            raise
        except Exception as exc:
            logger.exception('Analyze failed for asset %s', aid)
            item['status'] = 'failed'
            failures.record(kind='asset', ref=aid, message=str(exc))
        persist(job_id, items, token)
