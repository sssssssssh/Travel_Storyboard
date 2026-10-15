"""TSB-307 — timestamped multi-frame motion understanding.

Design decision the task book makes explicit: the first version does **not** use
a native video API. It samples N stills at known timestamps from inside the
shot's *valid* interval, sends them with their timestamps, and asks the model
what motion it can justify from those frames alone.

Consequences that the code enforces:

* every emitted frame carries its timestamp, so the model's answer is anchored
  to evidence rather than to an impression;
* the model is told which frames it saw and must answer ``unknown`` when the
  sample cannot support a conclusion;
* the result records ``method="multiframe"`` so no reader can mistake this for
  continuous-video analysis;
* audio is never analysed, and ``audio_analyzed`` stays false in the evidence.

Frames are extracted read-only from the source and re-encoded to strip metadata,
then removed; the source file is never written (C1).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update

from ...config import get_settings
from ...database import begin_write, session_scope
from ...ffmpeg import FFmpegError, extract_frame, safe_stat, safe_unlink
from ...models import Asset, Job, Segment, SegmentMotionReview, utcnow
from ...paths import project_cache_dir
from ..runner import JobCancelled, JobFailures, cancel_check_for, register_task, update_job
from .errors import (
    CANCELLED,
    INSUFFICIENT_FRAMES,
    MODEL_BUDGET_EXCEEDED,
    MODEL_NOT_CONFIGURED,
    MODEL_UPLOAD_DENIED,
    ModelError,
)

logger = logging.getLogger(__name__)

MOTION_TYPES = frozenset(
    {
        "static", "pan", "tilt", "tracking", "subject_action",
        "abrupt_change", "unknown",
    }
)
STABILITY_RISKS = frozenset({"low", "medium", "high", "unknown"})

#: Human verdicts. Kept separate from the model's own conclusion (C4).
HUMAN_VERDICTS = frozenset({"confirm", "reject", "unclear"})

MOTION_PROMPT_VERSION = "1.0"


@dataclass
class MotionFrame:
    path: str
    time_sec: float
    frame_id: str = ""

    @property
    def label(self) -> str:
        return f"t={self.time_sec:.2f}s"


def sample_times(start_sec: float, end_sec: float, count: int) -> list[float]:
    """Evenly spaced sample points strictly inside ``[start, end]``.

    Endpoints are inset slightly so a frame is never taken from the neighbouring
    shot (the decoded frame at exactly ``end`` may belong to the next cut).
    """
    span = max(0.0, end_sec - start_sec)
    if span <= 0:
        return [max(0.0, start_sec)]
    count = max(1, count)
    if count == 1:
        return [start_sec + span / 2.0]

    # Inset by half a step so the first/last samples sit inside the interval.
    step = span / (count + 1)
    return [round(start_sec + step * (i + 1), 3) for i in range(count)]


def stage_dir(project_id: str, job_id: str) -> Path:
    return project_cache_dir(project_id) / "vision" / "motion" / job_id


def build_prompt(manifest: list[dict], *, frame_count: int, audio_analyzed: bool) -> str:
    """Motion prompt. States the limits of the evidence up front."""
    lines = [
        "You are shown several STILL FRAMES sampled at known timestamps from ONE",
        "continuous shot of a travel video. They are not a video: you have no",
        "information about anything between the frames.",
        "",
        "RULES (all mandatory):",
        "- Judge only from the frames shown, and use their timestamps.",
        "- Do NOT claim a specific motion type if the sample cannot support it;",
        "  answer \"unknown\" instead. A wrong confident answer is worse than unknown.",
        "- You cannot know what happened between two frames.",
        "- You cannot hear or judge audio.",
        "- Never invent timestamps outside the range given below.",
        "",
        f"FRAMES SHOWN ({frame_count}):",
    ]
    for item in manifest:
        lines.append(f"  {item['label']} (segment_id={item['segment_id']})")
    lines += [
        "",
        "AUDIO: not analysed. Do not comment on sound." if not audio_analyzed else "",
        "",
        "Return ONE JSON object and nothing else:",
        "{",
        '  "motion_type": "<static|pan|tilt|tracking|subject_action|abrupt_change|unknown>",',
        '  "stability_risk": "<low|medium|high|unknown>",',
        '  "action_summary": "<what changes across the frames, or why unknown>",',
        '  "evidence_timestamps_sec": [<timestamps you actually relied on>],',
        '  "limitations": ["<e.g. frames too far apart to judge continuity>"],',
        '  "needs_human_review": <true|false>',
        "}",
    ]
    return "\n".join(line for line in lines if line is not None)


SYSTEM_PROMPT = (
    "You are a precise motion annotator working from a small set of timestamped "
    "stills. You answer with strict JSON only, and you answer \"unknown\" rather "
    "than guessing what happened between frames."
)


def extract_json_object(raw: str) -> dict:
    """Recover the JSON object, tolerating fences and surrounding prose."""
    text = (raw or "").strip()
    if not text:
        raise ModelError("MODEL_INVALID_OUTPUT", "empty motion response")
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ModelError("MODEL_INVALID_OUTPUT", "no JSON object in motion response") from None
        try:
            return json.loads(text[start : end + 1])
        except ValueError as exc:
            raise ModelError("MODEL_INVALID_OUTPUT", f"motion JSON unparseable: {exc}") from exc


def validate_motion(payload: dict, allowed_timestamps: list[float]) -> dict:
    """Normalise a motion response against the server's own frame times."""
    if not isinstance(payload, dict):
        raise ModelError("MODEL_INVALID_OUTPUT", "motion result is not an object")

    motion_type = str(payload.get("motion_type") or "unknown").strip().lower().replace(" ", "_")
    if motion_type not in MOTION_TYPES:
        motion_type = "unknown"

    risk = str(payload.get("stability_risk") or "unknown").strip().lower()
    if risk not in STABILITY_RISKS:
        risk = "unknown"

    summary = str(payload.get("action_summary") or "").strip()[:2000]
    limitations = [
        str(x).strip()[:300]
        for x in (payload.get("limitations") or [])
        if str(x).strip()
    ][:8]

    # A timestamp the model reports must be one we actually sent. Anything else
    # is an invented observation and is dropped.
    reported: list[float] = []
    for value in payload.get("evidence_timestamps_sec") or []:
        try:
            num = round(float(value), 3)
        except (TypeError, ValueError):
            continue
        if any(abs(num - t) <= 0.05 for t in allowed_timestamps) and num not in reported:
            reported.append(num)
    if not reported:
        reported = list(allowed_timestamps)

    needs_human = bool(payload.get("needs_human_review"))
    # An "unknown" verdict always deserves a human look; make that explicit
    # rather than leaving a silent gap.
    if motion_type == "unknown" and not needs_human:
        needs_human = True
        limitations = limitations + ["motion_type_unknown"]

    return {
        "motion_type": motion_type,
        "stability_risk": risk,
        "action_summary": summary,
        "evidence_timestamps_sec": reported,
        "limitations": limitations,
        "needs_human_review": needs_human,
    }


def motion_review_task(job_id: str) -> None:
    """Worker entry point for the ``motion_review`` job."""
    from . import get_provider, is_configured

    settings = get_settings()
    cancel_check = cancel_check_for(job_id)
    failures = JobFailures(job_id)

    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        config = dict(job.config_snapshot or {})

    if not bool(config.get("upload_acknowledged")) or settings.model_mode != "on":
        update_job(job_id, status="failed",
                   error=f"{MODEL_UPLOAD_DENIED}: 未确认上传或视觉模型未启用")
        return
    if not is_configured():
        update_job(job_id, status="failed", error=f"{MODEL_NOT_CONFIGURED}: 视觉模型未配置")
        return

    segment_ids = list(config.get("segment_ids") or [])
    frame_count = max(2, int(config.get("frame_count") or settings.motion_frame_count))
    limit = int(config.get("limit") or settings.vision_max_segments_per_job)

    targets = _select_targets(project_id, segment_ids, limit)
    if not targets:
        update_job(job_id, status="failed",
                   error=f"{INSUFFICIENT_FRAMES}: 没有可复核的镜头")
        return

    provider = get_provider()
    staging = stage_dir(project_id, job_id)
    counts = {"analyzed": 0, "failed": 0, "frames": 0, "skipped": 0}
    total = len(targets)

    update_job(job_id, status="running",
               stats={"total": total, "done": 0, "failed": 0, "phase": "sampling"})

    try:
        for index, segment in enumerate(targets, start=1):
            if cancel_check():
                raise JobCancelled("motion_review cancelled")
            try:
                frames = _sample_frames(project_id, job_id, segment, staging,
                                        frame_count, cancel_check)
                if len(frames) < 2:
                    counts["failed"] += 1
                    failures.record(
                        kind="motion", ref=segment["segment_id"],
                        message=f"[{INSUFFICIENT_FRAMES}] 只能抽取 {len(frames)} 帧，"
                                "不足以判断运动",
                        error_code=INSUFFICIENT_FRAMES,
                    )
                    _progress(job_id, index, total, counts)
                    continue

                result, usage = _analyze_motion(provider, segment, frames, cancel_check)
                _store(project_id, segment, result, frames,
                       provider_name=provider.name, model_id=settings.model_name,
                       usage=usage)
                counts["analyzed"] += 1
                counts["frames"] += len(frames)
            except ModelError as exc:
                if exc.code == CANCELLED:
                    raise JobCancelled("motion_review cancelled") from exc
                counts["failed"] += 1
                failures.record(kind="motion", ref=segment["segment_id"],
                                message=f"[{exc.code}] {exc.message}", error_code=exc.code)
            except FFmpegError as exc:
                counts["failed"] += 1
                failures.record(kind="motion", ref=segment["segment_id"],
                                message=f"[{exc.code}] {exc}", error_code=exc.code)
            _progress(job_id, index, total, counts)
    finally:
        _cleanup(staging)

    stats = {
        "total": total,
        "done": counts["analyzed"],
        "failed": counts["failed"],
        "analyzed": counts["analyzed"],
        "frames_sampled": counts["frames"],
        "method": "multiframe",
        "phase": "done",
    }
    if len(failures):
        update_job(job_id, status="partial_failed", stats=stats,
                   error=f"{len(failures)} 个镜头动态复核失败")
    else:
        update_job(job_id, status="succeeded", stats=stats)


def _select_targets(project_id: str, segment_ids: list[str], limit: int) -> list[dict]:
    """Shots to review, honouring an explicit id list when given."""
    from .semantic import INVALID_SEGMENT_STATUSES

    with session_scope() as s:
        stmt = (
            select(Segment, Asset)
            .join(Asset, Asset.id == Segment.asset_id)
            .where(
                Segment.project_id == project_id,
                Segment.status.not_in(INVALID_SEGMENT_STATUSES),
                Segment.user_rating != "exclude",
            )
        )
        if segment_ids:
            stmt = stmt.where(Segment.id.in_(segment_ids))
        rows = s.execute(stmt).all()

        out = []
        for segment, asset in rows:
            out.append(
                {
                    "segment_id": segment.id,
                    "asset_id": segment.asset_id,
                    "start_sec": float(segment.start_sec),
                    "end_sec": float(segment.end_sec),
                    "source_path": asset.source_path,
                    "source_duration": float(asset.duration_sec or 0.0),
                    "has_audio": bool(asset.has_audio),
                    "fps_num": int(asset.fps_num or 0),
                    "fps_den": int(asset.fps_den or 1),
                }
            )
    return out[: max(0, limit)]


def _sample_frames(project_id, job_id, segment, staging, frame_count, cancel_check):
    """Extract ``frame_count`` timestamped frames from inside the valid interval."""
    settings = get_settings()
    start = max(0.0, segment["start_sec"])
    end = segment["end_sec"]
    duration = segment["source_duration"]
    if duration > 0:
        # Never seek past the end of the file (source could have changed).
        end = min(end, duration)
    if end <= start:
        return []

    times = sample_times(start, end, frame_count)
    frames: list[MotionFrame] = []
    for i, t in enumerate(times):
        dest = staging / f"{segment['segment_id'][:8]}-{i:02d}.jpg"
        try:
            extract_frame(
                segment["source_path"], t, dest,
                width=settings.vision_target_width, cancel_check=cancel_check,
            )
        except FFmpegError as exc:
            logger.warning("Frame at %.2fs failed for %s: %s", t, segment["segment_id"], exc)
            continue
        # Re-encode through Pillow so the upload carries no EXIF/GPS (C5).
        if _strip_metadata(dest, settings.vision_target_width):
            frames.append(MotionFrame(path=str(dest), time_sec=t))
    return frames


def _strip_metadata(path: Path, target_width: int) -> bool:
    """Re-encode an extracted frame in place, dropping all metadata."""
    from PIL import Image

    from ...ffmpeg import safe_image_open

    try:
        with safe_image_open(str(path)) as img:
            clean = img.convert("RGB")
            if target_width and clean.width > target_width:
                ratio = target_width / float(clean.width)
                clean = clean.resize(
                    (target_width, max(1, int(clean.height * ratio))), Image.LANCZOS
                )
            clean.save(path, format="JPEG", quality=85, optimize=True)
        return safe_stat(path) is not None
    except (OSError, ValueError) as exc:
        logger.warning("Could not strip metadata from %s: %s", path, exc)
        return False


def _analyze_motion(provider, segment, frames, cancel_check):
    from .providers.base import ImagePart, VisionRequest

    manifest = [
        {"label": f.label, "segment_id": segment["segment_id"]} for f in frames
    ]
    prompt = build_prompt(
        manifest,
        frame_count=len(frames),
        audio_analyzed=False,
    )
    # Technical context the product already knows; useful, and clearly separate
    # from anything the model inferred.
    prompt += (
        f"\n\nSHOT CONTEXT (measured by the tool, not by you): "
        f"{segment['start_sec']:.2f}-{segment['end_sec']:.2f}s, "
        f"audio_present={segment['has_audio']} (audio content NOT analysed)."
    )

    response = provider.complete(
        VisionRequest(
            prompt=prompt,
            images=[ImagePart(path=f.path, label=f.label) for f in frames],
            system=SYSTEM_PROMPT,
            json_object=True,
            max_tokens=1200,
        ),
        cancel_check=cancel_check,
    )

    payload = extract_json_object(response.text)
    result = validate_motion(payload, [f.time_sec for f in frames])
    return result, response.usage


def _store(project_id, segment, result, frames, *, provider_name, model_id, usage):
    """Persist a motion reading, preserving any human verdict already recorded."""
    with session_scope() as s:
        begin_write(s)
        s.execute(
            update(SegmentMotionReview)
            .where(
                SegmentMotionReview.segment_id == segment["segment_id"],
                SegmentMotionReview.is_current.is_(True),
            )
            .values(is_current=False)
        )
        s.add(
            SegmentMotionReview(
                project_id=project_id,
                asset_id=segment["asset_id"],
                segment_id=segment["segment_id"],
                method="multiframe",
                status="succeeded",
                model_provider=provider_name,
                model_id=model_id,
                prompt_version=MOTION_PROMPT_VERSION,
                start_sec=segment["start_sec"],
                end_sec=segment["end_sec"],
                frame_manifest_hash=_frames_hash(frames),
                motion_type=result["motion_type"],
                stability_risk=result["stability_risk"],
                action_summary=result["action_summary"],
                analysis_json=result,
                evidence_json={
                    "method": "multiframe",
                    "frames": [{"time_sec": f.time_sec, "frame_id": f.frame_id} for f in frames],
                    "evidence_timestamps_sec": result["evidence_timestamps_sec"],
                    "limitations": result["limitations"],
                    "needs_human_review": result["needs_human_review"],
                    "audio_analyzed": False,
                    "video_analyzed": False,
                },
                request_usage=usage or {},
                is_current=True,
                updated_at=utcnow(),
            )
        )


def _frames_hash(frames: list[MotionFrame]) -> str:
    import hashlib

    payload = json.dumps([round(f.time_sec, 3) for f in frames], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:64]


def record_human_verdict(segment_id: str, verdict: str, note: str = "") -> bool:
    """Record a person's judgement on the current motion reading (C4)."""
    if verdict not in HUMAN_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(HUMAN_VERDICTS)}")
    with session_scope() as s:
        begin_write(s)
        row = s.scalars(
            select(SegmentMotionReview).where(
                SegmentMotionReview.segment_id == segment_id,
                SegmentMotionReview.is_current.is_(True),
            )
        ).first()
        if row is None:
            return False
        row.human_verdict = verdict
        row.human_note = (note or "")[:2000]
        row.human_at = utcnow()
    return True


def _progress(job_id: str, index: int, total: int, counts: dict) -> None:
    update_job(job_id, stats={
        "total": total,
        "done": counts["analyzed"],
        "failed": counts["failed"],
        "analyzed": counts["analyzed"],
        "frames_sampled": counts["frames"],
        "method": "multiframe",
        "phase": "analyzing",
    })


def _cleanup(staging: Path) -> None:
    """Remove this job's sampled frames only."""
    if not staging.exists():
        return
    for child in sorted(staging.rglob("*"), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                safe_unlink(child)
        except OSError:
            logger.warning("Could not remove %s", child, exc_info=True)
    try:
        staging.rmdir()
    except OSError:
        logger.warning("Motion staging not empty: %s", staging, exc_info=True)


def register_motion_tasks() -> None:
    register_task("motion_review")(motion_review_task)
