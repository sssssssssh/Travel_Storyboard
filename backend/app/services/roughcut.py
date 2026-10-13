"""M4 — proxy rough cut: concatenate the confirmed edit plan into a single
720p preview for quick pacing review. Source mapping is preserved; this is a
preview, not the final high-res output.

Audio: the source audio is kept (resampled to a common layout) so the cut can
be judged with ambient sound. Clips whose source has no audio get a silent
track of matching length, because ``concat`` requires uniform streams.
"""

from __future__ import annotations

import logging
import uuid
import shutil
from pathlib import Path

from sqlalchemy import select

from ..config import get_settings
from ..database import session_scope
from ..ffmpeg import locate_ffmpeg, run
from ..models import Asset, EditPlanShot, Job
from ..paths import project_cache_dir, rel_to_data
from .runner import JobFailures, raise_if_cancelled, register_task, update_job

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2


def _clamp_duration(shot: EditPlanShot, asset: Asset) -> float:
    """Planned duration, never longer than the source actually has.

    Audit P1-05: requesting ``target_duration_sec`` blindly could overrun the
    clip (or the file) and produce a short/black segment. Without a speed-change
    field, the source span is a hard ceiling.
    """
    span = max(0.0, shot.source_out_sec - shot.source_in_sec)
    planned = shot.target_duration_sec if shot.target_duration_sec > 0 else span
    duration = min(planned, span) if span > 0 else planned

    # And never past the end of the underlying file.
    available = max(0.0, (asset.duration_sec or 0.0) - shot.source_in_sec)
    if available > 0:
        duration = min(duration, available)
    return max(0.0, duration)


def _extract_normalized(source: str, start: float, dur: float, out: Path, has_audio: bool) -> None:
    width, height = 1280, 720
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,"
        f"tpad=stop_mode=clone:stop_duration={dur:.6f},trim=duration={dur:.6f},setpts=PTS-STARTPTS"
    )
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-ss", f"{start:.3f}",
        "-i", source,
    ]
    if not has_audio:
        # Give concat a uniform stream layout: silence of the same length.
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={SAMPLE_RATE}"]
    cmd += [
        "-t", f"{dur:.3f}",
        "-vf", vf,
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += [
            "-map", "0:a:0",
            "-af", f"aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                   f"apad,atrim=duration={dur:.6f},asetpts=PTS-STARTPTS",
            "-c:a", "aac",
            "-b:a", "128k",
        ]
    else:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
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

    stage = work / ("staging-" + str(uuid.uuid4()))
    stage.mkdir()
    clips: list[Path] = []
    total = len(pairs)
    done = 0
    failed = 0
    planned_total = 0.0
    actual_total = 0.0
    clamped: list[dict] = []
    for i, (sh, asset) in enumerate(pairs):
        raise_if_cancelled(job_id)
        planned_total += max(
            0.0,
            sh.target_duration_sec
            if sh.target_duration_sec > 0
            else sh.source_out_sec - sh.source_in_sec,
        )
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
        duration = _clamp_duration(sh, asset)
        if duration <= 0:
            failed += 1
            failures.record(
                kind="shot",
                ref=sh.id,
                message=f"shot #{sh.order}: 有效时长 <= 0（源区间或计划时长异常）",
            )
            done += 1
            update_job(job_id, stats={"total": total, "done": done, "failed": failed})
            continue
        clip = stage / f"clip_{i:04d}.mp4"
        try:
            _extract_normalized(
                asset.source_path, sh.source_in_sec, duration, clip, bool(asset.has_audio)
            )
            clips.append(clip)
            actual_total += duration
            if duration + 1e-3 < sh.target_duration_sec:
                clamped.append(
                    {
                        "shot": sh.order,
                        "planned": round(sh.target_duration_sec, 3),
                        "used": round(duration, 3),
                    }
                )
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

    list_file = stage / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for c in clips:
            escaped = c.as_posix().replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    # A rough cut missing shots must never look like the finished edit
    # (audit P1-08): the filename says so and the job ends partial_failed.
    incomplete = failed > 0
    out = work / ("roughcut_INCOMPLETE.mp4" if incomplete else "roughcut.mp4")
    temporary = stage / out.name
    raise_if_cancelled(job_id)
    run(
        [
            locate_ffmpeg(),
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(temporary),
        ]
    )
    raise_if_cancelled(job_id)
    temporary.replace(out)
    shutil.rmtree(stage, ignore_errors=True)
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
            # Lets the UI explain any drift from the saved plan's total.
            "planned_duration_sec": round(planned_total, 3),
            "actual_duration_sec": round(actual_total, 3),
            "clamped_shots": clamped,
        },
    )
