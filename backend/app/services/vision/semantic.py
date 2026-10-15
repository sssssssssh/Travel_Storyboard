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
from ...ffmpeg import safe_image_open, safe_mkdir, safe_open, safe_stat, safe_unlink
from ...models import Job, Asset, Frame, Segment, SegmentSemanticResult, SegmentSemanticAttempt, utcnow
from ...paths import project_cache_dir, resolve_in_project
from ..staging_cleanup import create_owned_staging, cleanup_staging_or_log, StagingNotOwned
from ..runner import JobCancelled, JobFailures, cancel_check_for, register_task, update_job, merge_job_stats
from . import schema as schema_mod
from .authorization import authorize_upload
from .verdicts import digest, source_identity
from .publication import validate_publication
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
    project_id: str = ""
    source_identity: str = ""
    actual_manifest: list = field(default_factory=list)
    calls: int = 0
    images_sent: int = 0
    usage: dict = field(default_factory=dict)
    request_audit: dict = field(default_factory=dict)


@dataclass
class RenderedImage:
    path: str
    label: str
    frame_id: str
    data: bytes = b""


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
                project_id=project_id,
                source_identity=source_identity(asset),
            )

        if by_segment:
            frame_rows = s.execute(
                select(Frame).where(Frame.segment_id.in_(list(by_segment))).order_by(Frame.time_sec, Frame.id)
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


def coverage_report(project_id: str, considered: list[Candidate], images_sent: int, *, analyzed: int | None = None, cached: int = 0) -> dict:
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
        "planned": len(considered_ids),
        "analyzed": analyzed or 0,
        "cached_valid": cached,
        "not_covered": max(0, eligible - (len(considered_ids) if analyzed is None else analyzed + cached)),
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
            with safe_open(dest, "wb") as stream:
                img.save(stream, format="JPEG", quality=85, optimize=True)
        return safe_stat(dest) is not None
    except (OSError, ValueError) as exc:
        logger.warning("Could not render privacy image (%s)", type(exc).__name__)
        return False


def stage_image_dir(project_id: str, job_id: str) -> Path:
    """Staging area for one job's privacy-rendered images (never the frames dir)."""
    return project_cache_dir(project_id) / "vision" / "staging" / job_id


def image_manifest_hash(entries: list) -> str:
    """Ordered frame IDs/labels and SHA-256 of actual immutable JPEG payloads."""
    return digest(entries)


def source_signature(candidates: list[Candidate]) -> str:
    """Versioned per-shot identity, independent of the selection batch."""
    settings = get_settings()
    return digest(["semantic-input-v2", [
        [c.project_id, c.asset_id, c.segment_id, c.source_identity,
         c.start_sec, c.end_sec, c.story_role, c.actual_manifest]
        for c in candidates], settings.model_name, settings.vision_prompt_version,
        settings.vision_schema_version, schema_mod.SCHEMA_VERSION,
        settings.model_max_images, settings.vision_target_width, "jpeg85-rgb-lanczos-v1"])


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
    job_id: str,
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
        validate_publication(s, job_id, project_id, _snapshot(candidate), "vision_analyze")
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
            existing = SegmentSemanticResult(project_id=project_id,
                                             segment_id=candidate.segment_id, **payload)
            s.add(existing)
        s.flush()
        s.add(SegmentSemanticAttempt(
            project_id=project_id, job_id=job_id, segment_id=candidate.segment_id,
            result_id=existing.id, source_signature=src_sig, frame_manifest_hash=manifest_hash,
            analysis_json=validated.visual,
            evidence_json={**candidate.request_audit, "model_id": model_id,
                           "prompt_version": prompt_version, "schema_version": schema_mod.SCHEMA_VERSION,
                           "manifest": candidate.actual_manifest},
            request_usage=usage or {},
        ))


def _already_done(
    segment_id: str, src_sig: str, manifest_hash: str, model_id: str, prompt_version: str,
    *, job_id: str, candidate: Candidate
) -> bool:
    """Idempotency key from the task book: skip a successful identical run."""
    with session_scope() as s:
        begin_write(s)
        validate_publication(s, job_id, candidate.project_id, _snapshot(candidate), "vision_analyze")
        existing = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.segment_id == segment_id,
                SegmentSemanticResult.source_signature == src_sig,
                SegmentSemanticResult.frame_manifest_hash == manifest_hash,
                SegmentSemanticResult.model_id == model_id,
                SegmentSemanticResult.prompt_version == prompt_version,
                SegmentSemanticResult.status == "succeeded",
                SegmentSemanticResult.is_current.is_(True),
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
    from . import get_provider

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

    # TSB-312: the single shared authorization boundary. It is evaluated before
    # any frame is selected, rendered or encoded, so a denied run leaves no temp
    # files behind as well as making zero outbound calls.
    try:
        context = authorize_upload(
            project_id=project_id,
            upload_acknowledged=acknowledged,
            purpose="vision_analyze",
        )
    except ModelError as exc:
        update_job(job_id, status="failed", error=str(exc))
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

    counts = {"analyzed": 0, "skipped": 0, "failed": 0, "images_sent": 0}
    usage_totals: dict[str, int] = {}
    considered: list[Candidate] = []
    total = len(candidates)

    update_job(job_id, status="running",
               stats={"total": total, "done": 0, "failed": 0, "phase": "analyzing"})

    try:
        create_owned_staging(project_id, staging, owner_job_id=job_id)
        for index, candidate in enumerate(candidates, start=1):
            if cancel_check():
                raise JobCancelled("vision_analyze cancelled")

            considered.append(candidate)

            try:
                rendered = _render_images(project_id, candidate, staging)
                candidate.actual_manifest = [[r.frame_id, r.label, hashlib.sha256(r.data).hexdigest()]
                                             for r in rendered]
                src_sig = source_signature([candidate])
                manifest_hash = image_manifest_hash(candidate.actual_manifest)
                if not candidate.source_identity:
                    raise ModelError(SOURCE_CHANGED, "source is unavailable or changed during selection")
                if not rendered:
                    raise ModelError(INSUFFICIENT_FRAMES, "no readable representative frame")
                if not force and _already_done(
                    candidate.segment_id, src_sig, manifest_hash,
                    settings.model_name, settings.vision_prompt_version,
                    job_id=job_id, candidate=candidate,
                ):
                    counts["skipped"] += 1
                    _progress(job_id, index, total, counts)
                    continue
                budgets.spend()
                outcome, usage, sent = _analyze_one(
                    project_id, candidate, staging, provider, cancel_check,
                    authorization=context, rendered=rendered,
                )
            except ModelError as exc:
                if exc.code == CANCELLED:
                    failures.record(kind="vision", ref=candidate.segment_id,
                                    message="[CANCELLED] provider cancelled", error_code=CANCELLED)
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
                    src_sig=src_sig, manifest_hash=manifest_hash, usage=usage, job_id=job_id,
                )
            except ModelError as exc:
                counts["failed"] += 1
                failures.record(kind="vision", ref=candidate.segment_id,
                                message=f"[{exc.code}] {exc.message}", error_code=exc.code)
                _progress(job_id, index, total, counts)
                continue
            except JobCancelled:
                failures.record(kind="vision", ref=candidate.segment_id,
                                message="[CANCELLED] cancelled before publication", error_code=CANCELLED)
                raise
            except IntegrityError as exc:
                # The version key already exists. That is a real outcome the
                # operator must see: reporting it as `skipped` with no failure
                # made a forced re-run look successful while storing nothing.
                counts["failed"] += 1
                failures.record(
                    kind="vision", ref=candidate.segment_id,
                    message="[DB_CONFLICT] result publication conflict",
                    error_code="DB_CONFLICT",
                )
                _progress(job_id, index, total, counts)
                continue

            counts["analyzed"] += 1
            _add_usage(usage_totals, usage)
            _progress(job_id, index, total, counts)

    except (StagingNotOwned, FileExistsError):
        counts["failed"] += 1
        failures.record(kind="cleanup", ref=job_id,
                        message="[STAGING_NOT_OWNED] staging creation refused; unknown residuals retained",
                        error_code="STAGING_NOT_OWNED")
    except OSError:
        counts["failed"] += 1
        failures.record(kind="staging", ref=job_id,
                        message="[STAGING_IO_FAILED] visual staging I/O failed", error_code="STAGING_IO_FAILED")
    finally:
        try:
            merge_job_stats(job_id, {"calls": sum(c.calls for c in considered),
                                    "images_sent": sum(c.images_sent for c in considered),
                                    "usage": _usage_totals(considered)})
        finally:
            cleanup_staging_or_log(project_id, staging, owner_job_id=job_id)

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
        "images_sent": sum(c.images_sent for c in considered),
        "calls": sum(c.calls for c in considered),
        "reused": 0,
        "usage": _usage_totals(considered),
        "coverage": coverage_report(project_id, candidates, sum(c.images_sent for c in considered),
                                    analyzed=counts["analyzed"], cached=counts["skipped"]),
        "phase": "done",
    }
    update_job(job_id, stats=stats)


def _snapshot(candidate):
    return {"segment_id": candidate.segment_id, "asset_id": candidate.asset_id,
            "start_sec": candidate.start_sec, "end_sec": candidate.end_sec,
            "source_signature": candidate.source_identity}


def _read_bytes(path):
    with safe_open(path, "rb") as stream:
        return stream.read()


def _render_images(project_id, candidate, staging):
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
                RenderedImage(path=str(dest), label=f"frame_{idx + 1}", frame_id=frame_id,
                              data=_read_bytes(dest))
            )
        if len(rendered) >= settings.model_max_images:
            break

    return rendered


def _usage_totals(candidates):
    totals = {}
    for candidate in candidates:
        _add_usage(totals, candidate.usage)
    return totals


def _analyze_one(
    project_id: str,
    candidate: Candidate,
    staging: Path,
    provider,
    cancel_check,
    *,
    authorization=None,
    rendered=None,
):
    """Render, call and validate one shot.

    Returns ``((validated, problems), usage, images_sent)`` or
    ``(None, {}, 0)`` when no frame could be rendered.
    """
    settings = get_settings()
    if rendered is None:
        rendered = _render_images(project_id, candidate, staging)

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

    candidate.calls += 1
    candidate.images_sent += len(rendered)
    try:
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
                images=[ImagePart(data=r.data, mime="image/jpeg", label=r.label) for r in rendered],
                system=schema_mod.SYSTEM_PROMPT,
                json_object=True,
                max_tokens=settings.vision_max_tokens,
                authorization=authorization,
            ),
            cancel_check=cancel_check,
        )
    except ModelError as exc:
        if exc.attempts is not None:
            candidate.calls += exc.attempts - 1
        if exc.images_sent is not None:
            candidate.images_sent += exc.images_sent - len(rendered)
        raise

    candidate.calls += max(0, response.attempts - 1)
    candidate.images_sent += len(rendered) * max(0, response.attempts - 1)
    candidate.usage = response.usage
    candidate.request_audit = {"request_id": response.request_id, "attempts": response.attempts,
                               "latency_ms": response.latency_ms}

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


def register_vision_tasks() -> None:
    """Register the M5 job types with the shared runner."""
    register_task("vision_analyze")(vision_analyze_task)
