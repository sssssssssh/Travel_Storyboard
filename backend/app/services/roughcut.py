"""M4 — proxy rough cut: concatenate the confirmed edit plan into a single
720p preview (no effects) for quick pacing review. Source mapping is preserved;
this is a preview, not the final high-res output."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..database import session_scope
from ..ffmpeg import locate_ffmpeg, run
from ..models import Asset, EditPlanShot, Job
from ..paths import project_cache_dir, rel_to_data
from .runner import JobFailures, raise_if_cancelled, register_task, update_job

logger = logging.getLogger(__name__)


def _extract_normalized(source: str, start: float, dur: float, out: Path) -> None:
    width, height = 1280, 720
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30"
    )
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-ss", f"{start:.3f}",
        "-i", source,
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    run(cmd)


@register_task("roughcut")
def roughcut_task(job_id: str) -> None:
    failures = JobFailures(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id

    with session_scope() as s:
        shots = s.scalars(
            select(EditPlanShot)
            .where(EditPlanShot.project_id == project_id)
            .order_by(EditPlanShot.order)
        ).all()
        assets = {
            a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()
        }
        pairs = [(sh, assets.get(sh.asset_id)) for sh in shots]

    work = project_cache_dir(project_id) / "roughcut"
    work.mkdir(parents=True, exist_ok=True)

    clips: list[Path] = []
    total = len(pairs)
    done = 0
    failed = 0
    for i, (sh, asset) in enumerate(pairs):
        raise_if_cancelled(job_id)
        if asset is None:
            failed += 1
            failures.record(
                kind="shot",
                ref=sh.id,
                message=f"shot #{sh.order}: referenced asset {sh.asset_id} is missing",
            )
            done += 1
            update_job(job_id, stats={"total": total, "done": done, "failed": failed})
            continue
        dur = sh.target_duration_sec or (sh.source_out_sec - sh.source_in_sec)
        clip = work / f"clip_{i:04d}.mp4"
        try:
            _extract_normalized(asset.source_path, sh.source_in_sec, dur, clip)
            clips.append(clip)
        except Exception as exc:  # noqa: BLE001 - keep going on other clips
            logger.exception("Rough-cut clip failed for shot %s", sh.id)
            failed += 1
            failures.record(
                kind="shot",
                ref=sh.id,
                message=f"shot #{sh.order} ({asset.display_name}): {exc}",
            )
        done += 1
        update_job(job_id, stats={"total": total, "done": done, "failed": failed})

    if not clips:
        raise RuntimeError("No clips were produced for the rough cut")

    list_file = work / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            f.write(f"file '{c.as_posix()}'\n")

    # A rough cut missing shots must never look like the finished edit
    # (audit P1-08): the filename says so and the job ends partial_failed.
    incomplete = failed > 0
    out = work / ("roughcut_INCOMPLETE.mp4" if incomplete else "roughcut.mp4")
    run(
        [
            locate_ffmpeg(),
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out),
        ]
    )
    # Drop any stale file from the other naming variant so /roughcut is unambiguous.
    stale = work / ("roughcut.mp4" if incomplete else "roughcut_INCOMPLETE.mp4")
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass

    update_job(
        job_id,
        resume_token={
            "path": rel_to_data(out),
            "size": out.stat().st_size,
            "incomplete": incomplete,
            "shots_included": len(clips),
            "shots_total": total,
            "missing_shots": [item["ref"] for item in failures.items],
        },
    )
