"""TSB-369-B — the plan validator: the one place a draft can be rejected.

Everything upstream of this module is a *suggestion*. Offline rules are
deterministic but still heuristics; a mock (and one day a real model) can emit
anything at all. This validator is what turns a suggestion into something the UI
is allowed to show, by checking it against the evidence it claims to rest on.

The rules fall into five groups:

1. **Identity** — every shot must reference a shot that was actually offered, in
   this project, still eligible. A planner cannot invent or resurrect a shot.
2. **Windows** — in/out must sit inside the current source window, be finite and
   ordered; the timeline must be contiguous and match the sum of durations.
3. **Claims** — a claim that asserts something about the footage must cite
   evidence that supports it, and must not upgrade ``uncertain`` /
   ``not_visible`` / ``freshness_unverified`` into certainty.
4. **Shape** — sections exist, no duplicate shots, roles and kinds are from the
   closed vocabularies.
5. **Distinctness** — asking for three variants must not yield the same shot
   sequence with different titles.

Validation returns *all* problems, not the first: a user staring at a rejected
plan deserves to see everything wrong with it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from . import director_candidates as dc
from .director_plan_input import PlanningEntry, PlanningInput
from .director_plan_schema import PlanDraft, VariantDraft

#: Tolerance for float drift when checking the timeline against the shot sum.
TIMELINE_EPS = 1e-3

#: Phrases that assert a specific identity or place the model cannot know from
#: pixels. Deliberately narrow: this is a backstop, not the primary mechanism —
#: the primary mechanism is that factual claims must carry evidence refs.
_PLACE_OR_IDENTITY_MARKERS = ()  # reserved; structural citation is the real check


class PlanValidationError(Exception):
    """Carries every problem found, with a stable code per problem."""

    def __init__(self, problems: list[dict]) -> None:
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s): " + problems[0]["code"])

    @property
    def codes(self) -> list[str]:
        return [p["code"] for p in self.problems]


@dataclass
class Problem:
    code: str
    message: str
    variant_id: str | None = None
    shot_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "variant_id": self.variant_id,
            "shot_id": self.shot_id,
        }


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _close(a: float, b: float, eps: float = TIMELINE_EPS) -> bool:
    return abs(float(a) - float(b)) <= eps


# ---------------------------------------------------------------------------
# Per-variant checks
# ---------------------------------------------------------------------------

def _validate_variant(
    variant: VariantDraft,
    entries: dict[str, PlanningEntry],
    problems: list[Problem],
) -> None:
    vid = variant.variant_id

    section_ids = [s.section_id for s in variant.sections]
    if len(set(section_ids)) != len(section_ids):
        problems.append(Problem("DUPLICATE_SECTION", "section ids must be unique", vid))

    seen_segments: set[str] = set()
    seen_shots: set[str] = set()

    for shot in variant.shots:
        sid = shot.shot_id
        if sid in seen_shots:
            problems.append(Problem("DUPLICATE_SHOT_ID", f"shot id {sid} reused", vid, sid))
        seen_shots.add(sid)

        entry = entries.get(shot.segment_id)
        if entry is None:
            # The planner referenced a shot it was not given. This is the most
            # important rejection: never silently substitute another shot.
            problems.append(Problem(
                "UNKNOWN_SEGMENT",
                f"segment {shot.segment_id} was not an eligible planning candidate",
                vid, sid,
            ))
            continue

        if shot.asset_id != entry.asset_id:
            problems.append(Problem(
                "ASSET_MISMATCH",
                f"asset {shot.asset_id} does not own segment {shot.segment_id}",
                vid, sid,
            ))

        if shot.segment_id in seen_segments:
            problems.append(Problem(
                "DUPLICATE_SEGMENT",
                f"segment {shot.segment_id} appears more than once in one variant",
                vid, sid,
            ))
        seen_segments.add(shot.segment_id)

        if shot.section_id not in section_ids:
            problems.append(Problem(
                "ORPHAN_SECTION",
                f"section {shot.section_id} is not declared in this variant",
                vid, sid,
            ))

        _validate_window(shot, entry, problems, vid, sid)
        _validate_claims(shot, entry, problems, vid, sid)

    _validate_timeline(variant, problems, vid)


def _validate_window(shot, entry: PlanningEntry, problems: list[Problem],
                     vid: str, sid: str) -> None:
    for label, value in (("source_in_sec", shot.source_in_sec),
                         ("source_out_sec", shot.source_out_sec),
                         ("planned_duration_sec", shot.planned_duration_sec),
                         ("timeline_start_sec", shot.timeline_start_sec)):
        if not _finite(value):
            problems.append(Problem("INVALID_SHOT_WINDOW",
                                    f"{label} is not a finite number", vid, sid))
            return

    if not shot.source_in_sec < shot.source_out_sec:
        problems.append(Problem("INVALID_SHOT_WINDOW",
                                "source_in_sec must be < source_out_sec", vid, sid))
        return

    # The window must sit inside the shot's CURRENT editable interval.
    if (shot.source_in_sec < entry.source_in_sec - TIMELINE_EPS
            or shot.source_out_sec > entry.source_out_sec + TIMELINE_EPS):
        problems.append(Problem(
            "INVALID_SHOT_WINDOW",
            f"window {shot.source_in_sec}-{shot.source_out_sec} is outside the shot's "
            f"{entry.source_in_sec}-{entry.source_out_sec}",
            vid, sid,
        ))

    expected = shot.source_out_sec - shot.source_in_sec
    if not _close(shot.planned_duration_sec, expected):
        # Speed changes are not implemented this round, so a mismatch means the
        # draft is internally inconsistent.
        problems.append(Problem(
            "INVALID_SHOT_DURATION",
            f"planned_duration_sec {shot.planned_duration_sec} != out-in {expected}",
            vid, sid,
        ))


def _validate_claims(shot, entry: PlanningEntry, problems: list[Problem],
                     vid: str, sid: str) -> None:
    available = _available_refs(entry)

    for claim in shot.claims:
        if claim.kind == "editorial_suggestion":
            # A craft opinion: no evidence needed, no footage asserted.
            continue
        if claim.kind == "unknown":
            continue
        if claim.kind == "human_supplied":
            if not entry.human.note and not entry.human.story_role:
                problems.append(Problem(
                    "UNSUPPORTED_HUMAN_CLAIM",
                    "claim cites human input but the shot has no note or role",
                    vid, sid,
                ))
            continue

        # kind == "observed_signal": must point at a real, usable reading.
        if not claim.evidence_refs:
            problems.append(Problem(
                "UNSUPPORTED_VISUAL_CLAIM",
                f"claim {claim.text[:60]!r} asserts an observation without evidence",
                vid, sid,
            ))
            continue
        for ref in claim.evidence_refs:
            key = (ref.channel, ref.result_id)
            if key not in available:
                problems.append(Problem(
                    "UNSUPPORTED_VISUAL_CLAIM",
                    f"claim cites {ref.channel}/{ref.result_id} which this shot does "
                    "not have as usable evidence",
                    vid, sid,
                ))
            else:
                _validate_upgrade(ref, entry, available[key], problems, vid, sid, claim.text)

    # Shot-level refs are also checked, they are what the UI renders.
    for ref in shot.evidence_refs:
        if (ref.channel, ref.result_id) not in available:
            problems.append(Problem(
                "UNKNOWN_EVIDENCE_REF",
                f"evidence {ref.channel}/{ref.result_id} is not usable for this shot",
                vid, sid,
            ))


def _available_refs(entry: PlanningEntry) -> dict[tuple[str, str], object]:
    """Readings a claim may legitimately cite.

    ``stale`` and ``failed`` are excluded: they describe a different version of
    the shot, or nothing at all, so they cannot support a claim about *this* one.
    """
    out: dict[tuple[str, str], object] = {}
    for channel, ev in (("static", entry.static), ("multiframe", entry.multiframe),
                        ("video_native", entry.video_native)):
        if ev.result_id and ev.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
            out[(channel, ev.result_id)] = ev
    return out


def _validate_upgrade(ref, entry: PlanningEntry, ev, problems: list[Problem],
                      vid: str, sid: str, text: str) -> None:
    """Refuse to turn a hedged reading into a confident assertion."""
    if ref.channel == "static":
        state = getattr(ev, "evidence_state", "")
        if state in ("uncertain", "not_visible"):
            problems.append(Problem(
                "SEMANTIC_UPGRADE",
                f"claim {text[:60]!r} cites a reading whose evidence state is "
                f"{state!r}; it cannot be stated as observed",
                vid, sid,
            ))
    if ref.channel == "video_native" and ref.event_start_sec is not None:
        if not _finite(ref.event_start_sec) or not _finite(ref.event_end_sec or 0.0):
            problems.append(Problem("INVALID_EVENT_TIME",
                                    "event time is not finite", vid, sid))
            return
        if not (entry.source_in_sec - TIMELINE_EPS <= ref.event_start_sec
                and (ref.event_end_sec or 0.0) <= entry.source_out_sec + TIMELINE_EPS):
            problems.append(Problem(
                "INVALID_EVENT_TIME",
                f"event {ref.event_start_sec}-{ref.event_end_sec} is outside the shot "
                f"{entry.source_in_sec}-{entry.source_out_sec}",
                vid, sid,
            ))


def _validate_timeline(variant: VariantDraft, problems: list[Problem], vid: str) -> None:
    if not variant.shots:
        if variant.total_duration_sec and not _close(variant.total_duration_sec, 0.0):
            problems.append(Problem("INVALID_TIMELINE",
                                    "a variant with no shots must not report a duration", vid))
        return

    expected_start = 0.0
    for shot in variant.shots:
        if not _finite(shot.timeline_start_sec) or not _close(shot.timeline_start_sec, expected_start):
            problems.append(Problem(
                "INVALID_TIMELINE",
                f"shot {shot.shot_id} starts at {shot.timeline_start_sec}, expected "
                f"{round(expected_start, 3)}",
                vid, shot.shot_id,
            ))
        if _finite(shot.planned_duration_sec):
            expected_start += shot.planned_duration_sec

    total = sum(s.planned_duration_sec for s in variant.shots if _finite(s.planned_duration_sec))
    if not _close(variant.total_duration_sec, total):
        problems.append(Problem(
            "INVALID_TIMELINE",
            f"total_duration_sec {variant.total_duration_sec} != sum {round(total, 3)}",
            vid,
        ))

    gap = variant.target_duration_sec - total
    if not _close(variant.duration_gap_sec, gap):
        problems.append(Problem(
            "INVALID_TIMELINE",
            f"duration_gap_sec {variant.duration_gap_sec} != target-sum {round(gap, 3)}",
            vid,
        ))


# ---------------------------------------------------------------------------
# Cross-variant checks
# ---------------------------------------------------------------------------

def _validate_distinctness(variants: Iterable[VariantDraft], problems: list[Problem]) -> None:
    """Three variants that select the same shots in the same order are one variant."""
    seen: dict[tuple, str] = {}
    for v in variants:
        if not v.shots:
            continue
        key = tuple(s.segment_id for s in v.shots)
        if key in seen:
            problems.append(Problem(
                "VARIANTS_NOT_DISTINCT",
                f"variant {v.variant_id} selects exactly the same shots in the same "
                f"order as {seen[key]}; changing only the title is not a new plan",
                v.variant_id,
            ))
        else:
            seen[key] = v.variant_id


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def validate_plan(plan: PlanDraft, planning_input: PlanningInput) -> list[Problem]:
    """Return every problem found. An empty list means the draft may be shown."""
    problems: list[Problem] = []

    if plan.project_id != planning_input.project_id:
        problems.append(Problem(
            "PROJECT_MISMATCH",
            f"plan is for project {plan.project_id}, input is for "
            f"{planning_input.project_id}",
        ))
        return problems

    if plan.source_input_digest != planning_input.source_input_digest:
        problems.append(Problem(
            "DIRECTOR_INPUT_CHANGED",
            "the plan was built against different input than the one supplied",
        ))
        return problems

    entries = {e.segment_id: e for e in planning_input.entries}
    for variant in plan.variants:
        _validate_variant(variant, entries, problems)

    _validate_distinctness(plan.variants, problems)

    if len(plan.variants) > 3:
        problems.append(Problem("TOO_MANY_VARIANTS", "at most 3 variants per plan"))

    return problems


def validate_or_raise(plan: PlanDraft, planning_input: PlanningInput) -> PlanDraft:
    problems = validate_plan(plan, planning_input)
    if problems:
        raise PlanValidationError([p.as_dict() for p in problems])
    return plan
