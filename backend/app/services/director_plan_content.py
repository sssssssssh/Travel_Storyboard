"""Server-authored text and claims for the grounded model plan contract."""
from __future__ import annotations

from . import director_candidates as dc
from .director_plan_input import PlanningEntry
from .director_plan_schema import Claim, EvidenceRef

REASONS = {
    "opening": "建议用此镜头开场。",
    "development": "建议用此镜头推进段落。",
    "turning_point": "建议用此镜头承担叙事转折。",
    "climax": "建议用此镜头构成高潮。",
    "closing": "建议用此镜头收束。",
}
TRANSITIONS = {"cut": "建议直接切换。", "dissolve": "建议尝试叠化。",
               "match_cut": "建议检查能否匹配剪辑。"}
PACING = {"quick": "建议加快节奏。", "hold": "建议保留停顿。",
          "slow_motion": "建议试看片段慢放效果。"}
SECTION_TITLES = {"opening": "开场", "body": "发展", "closing": "收束", "custom": "段落"}
ANGLES = {"chronological": "按时间推进", "highlights_first": "精彩片段先行", "atmospheric": "以氛围串联"}


def variant_title(variant_id: str) -> str:
    return f"剪辑方案 {variant_id[1:]}"


def claim_catalog(entry: PlanningEntry) -> dict[str, Claim]:
    """Values come from the filtered evidence and human inputs in PlanningInput."""
    claims: dict[str, Claim] = {
        "unknown": Claim(kind="unknown", text="画面内容待人工复核。"),
    }

    def observed(channel, evidence, key, value, *, event=None):
        if (not isinstance(value, str) or not value.strip() or not evidence.result_id
                or evidence.state not in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED)
                or "human_review_claims_uncertain" in evidence.limitations):
            return
        ref = EvidenceRef(channel=channel, result_id=evidence.result_id,
                          segment_id=entry.segment_id,
                          event_start_sec=event.start_sec if event is not None else None,
                          event_end_sec=event.end_sec if event is not None else None)
        claims[key] = Claim(kind="observed_signal", text=value.strip()[:300], evidence_refs=[ref])

    static = entry.static
    if static.evidence_state not in ("uncertain", "not_visible"):
        for field in ("scene_tags", "subjects", "visible_actions"):
            for index, value in enumerate(static.visual.get(field) or []):
                observed("static", static, f"{field}:{index}", value)
    observed("multiframe", entry.multiframe, "motion", entry.multiframe.action_summary)
    observed("video_native", entry.video_native, "video_summary", entry.video_native.summary)
    for index, event in enumerate(entry.video_native.events):
        observed("video_native", entry.video_native, f"event:{index}", event.description, event=event)

    for field, label in (("note", "人工备注"), ("story_role", "人工用途")):
        value = getattr(entry.human, field)
        if value.strip():
            claims[f"human:{field}"] = Claim(kind="human_supplied", text=f"{label}：{value}"[:300])
    for code, text in (TRANSITIONS | PACING).items():
        claims[f"edit:{code}"] = Claim(kind="editorial_suggestion", text=text)
    return claims
