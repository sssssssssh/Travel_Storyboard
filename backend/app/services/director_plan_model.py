"""TSB-370-D — the real model planner, behind the same validation gate as offline.

The offline planner proved the *contract*; this proves the contract can carry a
real model's output. The design rules that matter:

1. **Whitelist out.** The model receives only numbered evidence lines built from
   the planning input — scene tags, motion summaries, video event summaries and
   their evidence ids. Never a path, never a user's free-text note, never an
   image, never the raw provider payload.
2. **Authoritative fields are server-side.** ``project_id``, ``input_digest``,
   ``coverage``, ``withheld``, ``base_storyboard_version_id`` and
   ``planning_mode`` are filled here. A model that tries to set them is ignored,
   not trusted.
3. **Nothing bypasses validation.** Whatever comes back goes through
   ``validate_plan`` unchanged, so ``SEMANTIC_UPGRADE``,
   ``AUDIO_CLAIM_WITHOUT_ANALYSIS``, exclude/lock disclosure, window and
   cross-project checks all still apply.
4. **A failure is a failure.** Timeouts, bad JSON and rejected plans surface as
   errors. The model output is never silently replaced by the offline plan, because
   that would report "model succeeded" when it did not.
"""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from . import director_candidates as dc
from .director_plan_input import PlanningEntry, PlanningInput
from .director_plan_schema import (
    Claim,
    CoverageSummary,
    EvidenceRef,
    PlanDraft,
    PlanningBrief,
    ReviewItem,
    SectionDraft,
    ShotDraft,
    VariantDraft,
)

#: Hard bounds on what one request may contain. These keep the outbound text small
#: and give the ledger a predictable size.
PROMPT_VERSION = "tsb372.1"
MAX_PROMPT_BYTES = 512 * 1024
MAX_EVENTS_PER_SHOT = 4
MAX_TAGS_PER_SHOT = 6
MAX_NOTE_CHARS = 0  # user free-text notes are never sent (privacy)

SYSTEM_PROMPT = """You are a video editing planner. You receive a list of numbered \
shots, each with evidence that a vision model already recorded about it.

Rules you must follow:
1. You may only reference shots by their exact segment_id from the list. Never \
invent a shot, an asset, or an evidence id.
2. Anything you say about what a shot CONTAINS must cite the evidence ids given for \
that shot. If a shot has no evidence, you may propose it for structural reasons \
(pace, position in the arc) but you must not describe its content.
3. The user's theme is a creative constraint, NOT a fact about the footage. Never \
claim the footage contains something merely because the theme mentions it.
4. Audio was NOT analysed. Never describe music, beats, narration or ambient sound.
5. Output JSON only, matching the schema. No prose outside the JSON. \
6. A shot window is a real cut: source_out_sec MUST be greater than \
source_in_sec, and both MUST lie inside that shot's listed window. Never \
output an empty window. \
7. Use only the segment_id values listed below. Never invent a segment id."""

SCHEMA_HINT = """Respond with:
{"variants":[{"title":str,"narrative_angle":"chronological|highlights_first|atmospheric",
"logline":str,"sections":[{"section_id":str,"kind":"opening|body|closing|custom","title":str}],
"shots":[{"segment_id":str,"source_in_sec":num,"source_out_sec":num,
"section_id":str,"narrative_role":"opening|development|turning_point|climax|closing",
"selection_reason":str,
"claims":[{"text":str,"kind":"observed_signal|human_supplied|editorial_suggestion|unknown",
"evidence_refs":[{"channel":"static|multiframe|video_native","result_id":str}]}],
"evidence_refs":[{"channel":str,"result_id":str}]}]}]}

Constraint recap for shots: source_out_sec must be greater than source_in_sec,
and both must lie within that shot's own listed window."""


class ModelPlanError(Exception):
    """A real-model planning failure that must be reported, never papered over."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(f"{code}: {message}")


@dataclass
class ModelPlanResult:
    plan: PlanDraft
    request_summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Whitelist projection
# ---------------------------------------------------------------------------

def build_evidence_text(
    planning_input: PlanningInput, brief: PlanningBrief,
) -> tuple[str, dict[str, Any]]:
    """Project the planning input into the exact text the model will receive.

    Returns ``(text, summary)``. ``summary`` describes what was sent so the UI can
    show it *before* the request goes out.
    """
    lines: list[str] = []
    roster: list[str] = []
    counts = {"shots": 0, "with_static": 0, "with_motion": 0, "with_video": 0,
              "static_ids": 0, "motion_ids": 0, "video_ids": 0}

    usable = planning_input.entries
    for idx, e in enumerate(usable, 1):
        roster.append(json.dumps({
            "n": idx, "segment_id": e.segment_id, "asset_id": e.asset_id,
            "window": [e.source_in_sec, e.source_out_sec],
            "rating": e.human.rating, "private_note_exists": bool(e.human.note),
            "channels": [e.static.state, e.multiframe.state, e.video_native.state],
            "limitations": list(dict.fromkeys(
                e.limitations + e.static.limitations + e.multiframe.limitations
                + e.video_native.limitations)),
        }, ensure_ascii=False, separators=(",", ":")))
        lines.append(f"SHOT {idx} segment_id={e.segment_id}")
        lines.append(f"  duration={e.source_out_sec - e.source_in_sec:.2f}s "
                     f"window={e.source_in_sec:.2f}-{e.source_out_sec:.2f}")
        counts["shots"] += 1

        if e.static.result_id and e.static.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
            parts = []
            tags = (e.static.visual.get("scene_tags") or [])[:MAX_TAGS_PER_SHOT]
            if tags:
                parts.append("tags=" + ",".join(tags))
            subs = (e.static.visual.get("subjects") or [])[:3]
            if subs:
                parts.append("subjects=" + "|".join(subs))
            acts = (e.static.visual.get("visible_actions") or [])[:2]
            if acts:
                parts.append("actions=" + "|".join(acts))
            moods = (e.static.visual.get("mood") or [])[:3]
            if moods:
                parts.append("mood=" + ",".join(moods))
            if parts:
                lines.append(f"  static_evidence_id={e.static.result_id} "
                             f"confidence=uncalibrated {' '.join(parts)}")
                counts["with_static"] += 1
                counts["static_ids"] += 1

        if e.multiframe.result_id and e.multiframe.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
            if e.multiframe.action_summary or e.multiframe.motion_type or e.multiframe.stability_risk:
                lines.append(f"  motion_evidence_id={e.multiframe.result_id} "
                             f"type={e.multiframe.motion_type} "
                             f"stability_risk={e.multiframe.stability_risk} "
                             f"summary={e.multiframe.action_summary[:160]}")
                counts["with_motion"] += 1
                counts["motion_ids"] += 1

        if e.video_native.result_id and e.video_native.state in (dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
            evs = e.video_native.events[:MAX_EVENTS_PER_SHOT]
            if evs or e.video_native.summary:
                lines.append(f"  video_evidence_id={e.video_native.result_id} "
                             f"summary={e.video_native.summary[:160]}")
                for ev in evs:
                    lines.append(f"    event {ev.start_sec:.2f}-{ev.end_sec:.2f}s "
                                 f"{ev.event_type}: {ev.description[:120]}")
                if len(e.video_native.events) > len(evs):
                    lines.append(f"    detail_events_omitted={len(e.video_native.events) - len(evs)}")
                counts["with_video"] += 1
                counts["video_ids"] += 1

        # A human *rating* is a constraint, not content. The note is never sent:
        # it may contain people, places or contact details.
        lines.append(f"  human_rating={e.human.rating}"
                     + (f" human_role={e.human.story_role[:40]}" if e.human.story_role else "")
                     + (" LOCKED" if e.locked_in_base else ""))

    header = [
        f"THEME (creative constraint, not a fact about footage): {brief.theme[:200]}",
        f"TARGET DURATION: {brief.target_duration_sec}s",
        f"REQUESTED STYLE: {brief.style}",
        f"VARIANTS REQUESTED: {brief.variant_count}",
        f"AUDIO: not analysed for any shot.",
        "",
        "FULL CANDIDATE ROSTER (channel order: static, multiframe, video_native):",
    ]
    text = "\n".join(header + roster + ["", "SHOT EVIDENCE:"] + lines) + "\n\n" + SCHEMA_HINT
    raw = text.encode("utf-8")
    if len(raw) > MAX_PROMPT_BYTES:
        raise ModelPlanError("MODEL_INPUT_BUDGET_EXCEEDED",
                             f"全量候选需要 {len(raw)} 字节，策划输入上限为 {MAX_PROMPT_BYTES} 字节。")
    summary = {
        "shots_listed": counts["shots"],
        "shots_with_static": counts["with_static"],
        "shots_with_motion": counts["with_motion"],
        "shots_with_video": counts["with_video"],
        "evidence_ids": counts["static_ids"] + counts["motion_ids"] + counts["video_ids"],
        "sends_images": False,
        "sends_user_notes": MAX_NOTE_CHARS > 0,
        "sends_paths": False,
        "chars": len(text),
        "prompt_version": PROMPT_VERSION,
        "prompt_digest": hashlib.sha256(raw).hexdigest(),
        "sent_segment_ids": [e.segment_id for e in usable],
        "input_snapshot": planning_input.audit_snapshot(),
        "brief": brief.audit_summary(),
    }
    return text, summary


# ---------------------------------------------------------------------------
# Parsing the model's answer
# ---------------------------------------------------------------------------

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> dict:
    """Pull one JSON object out of a model reply, refusing anything else."""
    if not isinstance(text, str) or not text.strip():
        raise ModelPlanError("MODEL_INVALID_OUTPUT", "the model returned no text")
    candidates = [text.strip()]
    m = _JSON_BLOCK.search(text)
    if m:
        candidates.append(m.group(0))
    for raw in candidates:
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ModelPlanError("MODEL_INVALID_OUTPUT",
                         "the model reply contained no JSON object")


def _coerce_channel(raw: str) -> str:
    ch = str(raw or "").strip().lower()
    return ch if ch in ("static", "multiframe", "video_native") else "static"


def _refs_from(raw: list, segment_id: str,
               allowed: dict[str, set[str]]) -> list[EvidenceRef]:
    """Keep only refs that name an evidence id this shot actually has."""
    out: list[EvidenceRef] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        channel = _coerce_channel(item.get("channel"))
        rid = str(item.get("result_id") or "")
        if not rid:
            continue
        # Deliberately drop refs for ids the shot does not own: the validator would
        # reject them anyway, and dropping here keeps the error message focused on
        # what the model actually got wrong.
        if rid not in allowed.get(segment_id, set()):
            continue
        out.append(EvidenceRef(channel=channel, result_id=rid, segment_id=segment_id))
    return out


def _allowed_evidence(planning_input: PlanningInput) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in planning_input.entries:
        ids = set()
        for channel_ev in (e.static, e.multiframe, e.video_native):
            if channel_ev.result_id and channel_ev.state in (
                    dc.STATE_CURRENT, dc.STATE_UNVERIFIED):
                ids.add(channel_ev.result_id)
        out[e.segment_id] = ids
    return out


def parse_model_plan(
    payload: dict, planning_input: PlanningInput, brief: PlanningBrief,
    *, model_id: str, provider_name: str,
) -> PlanDraft:
    """Turn a model payload into a validated-able PlanDraft with server authority."""

    # TSB-371-R1: collect model mistakes instead of coercing them into a
    # shape the validator later rejects with a generic message.
    invented: list[str] = []
    empty_windows: list[tuple[str, float, float]] = []
    variants_raw = payload.get("variants")
    if not isinstance(variants_raw, list) or not variants_raw:
        raise ModelPlanError("MODEL_INVALID_OUTPUT",
                             "the reply had no variants array")

    entries = {e.segment_id: e for e in planning_input.entries}
    allowed = _allowed_evidence(planning_input)
    variants: list[VariantDraft] = []

    for vi, vraw in enumerate(variants_raw[:3], 1):
        if not isinstance(vraw, dict):
            continue
        vid = f"m{vi}"
        sections: list[SectionDraft] = []
        for si, sraw in enumerate(vraw.get("sections") or [], 1):
            if not isinstance(sraw, dict):
                continue
            kind = str(sraw.get("kind") or "body")
            if kind not in ("opening", "body", "closing", "custom"):
                kind = "body"
            sections.append(SectionDraft(
                section_id=str(sraw.get("section_id") or f"{vid}-sec{si}")[:64],
                kind=kind,  # type: ignore[arg-type]
                title=str(sraw.get("title") or "")[:80],
            ))
        if not sections:
            sections = [SectionDraft(section_id=f"{vid}-sec1", kind="body", title="主体")]

        shots: list[ShotDraft] = []
        timeline = 0.0
        for si, sraw in enumerate(vraw.get("shots") or [], 1):
            if not isinstance(sraw, dict):
                continue
            seg_id = str(sraw.get("segment_id") or "")
            entry = entries.get(seg_id)
            if entry is None:
                # TSB-371-R1: a shot the model invented must fail by name, early.
                # Handing it to the validator produced a generic error far from the
                # cause; the model needs to know which id was not in its list.
                invented.append(seg_id)
                # Emit a placeholder so the shared validator reaches its own
                # UNKNOWN_SEGMENT verdict: two independent defences, and the
                # validator's coverage stays exercised.
                shots.append(ShotDraft(
                    shot_id=f"{vid}-s{si:02d}", segment_id=seg_id,
                    asset_id=str(sraw.get("asset_id") or ""),
                    section_id=sections[0].section_id,
                    source_in_sec=0.0, source_out_sec=0.0,
                    planned_duration_sec=0.0,
                    timeline_start_sec=timeline, narrative_role="development",
                    selection_reason="model referenced an unknown shot",
                ))
                continue

            try:
                in_sec = float(sraw.get("source_in_sec", entry.source_in_sec))
                out_sec = float(sraw.get("source_out_sec", entry.source_out_sec))
            except (TypeError, ValueError):
                in_sec, out_sec = entry.source_in_sec, entry.source_out_sec

            # A window that is empty *as the model sent it* is not a rounding
            # problem to be clamped away -- it means the model described no cut.
            # Clamping used to preserve it (32.0-32.0 stays 32.0-32.0) and the
            # failure surfaced much later as a generic validation error.
            if not in_sec < out_sec:
                empty_windows.append(
                    (seg_id, round(in_sec, 3), round(out_sec, 3)))

            # Clamp into the shot's current interval, then keep ordering sane.
            in_sec = max(entry.source_in_sec, min(in_sec, entry.source_out_sec))
            out_sec = max(in_sec, min(out_sec, entry.source_out_sec))
            duration = round(out_sec - in_sec, 3)

            role = str(sraw.get("narrative_role") or "development")
            if role not in ("opening", "development", "turning_point", "climax", "closing"):
                role = "development"

            sec_id = str(sraw.get("section_id") or sections[0].section_id)
            if sec_id not in {s.section_id for s in sections}:
                sec_id = sections[0].section_id

            refs = _refs_from(sraw.get("evidence_refs") or [], seg_id, allowed)
            claims: list[Claim] = []
            for craw in (sraw.get("claims") or [])[:6]:
                if not isinstance(craw, dict):
                    continue
                kind = str(craw.get("kind") or "unknown")
                if kind not in ("observed_signal", "human_supplied",
                                "editorial_suggestion", "unknown"):
                    kind = "unknown"
                claims.append(Claim(
                    text=str(craw.get("text") or "")[:300],
                    kind=kind,  # type: ignore[arg-type]
                    evidence_refs=_refs_from(
                        craw.get("evidence_refs") or [], seg_id, allowed),
                ))

            shots.append(ShotDraft(
                shot_id=f"{vid}-s{si:02d}", segment_id=seg_id,
                asset_id=entry.asset_id, section_id=sec_id,
                source_in_sec=round(in_sec, 3), source_out_sec=round(out_sec, 3),
                planned_duration_sec=duration, timeline_start_sec=round(timeline, 3),
                narrative_role=role,  # type: ignore[arg-type]
                selection_reason=str(sraw.get("selection_reason") or "")[:300],
                claims=claims, evidence_refs=refs,
                needs_review=list(entry.limitations)[:6],
                transition_hint=str(sraw.get("transition_hint") or "")[:80],
                pacing_hint=str(sraw.get("pacing_hint") or "")[:80],
            ))
            timeline = round(timeline + duration, 3)

        total = round(timeline, 3)
        angle = str(vraw.get("narrative_angle") or "chronological")
        if angle not in ("chronological", "highlights_first", "atmospheric"):
            angle = "chronological"

        variants.append(VariantDraft(
            variant_id=vid,
            title=str(vraw.get("title") or f"模型方案 {vi}")[:120],
            narrative_angle=angle,  # type: ignore[arg-type]
            logline=str(vraw.get("logline") or "")[:300],
            status="provisional",
            sections=sections,
            shots=shots,
            total_duration_sec=total,
            target_duration_sec=brief.target_duration_sec,
            duration_gap_sec=round(brief.target_duration_sec - total, 3),
            review_items=[
                ReviewItem(segment_id=s.segment_id, reason=s.needs_review[0])
                for s in shots if s.needs_review
            ],
        ))

    if not variants:
        raise ModelPlanError("MODEL_INVALID_OUTPUT",
                             "no usable variant could be parsed from the reply")

    # A model that invented a shot, or described an empty cut, has produced an
    # unusable plan. Report it precisely, with the ids, so the operator knows
    # exactly what the model got wrong -- but do NOT stop the plan from reaching
    # the shared validator.
    #
    # Defence in depth: the validator has its own UNKNOWN_SEGMENT / window checks
    # and must keep catching these independently. Swallowing the plan here would
    # remove a second line of defence and make the validator's own coverage
    # untestable (test_tsb371_g4_roundtrip relies on seeing that path).
    model_problems: list[dict] = []
    if invented:
        model_problems.append({
            "code": "MODEL_UNKNOWN_SEGMENT",
            "detail": "模型引用了不在输入中的镜头：" + ", ".join(sorted(set(invented))[:5]),
            "segment_ids": sorted(set(invented))[:20],
        })
    if empty_windows:
        sample = ", ".join(f"{s}={a}-{b}" for s, a, b in empty_windows[:3])
        model_problems.append({
            "code": "MODEL_EMPTY_SHOT_WINDOW",
            "detail": f"模型给出了空镜头窗口（起始必须小于结束）：{sample}",
            "windows": [list(w) for w in empty_windows[:20]],
        })

    # ``coverage`` is a CoverageSummary on a real PlanningInput but a plain dict in
    # some tests; read it either way rather than assuming one shape and crashing.
    cov = planning_input.coverage

    def _cov(name: str) -> int:
        if isinstance(cov, dict):
            return int(cov.get(name) or 0)
        return int(getattr(cov, name, 0) or 0)

    return PlanDraft(
        project_id=planning_input.project_id,
        planning_mode="model",
        generation_status="provisional",
        source_input_digest=planning_input.source_input_digest,
        base_storyboard_version_id=planning_input.base_storyboard_version_id,
        coverage=CoverageSummary(
            library_segments=_cov("library_segments"),
            segments_with_any_valid_reading=_cov("segments_with_any_valid_reading"),
            no_valid_model_reading=_cov("no_valid_model_reading"),
            video_native_valid=_cov("video_native_valid"),
        ),
        limitations=list(planning_input.limitations) + ["model_planned"],
        variants=variants,
        withheld=planning_input.withheld,
        needs_more_analysis=bool(_cov("no_valid_model_reading")),
        warnings=[f"由真实模型 {model_id} 生成（provider={provider_name}）；"
                  "结论未经人工观看验证。"]
                 + [f"{q['code']}: {q['detail']}" for q in model_problems],
    )
