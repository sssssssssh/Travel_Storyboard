"""TSB-369-B — deterministic, explainable offline story planning.

This is **not** an AI director. It is a rule-driven structural planner: it puts
the shots the project actually has into a readable order, proposes sections and
durations, and states why each shot was chosen — using only the evidence that
exists. It never invents what was filmed.

Why rule-based first: the alternative (asking a language model) requires an
external call that is not authorized, cannot be tested deterministically, and
would let prose stand in for evidence. The rules here produce something honest
and testable; a model can be added later behind the same validation gate.

Three strategies, each genuinely different rather than re-labelled:

* ``chronological``   — follows the library's own order (asset, then time).
* ``highlights_first``— puts human-marked and evidenced shots up front.
* ``atmospheric``     — prefers shots with a usable reading and no hard action,
                        giving longer holds; falls back honestly when nothing
                        supports the distinction.

Determinism: the same input always yields the same plan. Every sort has an
explicit final tie-break on ``segment_id``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import director_candidates as dc
from .director_plan_input import PlanningEntry, PlanningInput
from .director_plan_schema import (
    CoverageSummary,
    EvidenceRef,
    Claim,
    NarrativeAngle,
    PlanDraft,
    PlanningBrief,
    ReviewItem,
    SectionDraft,
    ShotDraft,
    VariantDraft,
)

#: A shot needs at least this much usable time to be worth placing.
MIN_SHOT_SEC = 0.5

#: When the library is thinner than this, the plan is provisional rather than
#: pretending to be a finished edit.
COMPLETE_ENOUGH_READINGS = 3

_ROLE_BY_POSITION = ("opening", "development", "turning_point", "climax", "closing")


@dataclass
class _Picked:
    entry: PlanningEntry
    in_sec: float
    out_sec: float
    role: str
    reason: str
    claims: list[Claim]
    refs: list[EvidenceRef]
    needs_review: list[str]


def _usable(entry: PlanningEntry) -> bool:
    return (entry.source_out_sec - entry.source_in_sec) >= MIN_SHOT_SEC


def _timeline_key(entry: PlanningEntry) -> tuple:
    return (entry.asset_id, entry.source_in_sec, entry.segment_id)


def _evidence_refs(entry: PlanningEntry) -> list[EvidenceRef]:
    """References for the readings this shot actually has, in channel order."""
    refs: list[EvidenceRef] = []
    if entry.static.result_id and entry.static.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
        refs.append(EvidenceRef(
            channel="static", result_id=entry.static.result_id,
            segment_id=entry.segment_id, evidence_state=entry.static.evidence_state,
        ))
    if entry.multiframe.result_id and entry.multiframe.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
        refs.append(EvidenceRef(
            channel="multiframe", result_id=entry.multiframe.result_id,
            segment_id=entry.segment_id,
        ))
    if entry.video_native.result_id and entry.video_native.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
        refs.append(EvidenceRef(
            channel="video_native", result_id=entry.video_native.result_id,
            segment_id=entry.segment_id,
        ))
    return refs


def _claims_for(entry: PlanningEntry, refs: list[EvidenceRef]) -> list[Claim]:
    """Only claims the evidence can carry — and never an upgrade.

    A still reading yields a still claim; a motion reading yields a motion claim
    *worded as a model observation with its uncertainty intact*; a video event
    yields a timed claim. A shot with no reading yields no factual claim at all,
    which is the whole point.
    """
    claims: list[Claim] = []
    by_channel = {r.channel: r for r in refs}

    if "static" in by_channel and entry.static.evidence_state not in ("uncertain", "not_visible"):
        tags = entry.static.visual.get("scene_tags") or []
        subjects = entry.static.visual.get("subjects") or []
        if tags or subjects:
            bits = []
            if tags:
                bits.append("场景标签 " + "/".join(tags[:3]))
            if subjects:
                bits.append("可见主体 " + "、".join(subjects[:2]))
            claims.append(Claim(
                text="静帧观察：" + "；".join(bits),
                kind="observed_signal",
                evidence_refs=[by_channel["static"]],
            ))

    if "multiframe" in by_channel and entry.multiframe.action_summary:
        # Worded as what the model reported, not as an established fact.
        claims.append(Claim(
            text=f"多帧模型报告：{entry.multiframe.action_summary[:160]}",
            kind="observed_signal",
            evidence_refs=[by_channel["multiframe"]],
        ))

    if "video_native" in by_channel:
        for ev in entry.video_native.events[:3]:
            claims.append(Claim(
                text=f"视频事件（源片 {ev.start_sec:.1f}–{ev.end_sec:.1f}s）：{ev.description[:120]}",
                kind="observed_signal",
                evidence_refs=[EvidenceRef(
                    channel="video_native", result_id=entry.video_native.result_id or "",
                    segment_id=entry.segment_id,
                    event_start_sec=ev.start_sec, event_end_sec=ev.end_sec,
                )],
            ))

    if entry.human.note:
        claims.append(Claim(
            text=f"你的备注：{entry.human.note[:160]}",
            kind="human_supplied",
        ))

    return claims


def _needs_review(entry: PlanningEntry) -> list[str]:
    out: list[str] = []
    if entry.static.state == dc.STATE_UNVERIFIED:
        out.append("静帧读数新鲜度未验证")
    if entry.multiframe.state == dc.STATE_UNVERIFIED:
        out.append("多帧结论输入签名未验证")
    if entry.video_native.state == dc.STATE_UNVERIFIED:
        out.append("原生视频读数新鲜度未验证")
    if not entry.video_native.audio_analyzed:
        out.append("未分析音频")
    if not entry.has_usable_reading:
        out.append("该镜头没有任何模型读数，仅按结构位置建议使用")
    return out


def _selection_reason(entry: PlanningEntry) -> str:
    bits: list[str] = []
    if entry.human.rating in dc.POSITIVE_RATINGS:
        bits.append(f"人工标记为 {entry.human.rating}")
    if entry.human.story_role:
        bits.append(f"人工设定故事角色「{entry.human.story_role}」")
    if entry.human.note:
        bits.append("包含你的备注")
    if entry.static.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED) and entry.static.result_id:
        tags = entry.static.visual.get("scene_tags") or []
        bits.append("有静帧观察" + (f"（{tags[0]}）" if tags else ""))
    if entry.multiframe.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED) and entry.multiframe.result_id:
        bits.append(f"有多帧运动结论（{entry.multiframe.motion_type or '类型未知'}）")
    if entry.video_native.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED) and entry.video_native.result_id:
        bits.append(f"有原生视频观察（{len(entry.video_native.events)} 个时序事件）")
    if not bits:
        bits.append("无内容证据，仅按时间线结构建议保留")
    return "；".join(bits)[:300]


# ---------------------------------------------------------------------------
# Strategy ordering
# ---------------------------------------------------------------------------

def _order_entries(entries: list[PlanningEntry], angle: NarrativeAngle) -> list[PlanningEntry]:
    usable = [e for e in entries if _usable(e)]

    if angle == "chronological":
        return sorted(usable, key=_timeline_key)

    if angle == "highlights_first":
        def key(e: PlanningEntry) -> tuple:
            favoured = 0 if e.human.rating in dc.POSITIVE_RATINGS else 1
            role = 0 if e.human.story_role else 1
            evidenced = 0 if e.has_usable_reading else 1
            return (favoured, role, evidenced) + _timeline_key(e)

        return sorted(usable, key=key)

    # atmospheric: prefer shots with a reading and no hard action; keep the
    # reading-bearing shots from being scattered by asset.
    def atmo_key(e: PlanningEntry) -> tuple:
        has_static_mood = 0 if (e.static.visual.get("mood") or []) else 1
        no_action = 0 if not (e.static.visual.get("visible_actions") or []) else 1
        evidenced = 0 if e.has_usable_reading else 1
        return (evidenced, has_static_mood, no_action) + _timeline_key(e)

    return sorted(usable, key=atmo_key)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _distribute(ordered: list[PlanningEntry], target_duration: float) -> list[_Picked]:
    """Assign each shot a usable window and a role, filling toward the target.

    Shots are taken in strategy order; each contributes as much of its own
    window as the remaining target budget allows. A shot is never repeated to
    reach the target, and a shot that contributes nothing is not included.
    """
    if not ordered:
        return []

    picked: list[_Picked] = []
    remaining = float(target_duration)
    for entry in ordered:
        if remaining <= MIN_SHOT_SEC:
            break
        available = entry.source_out_sec - entry.source_in_sec
        if available < MIN_SHOT_SEC:
            continue
        take = min(available, remaining)
        # Keep sub-second precision but do not emit absurdly short slivers.
        if take < MIN_SHOT_SEC:
            break
        in_sec = round(entry.source_in_sec, 3)
        out_sec = round(entry.source_in_sec + take, 3)
        remaining = round(remaining - (out_sec - in_sec), 3)

        refs = _evidence_refs(entry)
        picked.append(_Picked(
            entry=entry,
            in_sec=in_sec,
            out_sec=out_sec,
            role="",                       # assigned once the count is known
            reason=_selection_reason(entry),
            claims=_claims_for(entry, refs),
            refs=refs,
            needs_review=_needs_review(entry),
        ))
    return picked


def _assign_roles(picked: list[_Picked]) -> None:
    n = len(picked)
    if n == 0:
        return
    if n == 1:
        picked[0].role = "opening"
        return
    if n == 2:
        picked[0].role, picked[1].role = "opening", "closing"
        return
    # First is the opening, last the closing; the middle spans the arc.
    picked[0].role = "opening"
    picked[-1].role = "closing"
    middle = picked[1:-1]
    for i, p in enumerate(middle):
        if len(middle) == 1:
            p.role = "development"
        elif i == len(middle) - 1:
            p.role = "climax"
        elif i == 0:
            p.role = "development"
        else:
            p.role = "turning_point"


def _sections_for(picked: list[_Picked], angle: NarrativeAngle) -> list[SectionDraft]:
    roles = {p.role for p in picked}
    sections = []
    if "opening" in roles:
        sections.append(SectionDraft(section_id="sec-opening", kind="opening",
                                     title="开场", narrative_role="opening"))
    if roles & {"development", "turning_point", "climax"}:
        sections.append(SectionDraft(section_id="sec-body", kind="body",
                                     title="展开", narrative_role="development"))
    if "closing" in roles:
        sections.append(SectionDraft(section_id="sec-closing", kind="closing",
                                     title="收束", narrative_role="closing"))
    if not sections:
        sections.append(SectionDraft(section_id="sec-body", kind="body", title="主体"))
    return sections


def _section_for_role(role: str) -> str:
    if role == "opening":
        return "sec-opening"
    if role == "closing":
        return "sec-closing"
    return "sec-body"


_TITLE = {
    "chronological": "以旅程推进为线索",
    "highlights_first": "先亮出重点，再补齐行程",
    "atmospheric": "以氛围与留白为线索",
}

_LOGLINE = {
    "chronological": "按你素材库的既有顺序串起一段行程结构；不声称已观察到每个地点。",
    "highlights_first": "先呈现你标记过或有观察证据的镜头，再补充其余行程结构。",
    "atmospheric": "优先使用有观察依据、动作较少的镜头，用较长停留换取氛围感。",
}

_DIFFERENCES = {
    "chronological": ["按素材与时间顺序排列", "开场取时间线最早的镜头"],
    "highlights_first": ["人工优先级高的镜头前置", "检验性证据优先于纯时间顺序"],
    "atmospheric": ["优先有场景/情绪标签且动作少的镜头", "单镜头停留更接近完整区间"],
}


def _build_variant(
    variant_id: str, angle: NarrativeAngle, picked: list[_Picked],
    target_duration: float, status: str,
) -> VariantDraft:
    sections = _sections_for(picked, angle)
    timeline = 0.0
    shots: list[ShotDraft] = []
    for i, p in enumerate(picked):
        duration = round(p.out_sec - p.in_sec, 3)
        shots.append(ShotDraft(
            shot_id=f"{variant_id}-s{i + 1:02d}",
            segment_id=p.entry.segment_id,
            asset_id=p.entry.asset_id,
            section_id=_section_for_role(p.role),
            source_in_sec=p.in_sec,
            source_out_sec=p.out_sec,
            planned_duration_sec=duration,
            timeline_start_sec=round(timeline, 3),
            narrative_role=p.role,  # type: ignore[arg-type]
            selection_reason=p.reason,
            claims=p.claims,
            evidence_refs=p.refs,
            needs_review=p.needs_review,
            transition_hint=_transition(i, len(picked), angle),
            pacing_hint=_pacing(p, angle),
            audio_hint="",
        ))
        timeline = round(timeline + duration, 3)

    total = round(timeline, 3)
    review = [
        ReviewItem(segment_id=p.entry.segment_id, reason=p.needs_review[0])
        for p in picked if p.needs_review
    ]

    return VariantDraft(
        variant_id=variant_id,
        title=_TITLE[angle],
        narrative_angle=angle,
        logline=_LOGLINE[angle],
        status=status,  # type: ignore[arg-type]
        sections=sections,
        shots=shots,
        total_duration_sec=total,
        target_duration_sec=target_duration,
        duration_gap_sec=round(target_duration - total, 3),
        review_items=review,
        differences=list(_DIFFERENCES[angle]),
    )


def _transition(index: int, total: int, angle: NarrativeAngle) -> str:
    if total <= 1:
        return ""
    if index == 0:
        return "建议直接切入"
    if index == total - 1:
        return "建议以留白收束"
    if angle == "atmospheric":
        return "建议叠化过渡"
    return "建议硬切"


def _pacing(p: _Picked, angle: NarrativeAngle) -> str:
    duration = p.out_sec - p.in_sec
    if angle == "atmospheric":
        return "建议放慢节奏，保留完整动作"
    if duration >= 5.0:
        return "建议保留完整时长"
    if duration <= 1.5:
        return "建议快切"
    return "建议中等节奏"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _coverage_summary(planning_input: PlanningInput) -> CoverageSummary:
    cov = planning_input.coverage or {}
    return CoverageSummary(
        library_segments=int(cov.get("library_segments") or 0),
        segments_with_any_valid_reading=int(cov.get("segments_with_any_valid_reading") or 0),
        no_valid_model_reading=int(cov.get("no_valid_model_reading") or 0),
        video_native_valid=int(cov.get("video_native_valid") or 0),
    )


def plan_offline(planning_input: PlanningInput, brief: PlanningBrief) -> PlanDraft:
    """Produce a draft plan from the deterministic rules.

    Deterministic: identical input and brief always produce an identical plan.
    """
    entries = list(planning_input.entries)
    coverage = _coverage_summary(planning_input)

    limitations = list(planning_input.limitations)
    limitations.append("offline_rules_structural_only")

    usable = [e for e in entries if _usable(e)]
    if not usable:
        return PlanDraft(
            project_id=planning_input.project_id,
            planning_mode="offline_rules",
            generation_status="insufficient_evidence",
            source_input_digest=planning_input.source_input_digest,
            base_storyboard_version_id=planning_input.base_storyboard_version_id,
            coverage=coverage,
            limitations=limitations + ["no_usable_shots"],
            variants=[],
            withheld=planning_input.withheld,
            needs_more_analysis=True,
            warnings=["该项目的合格镜头不足以形成任何剪辑结构。"],
        )

    with_reading = sum(1 for e in usable if e.has_usable_reading)
    status = "complete" if with_reading >= COMPLETE_ENOUGH_READINGS else "provisional"
    if coverage.video_native_valid == 0:
        limitations.append("no_video_native_evidence")
    if coverage.no_valid_model_reading:
        limitations.append("partial_analysis_coverage")

    # The user's chosen structure leads; the remaining slots explore the others
    # so the comparison is still available. Asking for 1 variant therefore gives
    # exactly the structure that was requested.
    _ALL_ANGLES: tuple[NarrativeAngle, ...] = ("chronological", "highlights_first",
                                               "atmospheric")
    ordered_angles: list[NarrativeAngle] = [brief.style]
    for candidate in _ALL_ANGLES:
        if len(ordered_angles) >= brief.variant_count:
            break
        if candidate != brief.style:
            ordered_angles.append(candidate)
    angles = ordered_angles

    variants: list[VariantDraft] = []
    seen_sequences: set[tuple] = set()
    for idx, angle in enumerate(angles):
        ordered = _order_entries(entries, angle)
        picked = _distribute(ordered, brief.target_duration_sec)
        _assign_roles(picked)
        variant = _build_variant(f"v{idx + 1}", angle, picked, brief.target_duration_sec, status)
        sequence = tuple(s.segment_id for s in variant.shots)
        if sequence and sequence in seen_sequences:
            # The library is too small for this strategy to differ. Dropping it is
            # more honest than showing three identical plans with new titles.
            continue
        seen_sequences.add(sequence)
        variants.append(variant)

    warnings: list[str] = []
    if len(variants) < len(angles):
        warnings.append(
            "素材量不足，部分策略无法给出与其他方案真正不同的排列，已省略。"
        )
    if with_reading == 0:
        warnings.append("候选中没有任何模型读数，方案仅为结构建议。")

    return PlanDraft(
        project_id=planning_input.project_id,
        planning_mode="offline_rules",
        generation_status=status,  # type: ignore[arg-type]
        source_input_digest=planning_input.source_input_digest,
        base_storyboard_version_id=planning_input.base_storyboard_version_id,
        coverage=coverage,
        limitations=limitations,
        variants=variants,
        withheld=planning_input.withheld,
        needs_more_analysis=bool(coverage.no_valid_model_reading),
        warnings=warnings,
    )
