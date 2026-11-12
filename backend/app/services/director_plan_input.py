"""TSB-369-A — the evidence-bound planning input.

This module assembles everything a planner is allowed to know about a project,
and nothing else. It exists so that three very different planners — deterministic
offline rules, a test mock, and (one day) a real model — all read **the same**
facts through **the same** projections, and so that "what the system has
actually observed" is a value with a type rather than a habit of querying.

Design rules, each of which is enforced by a test:

* **Built from the context, not from the project.** Only shots the context
  offered (``eligibility == eligible``) enter. A planner cannot widen its own
  input by asking.
* **Three channels stay three channels.** A still-frame reading, a multi-frame
  motion conclusion and a native-video observation each carry their own state,
  their own result id and their own limitations. Nothing collapses them into one
  "analyzed" flag, and ``not_analyzed`` is a first-class answer.
* **Uncertainty is data.** ``uncertain`` / ``not_visible`` /
  ``freshness_unverified`` travel verbatim; the planner is told, not protected.
* **No side effects.** Reading the input never creates a storyboard, a version, a
  job, or a row. In particular it never calls ``get_current_version()``, which
  auto-creates an empty v1.
* **Bounded cost.** One batched pass per channel, keyed by segment id, so the
  statement count does not grow with the size of the library.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import session_scope
from ..models import (
    Asset,
    Segment,
    StoryboardVersion,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
    Storyboard,
)
from . import director_candidates as dc
from . import director_query as dq

#: Budget limits. The context API caps ``max_candidates`` at 200; planning uses a
#: tighter default because a planner reading 200 shots of prose is neither useful
#: nor affordable.
DEFAULT_MAX_CANDIDATES = 40
HARD_MAX_CANDIDATES = 100

MAX_SUBJECTS = 3
MAX_ACTIONS = 3
MAX_MOTION_SUMMARY = 300
MAX_VIDEO_SUMMARY = 300
MAX_VIDEO_EVENTS = 12


class PlanningInputError(Exception):
    """Raised when the input cannot be assembled for a stated reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HumanFacts:
    """What the user said. Always data, never an instruction."""

    rating: str
    rating_source: str
    story_role: str
    note: str
    note_source: str = "human"


@dataclass(frozen=True)
class TechnicalFacts:
    duration_sec: float
    quality_metrics: dict[str, Any]
    is_trimmed: bool


@dataclass(frozen=True)
class StaticEvidence:
    state: str
    result_id: str | None
    evidence_state: str
    visual: dict[str, Any]
    model: dict[str, Any]
    limitations: list[str]


@dataclass(frozen=True)
class MotionEvidence:
    state: str
    result_id: str | None
    motion_type: str
    action_summary: str
    stability_risk: str
    human_verdict: str
    limitations: list[str]


@dataclass(frozen=True)
class VideoEvent:
    """One temporal event in **source absolute seconds**."""

    start_sec: float
    end_sec: float
    event_type: str
    description: str
    confidence: float | None
    confidence_calibration: str = "uncalibrated"


@dataclass(frozen=True)
class VideoEvidence:
    state: str
    result_id: str | None
    summary: str
    events: list[VideoEvent]
    audio_analyzed: bool
    color_unverified: bool
    observed_temporal_quality: str
    limitations: list[str]


@dataclass(frozen=True)
class PlanningEntry:
    """One shot, as the planner sees it."""

    segment_id: str
    asset_id: str
    asset_display_name: str
    source_in_sec: float
    source_out_sec: float
    eligibility: str
    human: HumanFacts
    technical: TechnicalFacts
    static: StaticEvidence
    multiframe: MotionEvidence
    video_native: VideoEvidence
    editorial_priority: str
    limitations: list[str]
    #: True when the shot is locked in the base storyboard version. A draft never
    #: writes, but it must disclose that it is proposing to move a locked shot.
    locked_in_base: bool = False

    @property
    def has_usable_reading(self) -> bool:
        return any(
            ch.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED) and ch.result_id
            for ch in (self.static, self.multiframe, self.video_native)
        )


@dataclass
class PlanningInput:
    project_id: str
    source_input_digest: str
    coverage: dict[str, Any]
    withheld: dict[str, int]
    limitations: list[str]
    base_storyboard_version_id: str | None
    storyboard_absent: bool
    entries: list[PlanningEntry] = field(default_factory=list)

    # The two shapes callers actually need.
    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_input_digest": self.source_input_digest,
            "coverage": self.coverage,
            "withheld": self.withheld,
            "limitations": self.limitations,
            "base_storyboard_version_id": self.base_storyboard_version_id,
            "storyboard_absent": self.storyboard_absent,
            "entries": [
                {
                    "segment_id": e.segment_id,
                    "asset_id": e.asset_id,
                    "source_window": {"start_sec": e.source_in_sec,
                                      "end_sec": e.source_out_sec},
                    "eligibility": e.eligibility,
                    "human": {
                        "rating": e.human.rating,
                        "rating_source": e.human.rating_source,
                        "story_role": e.human.story_role,
                        "note": e.human.note,
                        "note_source": e.human.note_source,
                    },
                    "static": {
                        "state": e.static.state,
                        "result_id": e.static.result_id,
                        "evidence_state": e.static.evidence_state,
                        "visual": e.static.visual,
                    },
                    "multiframe": {
                        "state": e.multiframe.state,
                        "result_id": e.multiframe.result_id,
                        "motion_type": e.multiframe.motion_type,
                        "action_summary": e.multiframe.action_summary,
                    },
                    "video_native": {
                        "state": e.video_native.state,
                        "result_id": e.video_native.result_id,
                        "summary": e.video_native.summary,
                        "events": [
                            {
                                "start_sec": ev.start_sec,
                                "end_sec": ev.end_sec,
                                "event_type": ev.event_type,
                                "description": ev.description,
                            }
                            for ev in e.video_native.events
                        ],
                        "audio_analyzed": e.video_native.audio_analyzed,
                    },
                }
                for e in self.entries
            ],
        }


# ---------------------------------------------------------------------------
# Storyboard state — read only, never auto-creating
# ---------------------------------------------------------------------------

def _storyboard_state(
    session: Session, project_id: str,
) -> tuple[str | None, bool, frozenset[str]]:
    """``(current_version_id, absent, locked_segment_ids)`` — strictly read-only.

    Deliberately does NOT call ``get_current_version()``: that helper auto-creates
    an empty v1 for a new board, which would turn a read-only preview into a write.

    The locked set is read straight off the version's shot snapshots. It is used
    only to annotate the plan ("this shot is locked"), never to alter it.
    """
    board = session.scalars(
        select(Storyboard).where(Storyboard.project_id == project_id)
    ).first()
    if board is None or not board.current_version_id:
        return None, True, frozenset()

    locked: set[str] = set()
    version = session.get(StoryboardVersion, board.current_version_id)
    if version is not None and version.board_id == board.id:
        for shot in (version.shots or []):
            if isinstance(shot, dict) and shot.get("locked"):
                seg = shot.get("segment_id")
                if seg:
                    locked.add(str(seg))
    return board.current_version_id, False, frozenset(locked)


# ---------------------------------------------------------------------------
# Batched evidence loading
# ---------------------------------------------------------------------------

def _load_current(session: Session, model, segment_ids: list[str],
                  project_id: str) -> dict[str, list]:
    if not segment_ids:
        return {}
    rows = session.scalars(
        select(model)
        .where(model.project_id == project_id, model.segment_id.in_(segment_ids))
        .order_by(model.segment_id, model.created_at.desc(), model.id.desc())
    ).all()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.segment_id, []).append(row)
    return grouped


def _bounded_list(values: Any, limit: int, max_len: int) -> list[str]:
    out: list[str] = []
    for v in (values or [])[:limit]:
        out.append(str(v)[:max_len])
    return out



def _apply_review(evidence, verdicts: dict[str, dict[str, str]],
                  limitations: list[str]):
    """Withhold claims a human marked wrong from one evidence block.

    Returns a new evidence object; the stored reading is not touched. Disclosure
    is per-block *and* per-shot so a reader of the input cannot miss it.
    """
    from . import evaluation_feedback as fb

    result_id = getattr(evidence, "result_id", None)
    if not result_id:
        return evidence
    judged = verdicts.get(result_id) or {}
    if not judged:
        return evidence

    if isinstance(evidence, StaticEvidence):
        filtered, reasons = fb.filter_visual(evidence.visual, judged)
        if not reasons:
            return evidence
        limitations.extend(reasons)
        return replace(evidence, visual=filtered,
                       limitations=list(evidence.limitations) + reasons)

    if isinstance(evidence, MotionEvidence):
        # filter_visual works on the readings shape (values are lists), because the
        # claim keys come from that projection -- not from these scalar fields.
        visual = {
            "action_summary": [evidence.action_summary] if evidence.action_summary else [],
            "motion_type": [evidence.motion_type] if evidence.motion_type else [],
            "stability_risk": ([evidence.stability_risk]
                               if evidence.stability_risk else []),
        }
        filtered, reasons = fb.filter_visual(visual, judged)
        if not reasons:
            return evidence
        limitations.extend(reasons)

        def _one(name: str, original: str) -> str:
            values = filtered.get(name)
            if values == [original]:
                return original
            return (values[0] if values else "")

        return replace(
            evidence,
            action_summary=_one("action_summary", evidence.action_summary),
            motion_type=_one("motion_type", evidence.motion_type),
            stability_risk=_one("stability_risk", evidence.stability_risk),
            limitations=list(evidence.limitations) + reasons,
        )

    if isinstance(evidence, VideoEvidence):
        # Events are individual claims: a wrong one is dropped, not the summary.
        kept_events = []
        withheld = 0
        for index, event in enumerate(evidence.events):
            verdict = judged.get(f"events:{index}")
            if verdict == "wrong":
                withheld += 1
                continue
            kept_events.append(event)
        reasons: list[str] = []
        if withheld:
            reasons.append(fb.REASON_WITHHELD)
            limitations.extend(reasons)
        if not reasons:
            return evidence
        return replace(evidence, events=kept_events,
                       limitations=list(evidence.limitations) + reasons)

    return evidence

def _static_evidence(rows: list, limitations: list[str]) -> StaticEvidence:
    if not rows:
        limitations.append("static_not_analyzed")
        return StaticEvidence(dc.STATE_NOT_ANALYZED, None, "", {}, {}, ["static_not_analyzed"])
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("static_all_superseded")
        return StaticEvidence(dc.STATE_STALE, rows[0].id, "", {}, {},
                              ["no_current_static_result"])
    if row.status != "succeeded":
        limitations.append("static_analysis_failed")
        return StaticEvidence(dc.STATE_FAILED, row.id, "", {}, {},
                              ["static_analysis_failed"])

    visual = dc._visual_payload(row.analysis_json or {})
    evidence = row.evidence_json or {}
    # Reuse the candidate-layer projection rather than inventing a third parser.
    payload = {
        "scene_tags": _bounded_list(visual.get("scene_tags"), 6, 32),
        "subjects": _bounded_list(visual.get("subjects"), MAX_SUBJECTS, 80),
        "visible_actions": _bounded_list(visual.get("visible_actions"), MAX_ACTIONS, 80),
        "lighting": str(visual.get("lighting") or "")[:32],
        "composition": str(visual.get("composition") or "")[:32],
        "mood": _bounded_list(visual.get("mood"), 4, 32),
    }
    return StaticEvidence(
        state=dc.STATE_UNVERIFIED,
        result_id=row.id,
        evidence_state=str(evidence.get("state") or ""),
        visual=payload,
        model={
            "provider": row.model_provider, "id": row.model_id,
            "prompt_version": row.prompt_version,
        },
        limitations=["source_signature_not_recomputable_without_reanalysis"],
    )


def _motion_evidence(rows: list, limitations: list[str]) -> MotionEvidence:
    if not rows:
        limitations.append("multiframe_not_analyzed")
        return MotionEvidence(dc.STATE_NOT_ANALYZED, None, "", "", "", "",
                              ["multiframe_not_analyzed"])
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("multiframe_all_superseded")
        return MotionEvidence(dc.STATE_STALE, rows[0].id, "", "", "", "",
                              ["no_current_multiframe_result"])
    if row.status != "succeeded":
        limitations.append("multiframe_analysis_failed")
        return MotionEvidence(dc.STATE_FAILED, row.id, "", "", "", "",
                              ["multiframe_analysis_failed"])
    return MotionEvidence(
        state=dc.STATE_UNVERIFIED,
        result_id=row.id,
        motion_type=str(row.motion_type or "")[:32],
        action_summary=str(row.action_summary or "")[:MAX_MOTION_SUMMARY],
        stability_risk=str(row.stability_risk or "")[:32],
        human_verdict=str(row.human_verdict or "")[:32],
        limitations=["input_signature_not_recomputable_offline"],
    )


def _video_evidence(rows: list, segment: Segment, limitations: list[str]) -> VideoEvidence:
    empty = VideoEvidence(dc.STATE_NOT_ANALYZED, None, "", [], False, False, "",
                          ["video_native_not_analyzed"])
    if not rows:
        limitations.append("video_native_not_analyzed")
        return empty
    row = next((r for r in rows if r.is_current), None)
    if row is None:
        limitations.append("video_native_all_superseded")
        return VideoEvidence(dc.STATE_STALE, rows[0].id, "", [], False, False, "",
                             ["no_current_video_native_result"])
    if row.status != "succeeded":
        limitations.append("video_native_analysis_failed")
        return VideoEvidence(dc.STATE_FAILED, row.id, "", [], False, False, "",
                             ["video_native_analysis_failed"])

    if dc._window_changed(segment, row.segment_start_sec, row.segment_end_sec):
        limitations.append("video_native_window_differs_from_current_shot")
        return VideoEvidence(dc.STATE_STALE, row.id, "", [], False, False, "",
                             ["analysis_window_differs_from_current_shot"])

    # Reuse the R2 normalizer so absolute times and legacy shapes behave exactly
    # as they do on the candidates endpoint — not a third parser.
    events, _counters, _reasons = dc._normalize_video_events(
        row.events_json if isinstance(row.events_json, list) else [], segment
    )
    mapped = [
        VideoEvent(
            start_sec=float(e["start_sec"]),
            end_sec=float(e["end_sec"]),
            event_type=str(e.get("event_type") or ""),
            description=str(e.get("label") or ""),
            confidence=e.get("confidence"),
        )
        for e in events[:MAX_VIDEO_EVENTS]
    ]
    analysis = row.analysis_json or {}
    return VideoEvidence(
        state=dc.STATE_UNVERIFIED,
        result_id=row.id,
        summary=str(analysis.get("summary") or "")[:MAX_VIDEO_SUMMARY],
        events=mapped,
        audio_analyzed=bool(row.audio_analyzed),
        color_unverified=bool(row.color_unverified),
        observed_temporal_quality=str(row.observed_temporal_quality or "")[:32],
        limitations=["source_signature_not_recomputable_without_reanalysis"],
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_planning_input(
    project_id: str, *, max_candidates: int = DEFAULT_MAX_CANDIDATES,
    session: Session | None = None,
) -> PlanningInput:
    """Assemble the evidence-bound planning input for one project.

    Raises :class:`PlanningInputError` with code ``PROJECT_NOT_FOUND`` when the
    project does not exist.
    """
    limit = max(1, min(int(max_candidates), HARD_MAX_CANDIDATES))
    if session is not None:
        return _build(session, project_id, limit)
    with session_scope() as s:
        return _build(s, project_id, limit)


def _build(session: Session, project_id: str, limit: int) -> PlanningInput:
    from ..models import Project

    if session.get(Project, project_id) is None:
        raise PlanningInputError("PROJECT_NOT_FOUND", "project not found")

    candidates = dc.collect_candidates(session, project_id, verify_freshness=False)
    ctx = dq.build_context(
        project_id=project_id, candidates=candidates,
        coverage=dc.coverage(candidates),
        brief={"theme": "", "target_duration_sec": None},
        max_candidates=limit,
    )
    wanted = list(ctx["candidate_segment_ids"])
    if not wanted:
        base_id, absent, _locked = _storyboard_state(session, project_id)
        return PlanningInput(
            project_id=project_id,
            source_input_digest=ctx["input_digest"],
            coverage=ctx["coverage"],
            withheld=dict(ctx.get("withheld") or {}),
            limitations=list(ctx.get("limitations") or []),
            base_storyboard_version_id=base_id,
            storyboard_absent=absent,
            entries=[],
        )

    segments = {
        g.id: g
        for g in session.scalars(
            select(Segment).where(Segment.project_id == project_id,
                                  Segment.id.in_(wanted))
        ).all()
    }
    asset_ids = {g.asset_id for g in segments.values()}
    assets = {
        a.id: a
        for a in session.scalars(
            select(Asset).where(Asset.project_id == project_id, Asset.id.in_(asset_ids))
        ).all()
    }

    static_rows = _load_current(session, SegmentSemanticResult, wanted, project_id)
    motion_rows = _load_current(session, SegmentMotionReview, wanted, project_id)
    video_rows = _load_current(session, SegmentVideoObservation, wanted, project_id)

    # TSB-371-R1 P1-01: the human's per-claim verdicts for every reading that will
    # feed this plan, read in ONE batch (P2-01: no per-shot query).
    from . import evaluation_feedback as fb

    review_verdicts: dict[str, dict[str, str]] = {}
    for rows_by_segment in (static_rows, motion_rows, video_rows):
        for rows in rows_by_segment.values():
            for row in rows:
                if getattr(row, "is_current", False):
                    review_verdicts[row.id] = fb.claim_verdicts_for_result(
                        session, project_id, row.id)
    review_digest = fb.review_signature(
        session, project_id, sorted(review_verdicts))

    from ..models import UserAnnotation

    annotations = {
        a.segment_id: a
        for a in session.scalars(
            select(UserAnnotation).where(UserAnnotation.segment_id.in_(wanted))
        ).all()
    }

    entries: list[PlanningEntry] = []
    for seg_id in wanted:                      # context order is the plan order
        seg = segments.get(seg_id)
        asset = assets.get(seg.asset_id) if seg else None
        if seg is None or asset is None:
            # The context named a shot this project cannot resolve. Refusing to
            # silently drop it keeps the discrepancy visible.
            raise PlanningInputError(
                "EVIDENCE_INCONSISTENT",
                f"context referenced segment {seg_id} that this project cannot resolve",
            )

        limitations: list[str] = []
        static = _static_evidence(static_rows.get(seg_id, []), limitations)
        multiframe = _motion_evidence(motion_rows.get(seg_id, []), limitations)
        video_native = _video_evidence(video_rows.get(seg_id, []), seg, limitations)

        # Withhold only the claims a human marked wrong. The shot keeps its place
        # and its window: a wrong description is not a worthless shot (§2).
        static = _apply_review(static, review_verdicts, limitations)
        multiframe = _apply_review(multiframe, review_verdicts, limitations)
        video_native = _apply_review(video_native, review_verdicts, limitations)

        annotation = annotations.get(seg_id)
        rating, source = dc.normalized_rating(seg, annotation)
        entries.append(
            PlanningEntry(
                segment_id=seg.id,
                asset_id=seg.asset_id,
                asset_display_name=asset.display_name or "",
                source_in_sec=float(seg.start_sec),
                source_out_sec=float(seg.end_sec),
                eligibility=dc.ELIGIBLE,
                human=HumanFacts(
                    rating=rating,
                    rating_source=source,
                    story_role=str(seg.story_role or "")[:64],
                    # A note is user text: stored verbatim (bounded), tagged as
                    # human-supplied so no consumer mistakes it for an instruction.
                    note=str(getattr(annotation, "note", "") or "")[:200],
                ),
                technical=TechnicalFacts(
                    duration_sec=round(max(0.0, seg.end_sec - seg.start_sec), 3),
                    quality_metrics={
                        k: v for k, v in (seg.quality_metrics or {}).items()
                        if k in dc.QUALITY_KEYS
                    },
                    is_trimmed=bool(seg.is_trimmed),
                ),
                static=static,
                multiframe=multiframe,
                video_native=video_native,
                editorial_priority=_priority_for(rating, seg.story_role, static,
                                                 multiframe, video_native),
                limitations=limitations,
            )
        )

    base_id, absent, locked_ids = _storyboard_state(session, project_id)
    for entry in entries:
        if entry.segment_id in locked_ids:
            object.__setattr__(entry, "locked_in_base", True)
            entry.limitations.append("locked_in_base_storyboard_version")

    # Fold the review facts into the digest: a plan built from filtered evidence
    # must never be indistinguishable from one built from untouched evidence.
    digest = ctx["input_digest"]
    if review_digest:
        import hashlib

        digest = hashlib.sha256(f"{digest}|review:{review_digest}".encode("utf-8")
                                ).hexdigest()
    return PlanningInput(
        project_id=project_id,
        source_input_digest=digest,
        coverage=ctx["coverage"],
        withheld=dict(ctx.get("withheld") or {}),
        limitations=list(ctx.get("limitations") or []),
        base_storyboard_version_id=base_id,
        storyboard_absent=absent,
        entries=entries,
    )


def _priority_for(rating: str, story_role: str, static: StaticEvidence,
                  multiframe: MotionEvidence, video: VideoEvidence) -> str:
    if rating in dc.POSITIVE_RATINGS:
        return "preferred"
    if rating in dc.NEGATIVE_RATINGS:
        return "excluded"
    if story_role:
        return "noted"
    if any(ch.state == dc.STATE_CURRENT for ch in (static, multiframe, video)):
        return "noted"
    return "unrated"
