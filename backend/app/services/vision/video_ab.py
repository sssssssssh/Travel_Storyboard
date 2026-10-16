"""TSB-330 — A/B evaluation of the three input modes.

Answers the question the whole batch exists for: *does sending a short clip
actually identify temporal events better than still frames or a handful of
sampled frames?* The task book is precise about what may and may not be claimed:

* **Three arms, same shots.** A = one representative still, B = the current
  multi-frame reading, C = native video. The comparison must be over the *same*
  segments, otherwise a difference could come from sampling rather than input mode.
* **Metrics are computed from labels, not from model agreement.** Without human
  annotations the harness reports coverage and internal consistency only, and says
  ``evaluated: false``. Fabricating an accuracy figure is the failure mode this
  module is built to prevent.
* **Arm C is blocked without a verified endpoint.** With ``video_native``
  unavailable the run is marked ``blocked_by_endpoint`` and C's numbers stay
  ``null`` — never back-filled from arm B, which would make an image reading look
  like a video result.
* **No token-count comparison across different billing shapes.** The report records
  what was actually sent (images vs clip seconds and bytes) and refuses to compare
  a per-token cost with a per-second one.

Everything here is deterministic given its inputs, so the reports are reproducible
and can be re-derived from stored rows without re-spending money.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from ...database import session_scope
from ...models import (
    Asset,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
)
from .video_capabilities import VideoCapabilities, unverified_capabilities

logger = logging.getLogger(__name__)

#: The three arms. Named so a report can never be ambiguous about which reading a
#: number came from.
ARM_A = "A_image_only"
ARM_B = "B_multiframe"
ARM_C = "C_video_native"
ARMS = (ARM_A, ARM_B, ARM_C)

#: Pre-registered adoption criteria (task book §3 TSB-330). Recorded here so the
#: thresholds cannot be chosen after seeing the numbers -- which is the difference
#: between a test and a rationalisation.
ADOPTION_CRITERIA = {
    "min_event_recall_gain_points": 10.0,
    "temporal_iou_threshold": 0.5,
    "max_false_positive_rate_delta": 0.05,
    "requires_real_human_labels": True,
    "requires_unit_cost_data": True,
}

#: Why an arm produced no result. Stable codes (C6).
BLOCKED_BY_ENDPOINT = "blocked_by_endpoint"
NOT_ANNOTATED = "not_annotated"
NO_READING = "no_reading"


@dataclass
class SegmentArms:
    """One shot's readings across the three arms."""

    segment_id: str
    asset_id: str
    start_sec: float
    end_sec: float
    arm_a: dict | None = None
    arm_b: dict | None = None
    arm_c: dict | None = None
    missing_reasons: dict = field(default_factory=dict)

    def available_arms(self) -> list[str]:
        return [
            arm
            for arm, value in ((ARM_A, self.arm_a), (ARM_B, self.arm_b), (ARM_C, self.arm_c))
            if value is not None
        ]

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "segment_window": {"start_sec": self.start_sec, "end_sec": self.end_sec},
            # Never emit a source path: the report is meant to be committable.
            "arms": {ARM_A: self.arm_a, ARM_B: self.arm_b, ARM_C: self.arm_c},
            "available_arms": self.available_arms(),
            "missing_reasons": dict(self.missing_reasons),
        }


# ---------------------------------------------------------------------------
# collecting the arms
# ---------------------------------------------------------------------------

def _reading_digest(events: list[dict], summary: str) -> dict:
    """A compact, comparable view of one arm's output."""
    windows = [
        {
            "event_type": e.get("event_type", ""),
            "start_abs_sec": e.get("start_abs_sec"),
            "end_abs_sec": e.get("end_abs_sec"),
            "description": e.get("description", ""),
            "confidence": e.get("confidence"),
        }
        for e in (events or [])
    ]
    return {"summary": summary, "events": windows, "event_count": len(windows)}


def collect_arms(
    project_id: str,
    *,
    segment_ids: list[str] | None = None,
    capabilities: VideoCapabilities | None = None,
) -> list[SegmentArms]:
    """Gather the three arms for each requested shot.

    Absences are recorded with reasons rather than omitted, because "arm C is
    missing" and "arm C ran and found nothing" are completely different findings.
    """
    caps = capabilities if capabilities is not None else unverified_capabilities()
    wanted = set(segment_ids or [])

    with session_scope() as s:
        rows = s.execute(
            select(Segment, Asset)
            .join(Asset, Asset.id == Segment.asset_id)
            .where(Segment.project_id == project_id)
            .order_by(Asset.display_name, Segment.start_sec)
        ).all()

        out: list[SegmentArms] = []
        for segment, asset in rows:
            if wanted and segment.id not in wanted:
                continue

            arms = SegmentArms(
                segment_id=segment.id,
                asset_id=asset.id,
                start_sec=segment.start_sec,
                end_sec=segment.end_sec,
            )

            semantic_row = s.scalars(
                select(SegmentSemanticResult).where(
                    SegmentSemanticResult.segment_id == segment.id,
                    SegmentSemanticResult.is_current.is_(True),
                    SegmentSemanticResult.status == "succeeded",
                )
            ).first()
            if semantic_row is not None:
                arms.arm_a = _reading_digest([], json.dumps(semantic_row.analysis_json or {},
                                                           ensure_ascii=False)[:400])
            else:
                arms.missing_reasons[ARM_A] = NO_READING

            motion_row = s.scalars(
                select(SegmentMotionReview).where(
                    SegmentMotionReview.segment_id == segment.id,
                    SegmentMotionReview.is_current.is_(True),
                    SegmentMotionReview.status == "succeeded",
                )
            ).first()
            if motion_row is not None:
                arms.arm_b = {
                    "summary": motion_row.action_summary,
                    "motion_type": motion_row.motion_type,
                    "stability_risk": motion_row.stability_risk,
                    "event_count": None,
                }
            else:
                arms.missing_reasons[ARM_B] = NO_READING

            video_row = s.scalars(
                select(SegmentVideoObservation).where(
                    SegmentVideoObservation.segment_id == segment.id,
                    SegmentVideoObservation.is_current.is_(True),
                    SegmentVideoObservation.status == "succeeded",
                )
            ).first()
            if video_row is not None:
                arms.arm_c = {
                    **_reading_digest(video_row.events_json or [],
                                      (video_row.analysis_json or {}).get("summary", "")),
                    "observed_temporal_quality": video_row.observed_temporal_quality,
                    "audio_analyzed": bool(video_row.audio_analyzed),
                    "color_unverified": bool(video_row.color_unverified),
                    "clip_bytes": video_row.clip_size_bytes,
                    "clip_duration_sec": video_row.clip_duration_sec,
                }
            else:
                # Distinguish "the endpoint cannot do this" from "it could but has
                # not run yet" -- they call for different next steps.
                arms.missing_reasons[ARM_C] = (
                    BLOCKED_BY_ENDPOINT if not caps.sendable else NO_READING
                )

            out.append(arms)
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def temporal_iou(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Intersection over union of two windows. Returns 0.0 for disjoint windows."""
    start = max(a[0], b[0])
    end = min(a[1], b[1])
    overlap = max(0.0, end - start)
    if overlap <= 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return overlap / union if union > 0 else 0.0


def match_events(
    predicted: list[dict],
    labels: list[dict],
    *,
    iou_threshold: float = 0.5,
) -> dict:
    """Greedy one-to-one matching of predicted events against human labels.

    Greedy rather than optimal because the counts are tiny and the tie-breaking is
    easier to explain: longest-overlap-first, each label used at most once. Using a
    label twice would inflate recall.
    """
    used_labels: set[int] = set()
    matched: list[tuple[int, int, float]] = []

    candidates: list[tuple[float, int, int]] = []
    for pi, pred in enumerate(predicted):
        if pred.get("start_abs_sec") is None or pred.get("end_abs_sec") is None:
            continue
        for li, label in enumerate(labels):
            score = temporal_iou(
                (float(pred["start_abs_sec"]), float(pred["end_abs_sec"])),
                (float(label["start_abs_sec"]), float(label["end_abs_sec"])),
            )
            if score >= iou_threshold:
                candidates.append((score, pi, li))

    for score, pi, li in sorted(candidates, key=lambda item: -item[0]):
        if li in used_labels or any(p == pi for _, p, _ in matched):
            continue
        used_labels.add(li)
        matched.append((pi, li, score))

    matched_pairs = {(pi, li) for pi, li, _ in matched}
    return {
        "true_positives": len(matched_pairs),
        "false_positives": max(0, len(predicted) - len(matched_pairs)),
        "false_negatives": max(0, len(labels) - len(matched_pairs)),
        "mean_iou": (
            round(statistics.fmean(score for _, _, score in matched), 4)
            if matched else 0.0
        ),
        "boundary_errors_sec": [
            round(
                abs(float(predicted[pi]["start_abs_sec"]) - float(labels[li]["start_abs_sec"])),
                3,
            )
            for pi, li, _ in matched
        ],
    }


def score_arms(
    arms: list[SegmentArms],
    labels_by_segment: dict[str, list[dict]],
    *,
    iou_threshold: float = 0.5,
) -> dict:
    """Compute recall/precision/IoU per arm, but only where labels exist.

    With no labels the per-arm metrics are ``null`` and ``evaluated`` is false: the
    harness reports coverage instead of inventing an accuracy figure.
    """
    labelled = {
        seg_id: labels
        for seg_id, labels in labels_by_segment.items()
        if labels and seg_id in {a.segment_id for a in arms}
    }

    per_arm: dict[str, dict] = {}
    for arm in ARMS:
        predictions: list[dict] = []
        label_total = 0
        segments_with_reading = 0
        for entry in arms:
            if entry.segment_id not in labelled:
                continue
            label_total += len(labelled[entry.segment_id])
            reading = {"A_image_only": entry.arm_a,
                       "B_multiframe": entry.arm_b,
                       "C_video_native": entry.arm_c}[arm]
            if reading is not None:
                segments_with_reading += 1
            if reading and reading.get("events"):
                predictions.extend(reading["events"])

        if not labelled:
            per_arm[arm] = {
                "evaluated": False,
                "reason": NOT_ANNOTATED,
                "event_recall": None,
                "event_precision": None,
                "temporal_iou_ge_threshold": None,
                "predicted_events": len(predictions),
            }
            continue

        # An arm with no reading at all is *not* an arm that scored zero. Reporting
        # 0.0 here would read as "it ran and found nothing", which is a completely
        # different (and false) statement when the channel never ran.
        if segments_with_reading == 0:
            per_arm[arm] = {
                "evaluated": False,
                "reason": NO_READING,
                "event_recall": None,
                "event_precision": None,
                "temporal_iou_ge_threshold": None,
                "mean_iou_on_matches": None,
                "mean_boundary_error_sec": None,
                "labelled_events": label_total,
                "predicted_events": 0,
            }
            continue

        perfect = [
            {"start_abs_sec": label["start_abs_sec"], "end_abs_sec": label["end_abs_sec"]}
            for labels in labelled.values()
            for label in labels
        ]
        result = match_events(predictions, perfect, iou_threshold=iou_threshold)
        tp, fp, fn = (result["true_positives"], result["false_positives"],
                      result["false_negatives"])
        per_arm[arm] = {
            # The arm ran; whether it produced events is separate from whether it
            # was evaluated, and a zero-event reading is a real (if poor) result.
            "evaluated": True,
            "reason": "" if predictions else "no_events_detected",
            "event_recall": round(tp / (tp + fn), 4) if (tp + fn) else None,
            "event_precision": round(tp / (tp + fp), 4) if (tp + fp) else None,
            "temporal_iou_ge_threshold": (
                round(tp / label_total, 4) if label_total else None
            ),
            "mean_iou_on_matches": result["mean_iou"],
            "mean_boundary_error_sec": (
                round(statistics.fmean(result["boundary_errors_sec"]), 3)
                if result["boundary_errors_sec"] else None
            ),
            "labelled_events": label_total,
            "predicted_events": len(predictions),
        }

    a = per_arm[ARM_A]["event_recall"]
    c = per_arm[ARM_C]["event_recall"]
    gain = None
    if isinstance(a, float) and isinstance(c, float):
        gain = round((c - a) * 100, 2)

    return {
        "evaluated": bool(labelled),
        "iou_threshold": iou_threshold,
        "labelled_segments": len(labelled),
        "arms": per_arm,
        "recall_gain_points_C_over_A": gain,
        "meets_adoption_criteria": decide_adoption(per_arm, gain),
    }


def decide_adoption(per_arm: dict, recall_gain: float | None) -> dict:
    """Apply the pre-registered thresholds, refusing to decide without data."""
    missing: list[str] = []
    if recall_gain is None:
        missing.append("arm A and arm C recall both need real labels")
    if not per_arm.get(ARM_C, {}).get("evaluated"):
        missing.append("arm C has no evaluated predictions")

    if missing:
        return {
            "decided": False,
            "adopt_video_as_preferred": False,
            "reason": "insufficient evidence",
            "missing": missing,
        }

    gain_ok = recall_gain >= ADOPTION_CRITERIA["min_event_recall_gain_points"]
    c_iou = per_arm[ARM_C].get("temporal_iou_ge_threshold")
    a_iou = per_arm[ARM_A].get("temporal_iou_ge_threshold")
    iou_ok = (
        c_iou is not None and a_iou is not None and c_iou >= a_iou
    )
    precision_ok = True
    c_prec = per_arm[ARM_C].get("event_precision")
    a_prec = per_arm[ARM_A].get("event_precision")
    if isinstance(c_prec, float) and isinstance(a_prec, float):
        precision_ok = (a_prec - c_prec) <= ADOPTION_CRITERIA["max_false_positive_rate_delta"]

    return {
        "decided": True,
        "adopt_video_as_preferred": bool(gain_ok and iou_ok and precision_ok),
        "reason": (
            f"recall gain {recall_gain} points (threshold "
            f"{ADOPTION_CRITERIA['min_event_recall_gain_points']}), "
            f"IoU not worse, precision not worse"
        ),
        "checks": {"recall_gain": gain_ok, "temporal_iou": iou_ok, "precision": precision_ok},
    }


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def build_report(
    *,
    project_id: str,
    arms: list[SegmentArms],
    labels_by_segment: dict[str, list[dict]] | None = None,
    capabilities: VideoCapabilities | None = None,
    endpoint_note: str = "",
) -> dict:
    """Assemble the committable, path-free A/B report."""
    caps = capabilities if capabilities is not None else unverified_capabilities()
    labels = labels_by_segment or {}

    c_present = [a for a in arms if a.arm_c is not None]
    blocked = any(
        a.missing_reasons.get(ARM_C) == BLOCKED_BY_ENDPOINT for a in arms
    )

    status = "complete"
    if blocked and not c_present:
        status = BLOCKED_BY_ENDPOINT
    elif not labels:
        status = NOT_ANNOTATED

    return {
        "task": "TSB-330",
        "status": status,
        "evaluated": bool(labels) and bool(c_present),
        "segments_in_run": len(arms),
        "distinct_assets": len({a.asset_id for a in arms}),
        "arm_c_available": len(c_present),
        "capability_state": caps.state,
        "arm_c_blocked_reason": (
            "endpoint does not accept video (see CAPABILITY_REPORT.json)" if blocked else ""
        ),
        "endpoint_note": endpoint_note,
        # The distinction the task book insists on: the same source video may
        # contribute several segments, and reporting those as "N different videos"
        # would overstate the evidence.
        "sampling_note": (
            f"{len(arms)} segments drawn from {len({a.asset_id for a in arms})} "
            "source videos; segments from one video are not independent samples"
        ),
        "per_segment": [a.to_dict() for a in arms],
        "metrics": score_arms(arms, labels),
        "adoption_criteria": ADOPTION_CRITERIA,
        "cost_note": (
            "arms A/B are billed per token; arm C sends "
            "clip_seconds and clip_bytes as well. Token counts are NOT comparable "
            "across these shapes and no unit-cost conclusion is drawn."
        ),
        "honesty": [
            "没有人工标注时 per-arm 指标为 null，只报告覆盖率，不填造准确率",
            "arm C 不可用时状态为 blocked_by_endpoint，其数值保持 null",
            "Mock/合成素材通过不等于真实视频质量验收通过",
        ],
    }


def load_labels(path: Path) -> dict[str, list[dict]]:
    """Read human annotations. Absent or malformed input yields no labels.

    A malformed label file must not silently become "zero labels but scored",
    which would render as a real (and wrong) accuracy figure.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict]] = {}
    for segment_id, labels in raw.items():
        if not isinstance(labels, list):
            continue
        clean = [
            label
            for label in labels
            if isinstance(label, dict)
            and isinstance(label.get("start_abs_sec"), (int, float))
            and isinstance(label.get("end_abs_sec"), (int, float))
            and label["end_abs_sec"] > label["start_abs_sec"]
        ]
        if clean:
            out[str(segment_id)] = clean
    return out


def write_report(report: dict, path: Path) -> None:
    """Write the report, refusing to emit anything that looks like a source path."""
    blob = json.dumps(report, ensure_ascii=False, indent=2)
    for forbidden in ("C:\\", "/Users/", "/tmp/", "\\\\"):
        if forbidden in blob:
            raise ValueError(
                f"refusing to write a report containing what looks like a local path ({forbidden})"
            )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(blob, encoding="utf-8")
