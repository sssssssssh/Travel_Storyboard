"""M2 — segment detection + representative frames + quality, per asset.

The pipeline for one asset:
1. detect boundaries (detector.py)
2. select representative frame times (frames.py)
3. extract full-res thumbnails via ffmpeg
4. compute quality metrics + perceptual hash
5. flag near-duplicates within the asset
6. persist segments/frames, preserving manual marks across re-runs by
   matching on (start, end) time windows.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select

from ..config import ANALYSIS_VERSION, get_settings
from ..database import session_scope
from ..ffmpeg import extract_frame
from ..models import Asset, Frame, Job, Project, Segment, utcnow
from ..paths import frames_dir, rel_to_data
from .detector import detect_segments
from .frames import select_rep_times
from .quality import analyze_frame, hamming, phash
from .runner import register_task, update_job

logger = logging.getLogger(__name__)

DUPLICATE_HAMMING_THRESHOLD = 6


def _time_key(start: float, end: float) -> tuple[int, int]:
    return (int(round(start, 1) * 10), int(round(end, 1) * 10))


def _analyze_asset(project_id: str, asset: Asset) -> dict:
    settings = get_settings()
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
    with session_scope() as s:
        old_segments = s.scalars(
            select(Segment).where(Segment.asset_id == asset.id)
        ).all()
        old_by_key = {_time_key(seg.start_sec, seg.end_sec): seg for seg in old_segments}
        old_ids = {seg.id for seg in old_segments}

    work_dir = frames_dir(project_id, asset.id)
    work_dir.mkdir(parents=True, exist_ok=True)

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
            # Reset derived fields, keep manual state.
            segment.start_sec = boundary.start_sec
            segment.end_sec = boundary.end_sec
            segment.boundary_reason = boundary.reason
            segment.algorithm_version = ANALYSIS_VERSION
            segment.proxy_ref = ""
        else:
            segment = Segment(
                id=str(uuid.uuid4()),
                asset_id=asset.id,
                project_id=project_id,
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
            out_path = work_dir / f"{frame_id}.jpg"
            extract_frame(source, t, out_path, width=settings.thumb_width)
            with Image.open(out_path) as im:
                fw, fh = im.size
            metrics = analyze_frame(out_path)
            ph = phash(out_path)
            rel = rel_to_data(out_path)
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
                    file_size_bytes=out_path.stat().st_size,
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
        s.merge(asset)

    return {"segments": len(new_segments), "frames": len(new_frames)}


@register_task("detect")
def detect_task(job_id: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        last_asset = job.resume_token.get("last_asset_id")
        s.expunge(job)

    with session_scope() as s:
        assets = s.scalars(
            select(Asset)
            .where(Asset.project_id == project_id, Asset.scan_status == "succeeded")
            .order_by(Asset.display_name)
        ).all()
        asset_ids = [a.id for a in assets]

    total = len(asset_ids)
    start_idx = 0
    if last_asset in asset_ids:
        start_idx = asset_ids.index(last_asset) + 1

    done = start_idx
    failed = 0
    for i in range(start_idx, total):
        asset_id = asset_ids[i]
        with session_scope() as s:
            asset = s.get(Asset, asset_id)
            if asset is None:
                continue
            s.expunge(asset)
        try:
            _analyze_asset(project_id, asset)
        except Exception as exc:  # noqa: BLE001 - per-asset isolation
            logger.exception("Analyze failed for asset %s", asset_id)
            failed += 1
        done += 1
        update_job(
            job_id,
            stats={"total": total, "done": done, "failed": failed},
            resume_token={"last_asset_id": asset_id},
        )
