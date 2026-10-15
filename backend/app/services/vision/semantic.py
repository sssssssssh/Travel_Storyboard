"""TSB-304 — vision semantics: selection, privacy rendering, persistence, job.

Flow (matching the task book's component diagram)::

    Selector & validation -> Privacy image renderer -> Provider adapter
      -> Schema validator -> Semantic repository

Two properties are load-bearing:

* **Coverage is explicit.** The job reports ``eligible / considered / images_sent
  / analyzed / skipped / failed / not_covered``. Analysing 6 of 100 shots must
  never be presented as "the library is understood".
* **Nothing here writes human state.** Results land in
  ``segment_semantic_results``; ``segments.user_rating``/``story_role``/
  ``start_sec``/``end_sec`` are read-only inputs to this module.

Images are re-rendered with Pillow (EXIF/GPS stripped) exactly like the analysis
package does, so the upload never carries camera metadata or a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ...config import get_settings
from ...database import begin_write, session_scope
from ...ffmpeg import safe_image_open, safe_mkdir, safe_stat, safe_unlink
from ...models import Job, Asset, Frame, Segment, SegmentSemanticResult, utcnow
from ...paths import project_cache_dir, resolve_in_project
from ..runner import JobCancelled, JobFailures, cancel_check_for, register_task, update_job
from . import schema as schema_mod
from .errors import (
    CANCELLED,
    INSUFFICIENT_FRAMES,
    MODEL_BUDGET_EXCEEDED,
    MODEL_NOT_CONFIGURED,
    MODEL_UPLOAD_DENIED,
    SOURCE_CHANGED,
    ModelError,
)

logger = logging.getLogger(__name__)

#: Segment statuses that are not eligible for analysis.
INVALID_SEGMENT_STATUSES = ("needs_review", "archived", "invalid")


@dataclass
class Candidate:
    segment_id: str
    asset_id: str
    start_sec: float
    end_sec: float
    frame_ids: list[str] = field(default_factory=list)
    frame_paths: list[str] = field(default_factory=list)
    story_role: str = ""
    user_rating: str = "pending"


@dataclass
class RenderedImage:
    path: str
    label: str
    frame_id: str


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def select_candidates(
    project_id: str,
    *,
    limit: int | None = None,
    segment_ids: list[str] | None = None,
    spread_across_assets: bool = True,
) -> list[Candidate]:
    """Pick eligible shots, preferring breadth across source videos.

    A round-robin over assets keeps one long video from consuming the whole
    budget, which is what "sampling must span several asset_ids" requires.
    ``exclude``d and invalid shots are skipped here but still counted by
    :func:`coverage_report`, so the gap stays visible.
    """
    with session_scope() as s:
        rows = s.execute(
            select(Segment, Asset)
            .join(Asset, Asset.id == Segment.asset_id)
            .where(
                Segment.project_id == project_id,
                Segment.status.not_in(INVALID_SEGMENT_STATUSES),
                Segment.user_rating != "exclude",
            )
            .order_by(Asset.display_name, Segment.start_sec)
        ).all()

        by_segment: dict[str, Candidate] = {}
        for segment, asset in rows:
            by_segment[segment.id] = Candidate(
                segment_id=segment.id,
                asset_id=asset.id,
                start_sec=float(segment.start_sec),
                end_sec=float(segment.end_sec),
                story_role=segment.story_role or "",
                user_rating=segment.user_rating or "pending",
            )

        if by_segment:
            frame_rows = s.execute(
                select(Frame).where(Frame.segment_id.in_(list(by_segment)))
            ).scalars().all()
            for frame in frame_rows:
                cand = by_segment.get(frame.segment_id)
                if cand is None:
                    continue
                cand.frame_ids.append(frame.id)
                cand.frame_paths.append(frame.image_path)

    candidates = list(by_segment.values())

    if segment_ids:
        wanted = set(segment_ids)
        candidates = [c for c in candidates if c.segment_id in wanted]

    # Only shots with at least one stored frame can be sent.
    candidates = [c for c in candidates if c.frame_ids]

    if spread_across_assets:
        buckets: dict[str, list[Candidate]] = {}
        for cand in candidates:
            buckets.setdefault(cand.asset_id, []).append(cand)
        ordered: list[Candidate] = []
        while buckets:
            for asset_id in list(buckets):
                bucket = buckets[asset_id]
                ordered.append(bucket.pop(0))
                if not bucket:
                    del buckets[asset_id]
        candidates = ordered

    if limit is not None:
        candidates = candidates[: max(0, limit)]
    return candidates


def coverage_report(project_id: str, considered: list[Candidate], images_sent: int) -> dict:
    """Honest accounting of how much of the library was actually looked at."""
    with session_scope() as s:
        all_segments = s.execute(
            select(Segment.id, Segment.user_rating, Segment.status).where(
                Segment.project_id == project_id
            )
        ).all()

    total = len(all_segments)
    eligible = excluded = invalid = 0
    for _sid, rating, status in all_segments:
        if status in INVALID_SEGMENT_STATUSES:
            invalid += 1
        elif (rating or "pending") == "exclude":
            excluded += 1
        else:
            eligible += 1

    considered_ids = {c.segment_id for c in considered}
    return {
        "library_segments": total,
        "eligible": eligible,
        "excluded": excluded,
        "invalid": invalid,
        "considered": len(considered),
        "not_covered": max(0, eligible - len(considered_ids)),
        "images_sent": images_sent,
    }


# --------------------------------------------------------------------------
# privacy rendering
# --------------------------------------------------------------------------

def render_privacy_image(src: str, dest: Path, *, target_width: int) -> bool:
    """Re-encode one representative frame into a metadata-free JPEG.

    Decoding and re-encoding through Pillow is what makes the EXIF/GPS removal
    real rather than claimed (C5): the bytes the model receives are produced by
    this process and never carried camera metadata.
    """
    try:
        with safe_image_open(src) as img:
            img = img.convert("RGB")
            if target_width and img.width > target_width:
                ratio = target_width / float(img.width)
                img = img.resize(
                    (target_width, max(1, int(img.height * ratio))), Image.LANCZOS
                )
            safe_mkdir(dest.parent)
            img.save(dest, format="JPEG", quality=85, optimize=True)
        return safe_stat(dest) is not None
    except (OSError, ValueError) as exc:
        logger.warning("Could not render privacy image from %s: %s", src, exc)
        return False


def stage_image_dir(project_id: str, job_id: str) -> Path:
    """Staging area for one job's privacy-rendered images (never the frames dir)."""
    return project_cache_dir(project_id) / "vision" / "staging" / job_id


def image_manifest_hash(entries: list[tuple[str, int]]) -> str:
    """Stable hash of (shot identity, frame count) for the exact images sent."""
    payload = json.dumps(sorted(entries), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:64]


def source_signature(candidates: list[Candidate]) -> str:
    """Signature of the *inputs*: shot identity plus its current bounds and frames.

    Re-trimming a shot or re-detecting it changes this, which makes a stored
    result detectably stale instead of silently wrong.
    """
    payload = json.dumps(
        sorted(
            (c.segment_id, round(c.start_sec, 3), round(c.end_sec, 3), sorted(c.frame_ids))
            for c in candidates
        ),
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:64]


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def _store_result(
    project_id: str,
    candidate: Candidate,
    validated: schema_mod.ValidatedSemantics,
    *,
    provider_name: str,
    model_id: str,
    prompt_version: str,
    src_sig: str,
    manifest_hash: str,
    usage: dict,
) -> None:
    """Upsert one validated result and demote the previous current row.

    Two cases, both in a single write transaction so a reader never observes two
    ``is_current`` rows for one shot:

    * a row with the **same version key** is replaced in place — this is a
      forced re-analysis of identical inputs, and the unique constraint means
      inserting a second copy is not an option;
    * a row with a **different** key is added, and the previous current row is
      demoted so the history stays auditable.
    """
    with session_scope() as s:
        begin_write(s)
        existing = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.segment_id == candidate.segment_id,
                SegmentSemanticResult.source_signature == src_sig,
                SegmentSemanticResult.frame_manifest_hash == manifest_hash,
                SegmentSemanticResult.model_id == model_id,
                SegmentSemanticResult.prompt_version == prompt_version,
            )
        ).first()

        s.execute(
            update(SegmentSemanticResult)
            .where(
                SegmentSemanticResult.segment_id == candidate.segment_id,
                SegmentSemanticResult.is_current.is_(True),
            )
            .values(is_current=False)
        )

        payload = {
            "asset_id": candidate.asset_id,
            "status": "succeeded",
            "model_provider": provider_name,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "schema_version": schema_mod.SCHEMA_VERSION,
            "source_signature": src_sig,
            "frame_manifest_hash": manifest_hash,
            "analysis_json": validated.visual,
            "evidence_json": {
                **validated.evidence,
                "review": validated.review,
                "warnings": validated.warnings,
            },
            "request_usage": usage or {},
            "is_current": True,
            "error_code": "",
            "error_message": "",
            "updated_at": utcnow(),
        }

        if existing is not None:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            s.add(
                SegmentSemanticResult(
                    project_id=project_id,
                    segment_id=candidate.segment_id,
                    **payload,
                )
            )


def _already_done(
    segment_id: str, src_sig: str, manifest_hash: str, model_id: str, prompt_version: str
) -> bool:
    """Idempotency key from the task book: skip a successful identical run."""
    with session_scope() as s:
        existing = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.segment_id == segment_id,
                SegmentSemanticResult.source_signature == src_sig,
                SegmentSemanticResult.frame_manifest_hash == manifest_hash,
                SegmentSemanticResult.model_id == model_id,
                SegmentSemanticResult.prompt_version == prompt_version,
                SegmentSemanticResult.status == "succeeded",
            )
        ).first()
    return existing is not None


# --------------------------------------------------------------------------
# job task
# --------------------------------------------------------------------------

class _Budget:
    """Per-job request budget; the task book requires a hard ceiling."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self.used = 0
        self.exceeded = False

    def spend(self) -> None:
        if self.used >= self.limit:
            self.exceeded = True
            raise ModelError(MODEL_BUDGET_EXCEEDED, f"call budget {self.limit} exhausted")
        self.used += 1


def vision_analyze_task(job_id: str) -> None:
    """Worker entry point for the ``vision_analyze`` job."""
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

    limit = int(config.get("limit") or settings.vision_max_segments_per_job)
    force = bool(config.get("force"))
    segment_ids = list(config.get("segment_ids") or [])
    acknowledged = bool(config.get("upload_acknowledged"))

    # Consent gate: images leave the machine only after an explicit opt-in.
    if not acknowledged or settings.model_mode != "on":
        update_job(
            job_id,
            status="failed",
            error=(
                f"{MODEL_UPLOAD_DENIED}: 未确认上传或视觉模型未启用"
                "（需 upload_acknowledged=true 且 TRAVELSB_MODEL_MODE=on）"
            ),
        )
        return

    if not is_configured():
        update_job(job_id, status="failed", error=f"{MODEL_NOT_CONFIGURED}: 视觉模型未配置")
        return

    candidates = select_candidates(project_id, limit=limit, segment_ids=segment_ids or None)
    if not candidates:
        update_job(
            job_id,
            status="failed",
            error=f"{INSUFFICIENT_FRAMES}: 没有可分析的有效镜头（需已检测且存在代表帧）",
            stats={"total": 0, "done": 0, "failed": 0,
                   "coverage": coverage_report(project_id, [], 0)},
        )
        return

    budgets = _Budget(settings.model_max_calls_per_job)
    provider = get_provider()
    staging = stage_image_dir(project_id, job_id)
    manifest_hash = image_manifest_hash([(c.segment_id, len(c.frame_ids)) for c in candidates])
    src_sig = source_signature(candidates)

    counts = {"analyzed": 0, "skipped": 0, "failed": 0, "images_sent": 0}
    usage_totals: dict[str, int] = {}
    considered: list[Candidate] = []
    total = len(candidates)

    update_job(job_id, status="running",
               stats={"total": total, "done": 0, "failed": 0, "phase": "analyzing"})

    try:
        for index, candidate in enumerate(candidates, start=1):
            if cancel_check():
                raise JobCancelled("vision_analyze cancelled")

            considered.append(candidate)

            if not force and _already_done(
                candidate.segment_id, src_sig, manifest_hash,
                settings.model_name, settings.vision_prompt_version,
            ):
                counts["skipped"] += 1
                _progress(job_id, index, total, counts)
                continue

            try:
                budgets.spend()
                outcome, usage, sent = _analyze_one(
                    project_id, candidate, staging, provider, cancel_check,
                )
            except ModelError as exc:
                if exc.code == CANCELLED:
                    raise JobCancelled("vision_analyze cancelled") from exc
                counts["failed"] += 1
                failures.record(kind="vision", ref=candidate.segment_id,
                                message=f"[{exc.code}] {exc.message}",
                                error_code=exc.code)
                _progress(job_id, index, total, counts)
                continue

            if outcome is None:
                counts["failed"] += 1
                failures.record(kind="vision", ref=candidate.segment_id,
                                message=f"[{INSUFFICIENT_FRAMES}] 无可读代表帧",
                                error_code=INSUFFICIENT_FRAMES)
                _progress(job_id, index, total, counts)
                continue

            validated, problems = outcome
            if not validated:
                counts["failed"] += 1
                detail = problems[0]["message"] if problems else "no usable result"
                failures.record(kind="vision", ref=candidate.segment_id,
                                message=f"[{schema_mod.MODEL_INVALID_OUTPUT}] {detail}",
                                error_code=schema_mod.MODEL_INVALID_OUTPUT)
                _progress(job_id, index, total, counts)
                continue

            try:
                _store_result(
                    project_id, candidate, validated[0],
                    provider_name=provider.name, model_id=settings.model_name,
                    prompt_version=settings.vision_prompt_version,
                    src_sig=src_sig, manifest_hash=manifest_hash, usage=usage,
                )
            except IntegrityError as exc:
                # The version key already exists. That is a real outcome the
                # operator must see: reporting it as `skipped` with no failure
                # made a forced re-run look successful while storing nothing.
                counts["failed"] += 1
                failures.record(
                    kind="vision", ref=candidate.segment_id,
                    message=f"[{schema_mod.MODEL_INVALID_OUTPUT}] 结果写入冲突：{exc.orig}",
                    error_code=schema_mod.MODEL_INVALID_OUTPUT,
                )
                _progress(job_id, index, total, counts)
                continue

            counts["analyzed"] += 1
            counts["images_sent"] += sent
            _add_usage(usage_totals, usage)
            _progress(job_id, index, total, counts)

    finally:
        _cleanup_staging(staging)

    if budgets.exceeded:
        failures.record(kind="budget", ref=job_id,
                        message=f"[{MODEL_BUDGET_EXCEEDED}] 达到每次任务调用上限 {budgets.limit}",
                        error_code=MODEL_BUDGET_EXCEEDED)

    stats = {
        "total": total,
        "done": counts["analyzed"] + counts["skipped"],
        "failed": counts["failed"],
        "analyzed": counts["analyzed"],
        "skipped": counts["skipped"],
        "images_sent": counts["images_sent"],
        "calls": budgets.used,
        "usage": usage_totals,
        "coverage": coverage_report(project_id, considered, counts["images_sent"]),
        "phase": "done",
    }
    if len(failures):
        update_job(job_id, status="partial_failed", stats=stats,
                   error=f"{len(failures)} 个镜头或预算项失败")
    else:
        update_job(job_id, status="succeeded", stats=stats)


def _analyze_one(
    project_id: str,
    candidate: Candidate,
    staging: Path,
    provider,
    cancel_check,
):
    """Render, call and validate one shot.

    Returns ``((validated, problems), usage, images_sent)`` or
    ``(None, {}, 0)`` when no frame could be rendered.
    """
    settings = get_settings()
    rendered: list[RenderedImage] = []

    for idx, (frame_id, rel_path) in enumerate(
        zip(candidate.frame_ids, candidate.frame_paths)
    ):
        try:
            src = resolve_in_project(project_id, rel_path)
        except (ValueError, OSError):
            continue
        dest = staging / f"{candidate.segment_id[:8]}-{idx}.jpg"
        if render_privacy_image(str(src), dest, target_width=settings.vision_target_width):
            rendered.append(
                RenderedImage(path=str(dest), label=f"frame_{idx + 1}", frame_id=frame_id)
            )
        if len(rendered) >= settings.model_max_images:
            break

    if not rendered:
        return None, {}, 0

    frame_ids = [r.frame_id for r in rendered]
    allowed = {
        candidate.segment_id: schema_mod.AllowedSegment(
            segment_id=candidate.segment_id,
            asset_id=candidate.asset_id,
            start_sec=candidate.start_sec,
            end_sec=candidate.end_sec,
            frame_ids=frame_ids,
        )
    }
    manifest = [
        {
            "segment_id": candidate.segment_id,
            "asset_id": candidate.asset_id,
            "start_sec": candidate.start_sec,
            "end_sec": candidate.end_sec,
            "frame_ids": frame_ids,
            "story_role": candidate.story_role,
        }
    ]

    from .providers.base import ImagePart, VisionRequest

    response = provider.complete(
        VisionRequest(
            prompt=schema_mod.build_prompt(
                manifest,
                coverage={
                    "images_sent": len(rendered),
                    "segments_considered": 1,
                    "eligible": 1,
                },
            ),
            images=[ImagePart(path=r.path, label=r.label) for r in rendered],
            system=schema_mod.SYSTEM_PROMPT,
            json_object=True,
            max_tokens=1500,
        ),
        cancel_check=cancel_check,
    )

    # Server-side re-check: the shot may have been re-detected while the model was
    # thinking. A result describing stale bounds must not be stored (C2/C3).
    if not _still_valid(candidate):
        raise ModelError(SOURCE_CHANGED, "segment changed while the model was running")

    # A response with no parseable object at all is a per-item failure, not a
    # crash: re-raising here would abandon the whole batch and lose the fact
    # that this shot was never read (C6, and the A13 "malformed JSON" row).
    try:
        validated, problems = schema_mod.validate_response(response.text, allowed)
    except schema_mod.SemanticValidationError as exc:
        return ([], [{"index": 0, "code": exc.code, "message": exc.message}]), response.usage, len(rendered)
    return (validated, problems), response.usage, len(rendered)


def _still_valid(candidate: Candidate) -> bool:
    with session_scope() as s:
        segment = s.get(Segment, candidate.segment_id)
        if segment is None:
            return False
        if segment.status in INVALID_SEGMENT_STATUSES:
            return False
        if (segment.user_rating or "pending") == "exclude":
            return False
        return (
            abs(float(segment.start_sec) - candidate.start_sec) < 0.001
            and abs(float(segment.end_sec) - candidate.end_sec) < 0.001
        )


def _add_usage(totals: dict, usage: dict) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            totals[key] = int(totals.get(key, 0) + value)


def _progress(job_id: str, index: int, total: int, counts: dict) -> None:
    update_job(job_id, stats={
        "total": total,
        "done": counts["analyzed"] + counts["skipped"],
        "failed": counts["failed"],
        "analyzed": counts["analyzed"],
        "skipped": counts["skipped"],
        "images_sent": counts["images_sent"],
        "phase": "analyzing",
    })


def _cleanup_staging(staging: Path) -> None:
    """Remove this job's rendered images. Only our own job-scoped directory."""
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
        logger.warning("Vision staging not empty: %s", staging, exc_info=True)


def register_vision_tasks() -> None:
    """Register the M5 job types with the shared runner."""
    register_task("vision_analyze")(vision_analyze_task)
