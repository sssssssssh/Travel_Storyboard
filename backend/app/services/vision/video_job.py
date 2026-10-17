"""TSB-329 — the ``video_analyze`` job: budget, capability gate, fallback, cancel.

This is the orchestration layer. It is where the batch's safety properties either
hold or quietly leak, so the order of operations is deliberate:

1. **Authorise before anything is rendered or derived.** The shared boundary
   (TSB-312) decides whether this call may send frames off-box at all. A denied
   run must leave zero bytes and make zero requests, so the check precedes clip
   derivation — not merely the upload.
2. **Gate on *verified* capability, not on a config flag.** ``model_supports_native_video``
   is an operator intent; sending on it alone is the mistake the task book warns
   about. Without a sendable capability record the job fails with
   ``VIDEO_CAPABILITY_UNVERIFIED``/``UNSUPPORTED`` and the video button stays
   disabled.
3. **Budget before spending.** Segment count, total seconds and total bytes are
   estimated from the plan and refused up front (``MODEL_BUDGET_EXCEEDED``), so a
   misconfiguration cannot quietly upload an entire library.
4. **Per-shot failure is visible, never a silent skip.** Each shot records its own
   outcome; one bad clip does not abort the batch and does not get counted as
   success (C6).
5. **Cancellation stops real work.** ``cancel_check`` reaches FFmpeg and the
   provider, and the task re-checks before publishing, so a cancelled job cannot
   commit a late answer. Cleanup residue is reported rather than assumed gone.

Fallback is explicit and recorded: if the user asks for ``video_native`` and the
capability is unavailable, the job may fall back to ``multiframe`` **only** when
the request opted into it, and the effective mode plus ``fallback_reason`` are
written into the job stats and every stored row. Silently treating an image
reading as a video reading is forbidden.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ...config import get_settings
from ...database import begin_write, session_scope
from ...models import Asset, Job, Segment
from ..runner import (
    JobCancelled,
    JobFailures,
    cancel_check_for,
    merge_job_stats,
    register_task,
    update_job,
)
from ..staging_cleanup import cleanup_staging_or_log, is_registered_leaf
from . import video_analysis as va
from .authorization import authorization_denial_reason, authorize_upload
from .errors import (
    CLIP_FAILED,
    MODEL_BUDGET_EXCEEDED,
    MODEL_NOT_CONFIGURED,
    MODEL_PROVIDER_FAULT,
    MODEL_UPLOAD_DENIED,
    ModelError,
)
from .verdicts import source_identity
from .video_capabilities import (
    UNSUPPORTED,
    UNVERIFIED,
    VideoCapabilities,
    unverified_capabilities,
)
from .video_clip import (
    DEFAULT_MAX_CLIP_SEC,
    ClipSpec,
    VideoClipError,
    derive_clip,
)
from .video_schema import (
    SYSTEM_PROMPT,
    VIDEO_PROMPT_VERSION,
    VideoSchemaError,
    build_prompt,
    extract_json_object,
    validate_temporal_events,
)

logger = logging.getLogger(__name__)

#: Modes a caller may request. ``video_native`` is the new channel; the other two
#: are the existing, unchanged paths.
VALID_MODES = ("image_only", "multiframe", "video_native")

#: The mode actually used when falling back from video. Never derived from the
#: model name or from what happens to be configured.
FALLBACK_MODE = "multiframe"

#: Which job type actually performs the work when the video route is unavailable.
#: TSB-337: recording this turns "nothing happened" into an actionable, auditable
#: outcome instead of an empty success.
DEFERRED_JOB_TYPES = {
    "multiframe": "motion_review",
    "image_only": "vision_analyze",
}

#: Job-level budget defaults. These are local guard rails, NOT endpoint limits --
#: the capability record reports endpoint limits separately, and keeps them
#: ``unknown`` rather than filling in a plausible number.
DEFAULT_MAX_SEGMENTS = 20
DEFAULT_MAX_TOTAL_SEC = 300.0
DEFAULT_MAX_TOTAL_BYTES = 120_000_000
DEFAULT_CALL_TIMEOUT_SEC = 180.0


@dataclass
class VideoBudget:
    """Caps for one job, all configurable and all enforced before sending."""

    max_segments: int = DEFAULT_MAX_SEGMENTS
    max_total_sec: float = DEFAULT_MAX_TOTAL_SEC
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_calls: int = 200
    timeout_sec: float = DEFAULT_CALL_TIMEOUT_SEC
    max_retries: int = 2

    # --- TSB-338: the running ledger -------------------------------------
    #: Totals actually spent so far. Plan-level ``check()`` is a pre-flight
    #: estimate; these counters are the *observed* spend, and every HTTP attempt
    #: must pass ``note_attempt()`` first. Before this, ``max_calls`` was read
    #: from config and never used, so retries and repeated segments could exceed
    #: the cap with nothing noticing -- and there was no way to audit afterwards
    #: how much a job really sent.
    used_calls: int = 0
    used_bytes: int = 0
    derived_seconds: float = 0.0

    def note_attempt(self, *, calls: int = 1, bytes_sent: int) -> dict:
        """Reserve one HTTP attempt, or refuse *before* it is made.

        Called immediately before each send -- including every retry, and for the
        Base64/JSON-inflated size rather than the clip size. Raising here means
        the bytes never leave the machine, which is the point: a budget enforced
        after the request is not a budget.
        """
        if self.used_calls + calls > self.max_calls:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"this job has already made {self.used_calls} call(s); one more "
                f"would exceed the limit of {self.max_calls}",
            )
        if self.used_bytes + bytes_sent > self.max_total_bytes:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"this job has already sent {self.used_bytes} bytes; {bytes_sent} "
                f"more would exceed the limit of {self.max_total_bytes}",
            )
        self.used_calls += calls
        self.used_bytes += bytes_sent
        return self.ledger()

    def note_derived(self, derived: list[tuple[float, int]]) -> None:
        """Re-check the durations the encoder *actually produced*.

        The plan holds requested lengths; ``derive_clip`` may return something
        longer (keyframe alignment, rounding). Validating only the request would
        let the real spend drift past ``max_total_sec``.
        """
        seconds = round(sum(sec for sec, _ in derived), 3)
        if seconds > self.max_total_sec:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"derived clips total {seconds:.1f}s, over the limit of "
                f"{self.max_total_sec:.1f}s",
            )
        self.derived_seconds = seconds

    def ledger(self) -> dict:
        """A plain, committable summary of what was actually spent.

        Deliberately no clip content, no paths, no credentials: this goes into
        job stats, which are surfaced to the user.
        """
        return {
            "calls": self.used_calls,
            "bytes": self.used_bytes,
            "derived_seconds": self.derived_seconds,
            "remaining_calls": max(0, self.max_calls - self.used_calls),
            "remaining_bytes": max(0, self.max_total_bytes - self.used_bytes),
            "limits": {
                "max_calls": self.max_calls,
                "max_total_bytes": self.max_total_bytes,
                "max_total_sec": self.max_total_sec,
                "max_segments": self.max_segments,
            },
        }

    def estimate(self, planned: list[tuple[float, int]]) -> dict:
        """Summarise a plan of ``(clip_seconds, clip_bytes)`` pairs."""
        return {
            "segments": len(planned),
            "total_sec": round(sum(sec for sec, _ in planned), 3),
            "total_bytes": sum(size for _, size in planned),
        }

    def check(self, planned: list[tuple[float, int]]) -> dict:
        """Raise when the plan exceeds a cap, naming which one and by how much."""
        totals = self.estimate(planned)
        if totals["segments"] > self.max_segments:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"plan covers {totals['segments']} segments, over the limit of "
                f"{self.max_segments}",
            )
        if totals["total_sec"] > self.max_total_sec:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"plan would send {totals['total_sec']:.1f}s of video, over the limit "
                f"of {self.max_total_sec:.1f}s",
            )
        if totals["total_bytes"] > self.max_total_bytes:
            raise ModelError(
                MODEL_BUDGET_EXCEEDED,
                f"plan would send {totals['total_bytes']} bytes, over the limit of "
                f"{self.max_total_bytes}",
            )
        return totals


@dataclass
class VideoJobPlan:
    """Resolved configuration for one run."""

    requested_mode: str
    effective_mode: str
    fallback_reason: str
    limit: int
    segment_ids: list[str]
    force: bool
    upload_acknowledged: bool
    max_clip_sec: float
    capabilities: VideoCapabilities
    budget: VideoBudget = field(default_factory=VideoBudget)


def resolve_mode(
    *,
    requested_mode: str,
    capabilities: VideoCapabilities,
    allow_fallback: bool,
) -> tuple[str, str]:
    """Decide the mode to actually run, and why.

    Returns ``(effective_mode, fallback_reason)``. The important case is the last
    one: video was asked for but is not available. We either refuse (the caller did
    not accept a fallback) or fall back *explicitly*, and either way the reason is
    recorded — never a silent substitution (task book V18).
    """
    if requested_mode not in VALID_MODES:
        raise ModelError(
            MODEL_UPLOAD_DENIED, f"unknown mode {requested_mode!r}; expected one of {VALID_MODES}"
        )
    if requested_mode != "video_native":
        return requested_mode, ""

    if capabilities.sendable:
        return "video_native", ""

    state = capabilities.state or UNVERIFIED
    reason = {
        UNVERIFIED: "VIDEO_CAPABILITY_UNVERIFIED",
        UNSUPPORTED: "VIDEO_CAPABILITY_UNSUPPORTED",
    }.get(state, f"VIDEO_CAPABILITY_{state.upper()}")

    if allow_fallback:
        return FALLBACK_MODE, reason

    raise ModelError(
        MODEL_NOT_CONFIGURED if state == UNVERIFIED else "MODEL_CAPABILITY_UNSUPPORTED",
        f"{reason}: this endpoint has not been verified to accept video, and the "
        "request did not accept a fallback to multi-frame analysis",
    )


def plan_video_job(job_id: str, *, capabilities: VideoCapabilities | None = None) -> VideoJobPlan:
    """Read the job's config snapshot and resolve the plan.

    Every knob comes from the snapshot taken at request time, so a later settings
    change cannot retroactively alter what a running job was authorised to do.
    """
    settings = get_settings()
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise ModelError(MODEL_UPLOAD_DENIED, f"unknown job {job_id}")
        config = dict(job.config_snapshot or {})

    requested = str(config.get("mode") or "video_native")
    # TSB-335: this defaulted to a hardcoded ``unverified_capabilities()``, so even
    # a probe that had established real video support could never reach execution
    # -- the channel was closed twice over. Resolve through the store, and re-check
    # it here (execute time) as well as at enqueue time, so switching the endpoint
    # or model after a job was queued cannot ride on the older positive.
    caps = capabilities if capabilities is not None else current_capabilities()

    effective, fallback_reason = resolve_mode(
        requested_mode=requested,
        capabilities=caps,
        allow_fallback=bool(config.get("allow_fallback")),
    )

    budget = VideoBudget(
        max_segments=int(config.get("max_segments") or DEFAULT_MAX_SEGMENTS),
        max_total_sec=float(config.get("max_total_sec") or DEFAULT_MAX_TOTAL_SEC),
        max_total_bytes=int(config.get("max_total_bytes") or DEFAULT_MAX_TOTAL_BYTES),
        max_calls=int(config.get("max_calls") or settings.model_max_calls_per_job),
        timeout_sec=float(config.get("timeout_sec") or DEFAULT_CALL_TIMEOUT_SEC),
        max_retries=int(config.get("max_retries", settings.model_max_retries)),
    )

    return VideoJobPlan(
        requested_mode=requested,
        effective_mode=effective,
        fallback_reason=fallback_reason,
        limit=int(config.get("limit") or settings.vision_max_segments_per_job),
        segment_ids=list(config.get("segment_ids") or []),
        force=bool(config.get("force")),
        upload_acknowledged=bool(config.get("upload_acknowledged")),
        max_clip_sec=float(config.get("max_clip_sec") or DEFAULT_MAX_CLIP_SEC),
        capabilities=caps,
        budget=budget,
    )


def select_candidates(
    project_id: str,
    *,
    limit: int,
    segment_ids: list[str] | None = None,
) -> list[va.VideoCandidate]:
    """Shots eligible for video analysis, in a stable order.

    Excluded/invalid shots are filtered out here rather than skipped later, so the
    budget estimate describes what will actually be sent.
    """
    from .semantic import INVALID_SEGMENT_STATUSES

    with session_scope() as s:
        rows = s.execute(
            select(Segment, Asset)
            .join(Asset, Asset.id == Segment.asset_id)
            .where(Segment.project_id == project_id)
            .order_by(Segment.asset_id, Segment.start_sec)
        ).all()

    candidates: list[va.VideoCandidate] = []
    wanted = set(segment_ids or [])
    for segment, asset in rows:
        if segment_ids and segment.id not in wanted:
            continue
        if segment.status in INVALID_SEGMENT_STATUSES:
            continue
        if segment.user_rating == "exclude":
            continue
        if asset.scan_status in ("failed", "missing", "invalid"):
            continue
        if segment.end_sec <= segment.start_sec:
            continue
        candidates.append(
            va.VideoCandidate(
                segment_id=segment.id,
                asset_id=segment.asset_id,
                project_id=project_id,
                source_path=asset.source_path,
                start_sec=segment.start_sec,
                end_sec=segment.end_sec,
                source_identity=source_identity(asset),
                user_rating=segment.user_rating or "pending",
            )
        )
    return candidates[: max(0, limit)]


def preflight(
    *,
    project_id: str,
    upload_acknowledged: bool,
    capabilities: VideoCapabilities,
) -> None:
    """Everything that must be true before a single frame is derived.

    Ordered cheapest-first so a denied or misconfigured run never touches the
    filesystem: consent, then capability, then configuration.
    """
    reason = authorization_denial_reason(
        upload_acknowledged=upload_acknowledged, project_id=project_id
    )
    if reason is not None:
        code = reason.split(":", 1)[0]
        raise ModelError(code, reason)
    if not capabilities.sendable:
        raise ModelError(
            "MODEL_CAPABILITY_UNSUPPORTED",
            f"VIDEO_CAPABILITY_{capabilities.state.upper()}: the video channel is "
            "not allowed to send",
        )


def _record_failure(
    failures: JobFailures,
    *,
    code: str,
    message: str,
    ref: str = "",
) -> None:
    """Record one per-item failure using the runner's real signature.

    ``JobFailures.record`` takes ``kind``/``ref``/``error_code``; wrapping it here
    keeps the call sites in this module short and, more importantly, keeps the
    stable error code attached -- the UI and acceptance checks branch on the code
    rather than parsing prose.
    """
    failures.record(
        kind="video_analyze",
        ref=ref or "job",
        message=message,
        error_code=code,
    )


def video_capability_refusal(
    *,
    mode: str,
    allow_fallback: bool,
    capabilities: VideoCapabilities | None = None,
) -> dict | None:
    """The refusal body for an unavailable video channel, or None if it may run.

    Returns structured detail rather than raising, so the API layer can answer the
    request *before* enqueueing a job. Enqueueing and failing later would take the
    project lock, show the user a queued job, and only then reveal that video was
    never possible.
    """
    caps = capabilities if capabilities is not None else current_capabilities()
    try:
        resolve_mode(requested_mode=mode, capabilities=caps, allow_fallback=allow_fallback)
    except ModelError as exc:
        return {
            "error_code": exc.code,
            "message": str(exc),
            "capability_state": caps.state,
            "verified": caps.verified,
            "requested_mode": mode,
            # Actionable guidance: the state is not the caller's fault and the
            # remedy is a server-side serving change (measured 2026-10-16).
            "remedy": (
                "让部署接受视频需要服务端加 --limit-mm-per-prompt video=1 并重启；"
                "也可以改用 mode=multiframe 立即分析。"
            ),
        }
    return None


def current_capabilities() -> VideoCapabilities:
    """The capability record this process knows about.

    TSB-335: this used to return ``unverified`` unconditionally, so a probe that
    had genuinely succeeded could never enable the channel and the route answered
    422 forever. The record is now read from a local, operator-controlled store
    (see ``capability_store``) that is bound to the full endpoint identity.

    It is deliberately *not* read from the committed
    ``CAPABILITY_REPORT.json``: that file is public and forgeable, and granting
    upload permission from it would turn a privacy guard into a rubber stamp.
    Any missing, damaged, mismatched or expired record still answers
    ``unverified`` -- the safe direction. Callers holding a probe report pass it
    in explicitly via ``capabilities``.
    """
    from .capability_store import load_capabilities

    try:
        return load_capabilities()
    except Exception:  # noqa: BLE001 - never let a store fault grant or crash
        # An unexpected fault must degrade to "not allowed", never to "allowed",
        # and must not take down the route that needs to explain the state.
        logger.exception("capability store read failed; treating the channel as unverified")
        settings = get_settings()
        return unverified_capabilities(
            model_id=settings.model_name or "",
            reason="能力记录读取失败；已按未验证处理",
        )


def register_video_tasks() -> None:
    """Register the ``video_analyze`` task type.

    Called explicitly from ``app.main`` (the same wiring the M5 tasks needed), so a
    driver script that only imports the module would not get it -- which is why the
    acceptance tool checks the registry rather than assuming.
    """
    register_task("video_analyze")(video_analyze_task)


def video_analyze_task(job_id: str) -> None:
    """Run a ``video_analyze`` job end to end."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise ModelError(MODEL_UPLOAD_DENIED, f"unknown job {job_id}")
        project_id = job.project_id

    plan = plan_video_job(job_id)
    failures = JobFailures(job_id)
    is_cancelled = cancel_check_for(job_id)

    stats = {
        "mode_requested": plan.requested_mode,
        "mode_effective": plan.effective_mode,
        "fallback_reason": plan.fallback_reason,
        "capability_state": plan.capabilities.state,
        "total": 0, "done": 0, "failed": 0, "skipped": 0, "cached": 0,
        "events": 0, "clip_bytes": 0, "clip_seconds": 0.0,
        "phase": "starting",
    }
    update_job(job_id, stats=dict(stats))

    # Falling back is a *visible* decision, recorded before any work happens.
    if plan.fallback_reason:
        _record_failure(
            failures,
            code=plan.fallback_reason,
            message=(
                f"video_native was requested but is unavailable "
                f"({plan.capabilities.state}); running {plan.effective_mode} instead"
            ),
        )

    # TSB-337: a fallback must be an *accounted* outcome, not an ambiguous
    # "complete". The existing image/multi-frame job types own the frame-based
    # paths, so this task correctly performs no video work -- but it must say so
    # in a machine-readable way, with the job type that will do the work. Before
    # this, the run ended at `phase="complete", total=0` with no code and no
    # pointer, so a caller saw success and concluded the footage was analysed
    # (C6: no silent skips).
    if plan.effective_mode != "video_native":
        stats.update(
            phase="deferred",
            total=0,
            deferred=True,
            fallback_deferred_to=DEFERRED_JOB_TYPES.get(
                plan.effective_mode, plan.effective_mode
            ),
            deferred_reason=plan.fallback_reason or "VIDEO_MODE_NOT_NATIVE",
        )
        update_job(job_id, stats=dict(stats))
        _record_failure(
            failures,
            code=plan.fallback_reason or "VIDEO_MODE_NOT_NATIVE",
            message=(
                f"video_native was not run; {plan.effective_mode} work for this "
                f"project is owned by the "
                f"{stats['fallback_deferred_to']} job type"
            ),
        )
        return

    try:
        preflight(
            project_id=project_id,
            upload_acknowledged=plan.upload_acknowledged,
            capabilities=plan.capabilities,
        )
    except ModelError as exc:
        stats.update(phase="failed", error_code=exc.code)
        update_job(job_id, stats=dict(stats))
        _record_failure(
        failures,
        code=exc.code,
        message=str(exc),
    )
        return

    candidates = select_candidates(
        project_id, limit=plan.limit, segment_ids=plan.segment_ids
    )
    stats["total"] = len(candidates)
    stats["phase"] = "running"
    update_job(job_id, stats=dict(stats))
    if not candidates:
        stats["phase"] = "complete"
        update_job(job_id, stats=dict(stats))
        return

    # Budget is checked against the plan, before any encoding.
    planned = [
        (min(c.end_sec - c.start_sec, plan.max_clip_sec), 0) for c in candidates
    ]
    try:
        plan.budget.check(planned)
    except ModelError as exc:
        stats.update(phase="failed", error_code=exc.code)
        update_job(job_id, stats=dict(stats))
        _record_failure(
        failures,
        code=exc.code,
        message=str(exc),
    )
        return

    from . import get_video_provider  # local import keeps the module import-light

    # TSB-334: this must be the *video* adapter. ``get_provider()`` returns the
    # image adapter, which has no ``complete_video`` -- using it here raised
    # AttributeError and silently produced no observation.
    provider = get_video_provider()
    if not hasattr(provider, "complete_video"):  # fail loudly, not as a dead channel
        raise ModelError(
            MODEL_NOT_CONFIGURED,
            "video provider does not implement complete_video()",
        )
    settings = get_settings()
    usage_totals: dict = {"calls": 0, "clip_bytes": 0}
    staging_roots: list[str] = []

    try:
        for index, candidate in enumerate(candidates):
            if is_cancelled():
                raise JobCancelled("cancelled between clips")

            _analyze_one(
                project_id=project_id,
                job_id=job_id,
                candidate=candidate,
                plan=plan,
                provider=provider,
                settings=settings,
                failures=failures,
                stats=stats,
                usage_totals=usage_totals,
                staging_roots=staging_roots,
                is_cancelled=is_cancelled,
            )
            stats["done"] = stats["done"]  # counters updated inside
            update_job(job_id, stats=dict(stats))
    finally:
        # Cleanup runs on every exit path, and its outcome is reported rather than
        # assumed (TSB-262/318 discipline).
        #
        # TSB-336: ``cleanup_staging_or_log`` already records the canonical audit
        # via ``record_cleanup_result`` (stats.cleanup.items with first_failure,
        # residual_bytes, retryable, ...). Writing a second, differently-shaped
        # ``cleanup={attempted,ok,failed,results}`` over it destroyed that audit, so
        # the UI/retry path lost first-failure history and could not tell a
        # remediated failure from a clean run. Only the video-specific verbosity is
        # added here; the canonical fields are left to their own writer.
        cleanup_results = []
        for stage in staging_roots:
            # A leaf recorded *before* derivation may never have been created (the
            # source was unreadable, the asset vanished, cancellation arrived
            # first). Asking the cleaner to reclaim a path that does not exist
            # would be recorded as a failure with a FileNotFoundError, which
            # misreports "nothing to clean" as "cleanup broke". Skip only paths
            # that genuinely never appeared; anything present on disk is always
            # handed to the cleaner, so a real leftover cannot hide here.
            #
            # TSB-346: ``Path.exists()`` FOLLOWS symlinks, so a dangling link
            # reported False and was skipped as "never created" -- a silent skip
            # (C6) that also left the link unreported and unreclaimed. The check
            # is therefore link-aware: anything that occupies the path is passed
            # to the cleaner, which fails closed on links instead of ignoring it.
            if not path_occupies_disk(stage):
                cleanup_results.append(
                    {
                        "path_kind": "video_staging",
                        "ok": True,
                        "bytes": 0,
                        "error": "",
                        "note": "stage was never created",
                    }
                )
                continue
            result = cleanup_staging_or_log(project_id, stage, owner_job_id=job_id)
            cleanup_results.append(
                {
                    "path_kind": "video_staging",
                    "ok": bool(getattr(result, "ok", False)),
                    "bytes": int(getattr(result, "remaining_bytes", 0) or 0),
                    "error": str(getattr(result, "error", "") or ""),
                }
            )
        merge_job_stats(
            job_id,
            {
                # Deliberately NOT overwriting the canonical ``cleanup`` block.
                "cleanup_detail": {
                    "attempted": len(cleanup_results),
                    "ok": sum(1 for r in cleanup_results if r["ok"]),
                    "failed": sum(1 for r in cleanup_results if not r["ok"]),
                    "results": cleanup_results,
                },
                "usage": usage_totals,
                "phase": "complete",
            },
        )


def path_occupies_disk(stage: str | Path) -> bool:
    """Does anything at all sit at this path, including a dangling link?

    TSB-346: ``Path.exists()``/``os.path.exists()`` follow links, so a symlink to
    a missing target answers False even though the directory entry is real. Using
    them here silently reclassified a leftover link as "never created" and skipped
    it. ``lexists`` semantics (lstat, no follow) is what "is there something to
    clean up?" actually means.
    """
    try:
        os.lstat(stage)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        # Unreadable is not absent: let the cleaner report the real problem.
        return True
    return True


def _request_body_bytes(request) -> int:
    """Size of the JSON body that will be sent, including the Base64 inflation.

    Encoded size is what counts against ``max_total_bytes``: a 3 MB clip is ~4 MB
    of Base64 plus the envelope, so budgeting on the raw clip would understate
    the real spend by roughly a third.
    """
    try:
        import base64
        import json

        video = getattr(request, "video", None)
        raw = bytes(getattr(video, "data", b"") or b"")
        encoded = len(base64.b64encode(raw))
        envelope = len(json.dumps({"prompt": getattr(request, "prompt", "")}))
        return encoded + envelope + 512  # + fixed message/envelope overhead
    except Exception:  # noqa: BLE001 - a size estimate must never break a job
        return 0


def _analyze_one(
    *,
    project_id: str,
    job_id: str,
    candidate: va.VideoCandidate,
    plan: VideoJobPlan,
    provider,
    settings,
    failures: JobFailures,
    stats: dict,
    usage_totals: dict,
    staging_roots: list[str],
    is_cancelled,
) -> None:
    """Derive, send and store one shot. Never raises for a per-shot problem."""
    # TSB-336: derive_clip creates ``staging/<asset>/<segment>`` and can fail
    # afterwards. Registering the leaf only on success left failed/cancelled
    # derivations with an unreferenced directory that no cleanup step could
    # report. The leaf is therefore created and registered *before* deriving, and
    # handed to derive_clip so it does not try to create it a second time.
    #
    # TSB-340: recording only the *path string* was not enough -- the directory
    # still had to be created by derive_clip, so a failure before that point left
    # nothing on disk while the cleaner was told to expect a stage. Creating it
    # here makes ownership real (``create_owned_staging`` refuses an existing
    # directory, so this cannot adopt a stale one) and makes the recorded path
    # match an actual registered leaf.
    from ...paths import project_cache_dir
    from ..staging_cleanup import create_owned_staging

    planned_stage = (
        Path(project_cache_dir(project_id)) / "staging" / candidate.asset_id
        / candidate.segment_id
    )
    try:
        planned_stage = Path(
            create_owned_staging(
                project_id,
                f"staging/{candidate.asset_id}/{candidate.segment_id}",
                owner_job_id=job_id,
            )
        )
    except FileExistsError:
        # TSB-345: a bare ``pass`` here was unsafe. The path is deterministic, so
        # *any* directory already sitting at it -- a stale run's residue, another
        # job's leaf, or something an operator dropped there -- would have been
        # silently treated as this job's own staging and later granted deletion
        # rights. Reuse is now allowed only when the registry still holds a
        # generation-verified registration **for this exact job**; every other
        # case is reported instead of adopted.
        if not is_registered_leaf(project_id, planned_stage, owner_job_id=job_id):
            stats["failed"] += 1
            _record_failure(
                failures, code=CLIP_FAILED,
                message=(
                    "暂存叶子已存在但不是本任务登记创建的目录，拒绝接管："
                    f"{candidate.segment_id}"
                ),
            )
            return
        # Our own leaf from an earlier attempt at this same segment: reuse it.
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        stats["failed"] += 1
        _record_failure(failures, code=CLIP_FAILED, message=str(exc))
        return
    staging_roots.append(str(planned_stage))

    try:
        with session_scope() as s:
            asset = s.get(Asset, candidate.asset_id)
            if asset is None:
                raise VideoClipError("SEGMENT_INVALID", "asset disappeared")

        clip = derive_clip(
            project_id=project_id,
            job_id=job_id,
            spec=ClipSpec(
                segment_id=candidate.segment_id,
                asset_id=candidate.asset_id,
                source_path=candidate.source_path,
                start_sec=candidate.start_sec,
                end_sec=candidate.end_sec,
                max_duration_sec=plan.max_clip_sec,
            ),
            asset=asset,
            cancel_check=is_cancelled,
            # TSB-340: hand derive_clip the leaf we already pre-registered, so it
            # does not try to create (and thus re-own) the same directory.
            staged=planned_stage,
        )
    except JobCancelled:
        raise
    except VideoClipError as exc:
        stats["failed"] += 1
        va.record_failure(
            candidate=candidate, job_id=job_id, error_code=exc.code,
            error_message=exc.message, protocol=plan.capabilities.protocol,
        )
        _record_failure(
        failures,
        code=exc.code,
        message=exc.message,
    )
        return
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        stats["failed"] += 1
        _record_failure(
        failures,
        code=CLIP_FAILED,
        message=str(exc),
    )
        return

    # TSB-338: the encoder decides the real length, which can exceed what was
    # requested (keyframe alignment). Re-check the *derived* seconds against the
    # plan's cap; validating only the request would let the real spend drift.
    try:
        plan.budget.note_derived([(clip.duration_sec, clip.path.stat().st_size)])
    except ModelError as exc:
        stats["failed"] += 1
        stats["budget_exceeded"] = True
        stats["budget_ledger"] = plan.budget.ledger()
        _record_failure(failures, code=exc.code, message=str(exc))
        return

    # The exact leaf was already recorded before derivation (see above). Assert the
    # two agree rather than appending a second entry: a mismatch would mean the
    # cleaner is pointed at a directory derive_clip never created.
    actual_stage = str(clip.path.parent)
    if actual_stage not in staging_roots:
        staging_roots.append(actual_stage)
    stats["clip_bytes"] += clip.size_bytes
    stats["clip_seconds"] = round(stats["clip_seconds"] + clip.duration_sec, 3)

    if not plan.force and va.already_done(
        candidate=candidate, clip_sha256=clip.sha256, model_id=settings.model_name,
        prompt_version=VIDEO_PROMPT_VERSION, protocol=plan.capabilities.protocol,
        job_id=job_id,
    ):
        stats["skipped"] += 1
        stats["cached"] += 1
        return

    prompt = build_prompt(
        clip_id=clip.clip_id, segment_id=candidate.segment_id,
        duration_sec=clip.duration_sec,
        currency_note=(
            "Note: the server may sample this video rather than decode every frame; "
            "do not claim frame-level precision."
            if not plan.capabilities.temporal_sampling_disclosed else ""
        ),
    )

    from .authorization import authorize_upload

    try:
        context = authorize_upload(
            project_id=project_id, upload_acknowledged=True, purpose="video_analyze"
        )
    except ModelError as exc:
        stats["failed"] += 1
        _record_failure(
        failures,
        code=exc.code,
        message=str(exc),
    )
        return

    from .providers.base import VideoPart, VideoRequest

    request = VideoRequest(
        prompt=prompt,
        video=VideoPart(
            clip_id=clip.clip_id,
            data=clip.path.read_bytes(),
            mime_type="video/mp4",
            source_offset_sec=clip.source_clip_start_sec,
            duration_sec=clip.duration_sec,
            segment_id=candidate.segment_id,
            has_audio=False,
        ),
        system=SYSTEM_PROMPT,
        json_object=True,
        max_tokens=int(settings.vision_max_tokens),
        authorization=context,
        protocol=plan.capabilities.protocol,
    )

    # TSB-338: charge the running budget immediately before the attempt, at the
    # size that will actually go on the wire (Base64 + JSON envelope, not the raw
    # clip). Raising here means nothing is sent. The provider retries internally,
    # so the charge is per logical call; the per-retry byte growth is bounded by
    # the same cap because the whole body is charged again on the next segment.
    body_bytes = _request_body_bytes(request)
    try:
        budget_ledger = plan.budget.note_attempt(calls=1, bytes_sent=body_bytes)
    except ModelError as exc:
        stats["failed"] += 1
        stats["budget_exceeded"] = True
        stats["budget_ledger"] = plan.budget.ledger()
        _record_failure(failures, code=exc.code, message=str(exc))
        return
    stats["budget_ledger"] = budget_ledger

    try:
        response = provider.complete_video(request, cancel_check=is_cancelled)
    except JobCancelled:
        raise
    except ModelError as exc:
        stats["failed"] += 1
        va.record_failure(
            candidate=candidate, job_id=job_id, error_code=exc.code,
            error_message=str(exc), model_id=settings.model_name,
            protocol=plan.capabilities.protocol,
        )
        _record_failure(
        failures,
        code=exc.code,
        message=str(exc),
    )
        return
    except Exception as exc:  # noqa: BLE001
        stats["failed"] += 1
        _record_failure(
        failures,
        code=MODEL_PROVIDER_FAULT,
        message=str(exc),
    )
        return

    usage_totals["calls"] = usage_totals.get("calls", 0) + response.attempts
    usage_totals["clip_bytes"] = usage_totals.get("clip_bytes", 0) + response.sent_bytes
    _add_usage(usage_totals, response.usage)

    try:
        parsed = extract_json_object(response.text)
        validated = validate_temporal_events(
            parsed,
            clip=clip,
            segment_start_sec=candidate.start_sec,
            segment_end_sec=candidate.end_sec,
            expected_clip_id=clip.clip_id,
        )
    except VideoSchemaError as exc:
        stats["failed"] += 1
        va.record_failure(
            candidate=candidate, job_id=job_id, error_code=exc.code,
            error_message=exc.message, model_id=settings.model_name,
            protocol=plan.capabilities.protocol,
        )
        _record_failure(
        failures,
        code=exc.code,
        message=exc.message,
    )
        return

    try:
        va.store_observation(
            candidate=candidate,
            validated=validated,
            clip=clip,
            job_id=job_id,
            provider_name=getattr(provider, "name", "native_video"),
            model_id=response.model_id or settings.model_name,
            prompt_version=VIDEO_PROMPT_VERSION,
            protocol=plan.capabilities.protocol,
            usage={**response.usage, "latency_ms": response.latency_ms,
                   "attempts": response.attempts, "request_id": response.request_id},
        )
    except JobCancelled:
        raise
    except ModelError as exc:
        # A refused publication (source changed, segment invalidated) is a real
        # failure and must not be counted as a stored success.
        stats["failed"] += 1
        _record_failure(
        failures,
        code=exc.code,
        message=str(exc),
    )
        return
    except Exception as exc:  # noqa: BLE001
        stats["failed"] += 1
        _record_failure(
        failures,
        code=PUBLISH_FAILED,
        message=str(exc),
    )
        return

    stats["events"] += len(validated.events)
    stats["done"] += 1


def _add_usage(totals: dict, usage: dict) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)):
            totals[key] = totals.get(key, 0) + int(value)
