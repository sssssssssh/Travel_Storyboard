"""TSB-306 — dynamic-review candidate generation.

A shot qualifies for a multi-frame (dynamic) review from one of three sources:

* ``semantic`` — the still-image model asked for it (``needs_motion_review``);
* ``rule``     — its measured frame difference is high;
* ``human``    — a person flagged it.

The rule source is presented as **"suspected change, needs verification"**, never
as a proven shake. The earlier ``ai_review.py`` script computed a mean frame
difference and labelled the result "疑似抖动或快速甩镜"; a pixel delta cannot
establish intent, and AGENTS.md §5.3 warns that only asserting the observable
quantity is defensible. So the quantity is reported and the verdict is left to
the multi-frame pass or to a human.

A shot with fewer than two frames is **not** excluded: it is marked
``temporal_coverage_insufficient`` so it can be re-sampled for motion. Silently
dropping it is what would hide a coverage gap.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Frame, Segment, SegmentSemanticResult
from .semantic import INVALID_SEGMENT_STATUSES

logger = logging.getLogger(__name__)

#: Mean absolute inter-frame difference thresholds. These are *hints*.
MOTION_HIGH = 0.20
MOTION_MEDIUM = 0.12


def build_candidates(db: Session, project_id: str) -> list[dict]:
    """Combine every candidate source into one deduplicated, labelled list."""
    segments = db.scalars(
        select(Segment).where(
            Segment.project_id == project_id,
            Segment.status.not_in(INVALID_SEGMENT_STATUSES),
            Segment.user_rating != "exclude",
        )
    ).all()
    if not segments:
        return []

    segment_ids = [s.id for s in segments]

    frames_by_segment: dict[str, list[Frame]] = {}
    for frame in db.scalars(select(Frame).where(Frame.segment_id.in_(segment_ids))).all():
        frames_by_segment.setdefault(frame.segment_id, []).append(frame)

    semantic_by_segment: dict[str, SegmentSemanticResult] = {}
    for row in db.scalars(
        select(SegmentSemanticResult).where(
            SegmentSemanticResult.segment_id.in_(segment_ids),
            SegmentSemanticResult.is_current.is_(True),
        )
    ).all():
        semantic_by_segment[row.segment_id] = row

    candidates: list[dict] = []
    for segment in segments:
        frames = frames_by_segment.get(segment.id, [])
        sources: list[str] = []
        reasons: list[str] = []

        semantic = semantic_by_segment.get(segment.id)
        if semantic is not None:
            review = (semantic.evidence_json or {}).get("review") or {}
            if review.get("needs_motion_review"):
                sources.append("semantic")
                for reason in review.get("reasons") or []:
                    reasons.append(f"模型建议：{reason}")
                if not review.get("reasons"):
                    reasons.append("模型建议：需要多帧核验")

        motion = _motion_hint(project_id, frames)
        # Only a measured change above the medium threshold is a reason to look
        # again. A low difference is the *absence* of a signal, not a weak one:
        # queueing it would fill the dynamic pass with static footage.
        if motion is not None and motion[1] != "low":
            value, priority = motion
            sources.append("rule")
            # Deliberately worded as an observation plus a request to verify.
            reasons.append(
                f"规则提示：代表帧平均像素差 {value:.3f}（阈值 {MOTION_MEDIUM}/{MOTION_HIGH}），"
                "疑似画面变化，需核验；帧差本身不能证明抖动或甩镜"
            )

        insufficient = len(frames) < 2
        if insufficient:
            # Not dropped: it is precisely the case a dynamic pass can fix.
            reasons.append("只有 1 张代表帧，无法用静帧判断运动（temporal_coverage_insufficient）")
            if "rule" not in sources and "semantic" not in sources:
                sources.append("coverage")

        # A measured-but-static shot is not queued: the absence of a change is
        # not a weak reason to look again.
        if not sources:
            continue

        priority = _priority(sources, semantic, motion)
        candidates.append(
            {
                "segment_id": segment.id,
                "asset_id": segment.asset_id,
                "start_sec": float(segment.start_sec),
                "end_sec": float(segment.end_sec),
                "frame_count": len(frames),
                "sources": sources,
                "reasons": reasons,
                "temporal_coverage_insufficient": insufficient,
                "suggested_priority": priority,
            }
        )

    candidates.sort(key=lambda c: (c["suggested_priority"] != "high", -c["frame_count"]))
    return candidates


def _motion_hint(project_id: str, frames: list[Frame]) -> tuple[float, str] | None:
    """Mean absolute difference between consecutive representative frames.

    Returns ``None`` when fewer than two frames exist (no measurement is
    possible) — as opposed to returning a fabricated zero.
    """
    if len(frames) < 2:
        return None

    ordered = sorted(frames, key=lambda f: f.time_sec)
    deltas: list[float] = []
    for first, second in zip(ordered, ordered[1:]):
        path_a = _resolve(project_id, first.image_path)
        path_b = _resolve(project_id, second.image_path)
        if path_a is None or path_b is None:
            continue
        value = _mean_abs_diff(path_a, path_b)
        if value is not None:
            deltas.append(value)
    if not deltas:
        return None

    deltas.sort()
    median = deltas[len(deltas) // 2]
    if median >= MOTION_HIGH:
        return median, "high"
    if median >= MOTION_MEDIUM:
        return median, "medium"
    return median, "low"


def _resolve(project_id: str, stored_path: str) -> str | None:
    """Resolve a stored frame reference to an absolute path.

    ``Frame.image_path`` is stored **relative to the data dir** (or absolute for
    a project with a custom ``output_dir``). Feeding it straight to PIL produced
    a bogus path and a silent zero-difference reading, which would have hidden
    every genuinely moving shot. ``resolve_in_project`` handles both forms and
    also confines the result to the owning project.
    """
    from ...paths import resolve_in_project

    try:
        return str(resolve_in_project(project_id, stored_path))
    except (ValueError, OSError) as exc:
        logger.warning("Could not resolve frame path %r: %s", stored_path, exc)
        return None


def _mean_abs_diff(path_a: str, path_b: str) -> float | None:
    """Decode both frames and return the normalised mean absolute difference.

    Uses ``safe_image_open`` + NumPy rather than ``cv2.imread``, because the
    latter returns ``None`` (silently) for long or non-ASCII paths — see
    AGENTS.md §5.1.
    """
    try:
        import numpy as np

        from ...ffmpeg import safe_image_open

        with safe_image_open(path_a) as img_a, safe_image_open(path_b) as img_b:
            a = np.asarray(img_a.convert("L"), dtype="float32")
            b = np.asarray(img_b.convert("L"), dtype="float32")
        if a.shape != b.shape:
            return None
        return float(np.mean(np.abs(a - b)) / 255.0)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("Could not measure frame difference: %s", exc)
        return None


def _priority(sources: list[str], semantic, motion) -> str:
    if "semantic" in sources and semantic is not None:
        review = (semantic.evidence_json or {}).get("review") or {}
        if review.get("reasons"):
            return "high"
    if motion is not None and motion[1] == "high":
        return "high"
    if motion is not None and motion[1] == "medium":
        return "medium"
    if "coverage" in sources:
        return "medium"
    return "low"
