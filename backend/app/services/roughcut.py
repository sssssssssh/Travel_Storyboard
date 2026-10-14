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
from ..database import begin_write, session_scope
from ..ffmpeg import (
    FFmpegCancelled,
    locate_ffmpeg,
    long_path,
    run,
    safe_is_file,
    safe_mkdir,
    safe_replace,
    safe_stat,
    safe_unlink,
    safe_write_text,
)
from ..models import Asset, EditPlanShot, Job, Segment
from ..paths import project_cache_dir, rel_to_data
from .runner import (
    JobCancelled,
    JobFailures,
    cancel_check_for,
    raise_if_cancelled,
    raise_if_cancelled_in_session,
    register_task,
    update_job,
)
from .staging_cleanup import cleanup_staging_or_log, create_owned_staging

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2


def _write_concat_manifest(list_file: Path, clips: list[Path]) -> None:
    """Write an ffconcat manifest that survives spaces, CJK and quotes.

    Entry form is **single-quoted**, verified against this project's FFmpeg:
    double quotes are NOT stripped by the concat demuxer — it looks for a file
    literally named ``"C:/...`` and fails with "Impossible to open". An unquoted
    entry breaks on the first space. So ``file '<path>'`` is the only form that
    works for the paths this app produces.

    Paths are written POSIX-style on purpose: ffconcat treats ``\\`` as an escape
    character, so a native Windows path is silently mangled (``C:\\Users``
    becomes ``C:Users``). FFmpeg accepts forward slashes on Windows.

    Do NOT apply ``long_path()`` inside the manifest. The ``\\\\?\\`` prefix is
    meaningful only on the ffmpeg *command line*; inside a manifest FFmpeg
    resolves entries itself and rejects the prefixed form.

    UTF-8 without a BOM: a BOM would corrupt the leading keyword.
    """
    lines = ["ffconcat version 1.0"]
    for clip in clips:
        raw = clip.as_posix()
        escaped = raw.replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    safe_write_text(list_file, "\n".join(lines) + "\n")


def _long_replace(src: Path, dst: Path) -> None:
    """Backwards-compatible alias for the shared helper."""
    safe_replace(src, dst)


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


def _extract_normalized(source: str, start: float, dur: float, out: Path,
                        has_audio: bool, cancel_check=None) -> None:
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
        "-i", long_path(source),
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
        long_path(out),
    ]
    run(cmd, expect_output=out, cancel_check=cancel_check)


@register_task("roughcut")
def roughcut_task(job_id: str) -> None:
    """Rough-cut entry point: guarantees this run's staging is collected.

    TSB-262: the staging directory used to be removed only on the success path,
    so a cancel, timeout or failed encode leaked every clip it had written. The
    cleanup now runs in ``finally`` — before the worker returns and therefore
    before the project mutex is released — and reports residual bytes instead of
    hiding them behind ``ignore_errors=True``.
    """
    project_id = _project_for_job(job_id)
    if project_id is None:
        return

    stage = project_cache_dir(project_id) / "roughcut" / ("staging-" + str(uuid.uuid4()))
    create_owned_staging(project_id, stage, owner_job_id=job_id)
    try:
        _roughcut_body(job_id, project_id, stage)
    finally:
        cleanup_staging_or_log(project_id, stage, owner_job_id=job_id)


def _project_for_job(job_id: str) -> str | None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        return job.project_id if job is not None else None


def _roughcut_body(job_id: str, project_id: str, stage: Path) -> None:
    failures = JobFailures(job_id)

    with session_scope() as s:
        shots = s.scalars(
            select(EditPlanShot)
            .where(EditPlanShot.project_id == project_id)
            .order_by(EditPlanShot.order)
        ).all()
        assets = {
            a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()
        }
        segments = {g.id: g for g in s.scalars(select(Segment).where(Segment.project_id == project_id)).all()}
        pairs = [(sh, assets.get(sh.asset_id)) for sh in shots]

    work = project_cache_dir(project_id) / "roughcut"
    safe_mkdir(work)

    # Cancelling the job must stop the in-flight encode, not wait it out.
    cancel_check = cancel_check_for(job_id)
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
        segment = segments.get(sh.segment_id)
        if segment is None or segment.asset_id != asset.id or segment.status == 'needs_review':
            failed += 1
            failures.record(kind='shot', ref=sh.id, message=f'shot #{sh.order}: 镜头源引用已变化或需要复核，请更新剪辑计划')
            done += 1
            update_job(job_id, stats={'total': total, 'done': done, 'failed': failed})
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
                asset.source_path, sh.source_in_sec, duration, clip,
                bool(asset.has_audio), cancel_check=cancel_check,
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
        except FFmpegCancelled:
            # Cancellation aborts the whole job; it must not be recorded as one
            # more failed clip and then be followed by more encodes (TSB-253).
            raise JobCancelled("Rough cut cancelled by user") from None
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
    _write_concat_manifest(list_file, clips)

    # A rough cut missing shots must never look like the finished edit
    # (audit P1-08): the filename says so and the job ends partial_failed.
    incomplete = failed > 0
    # Per-attempt immutable publication: historical rough cuts keep their bytes.
    label = "roughcut_INCOMPLETE" if incomplete else "roughcut"
    out = work / f"{label}-{uuid.uuid4().hex[:16]}.mp4"
    temporary = stage / out.name
    raise_if_cancelled(job_id)
    run(
        [
            locate_ffmpeg(),
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", long_path(list_file),
            "-c", "copy",
            long_path(temporary),
        ],
        expect_output=temporary,
        cancel_check=cancel_check,
    )
    # Re-check after the concat: a cancel during it must never publish (TSB-253).
    raise_if_cancelled(job_id)
    try:
        with session_scope() as s:
            begin_write(s)
            raise_if_cancelled_in_session(s, job_id)
            _long_replace(temporary, out)
            job = s.get(Job, job_id)
            job.resume_token = {
                "path": rel_to_data(out), "size": safe_stat(out).st_size,
                "incomplete": incomplete, "shots_included": len(clips),
                "shots_total": total,
                "missing_shots": [item["ref"] for item in failures.items],
                "planned_duration_sec": round(planned_total, 3),
                "actual_duration_sec": round(actual_total, 3), "clamped_shots": clamped,
            }
    except Exception:
        # Only the new, uncommitted publication is ours; never legacy outputs.
        safe_unlink(out)
        raise
