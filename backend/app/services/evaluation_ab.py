"""TSB-371-C — blind A/B between the offline plan and a real model plan.

The blind is the whole point, so the design puts the mapping on the server:

* the A/B *side* assignment is generated with a CSPRNG and stored; it is never
  derived from the plan's mode, and never sent to the browser before submission;
* both sides are rendered from an identical projection, so nothing in the payload
  (title, variant id, logline) can hint at which is the model;
* the reveal happens only after a preference is submitted, and the honest metric
  is reported beside it — with one reviewer, the output is *that reviewer's
  preference*, never a "win rate".

Pairing is refused unless the two plans are genuinely comparable: same project,
same ``input_digest``, same candidate count and target duration, both validated,
and neither being a mock.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EvaluationSession
from . import director_plan_validation as plan_validation
from .director_plan_schema import PlanDraft

AB_SCHEMA_VERSION = "1.0"
DIMENSIONS = ("coherence", "pacing", "reason_credibility", "operability")
VALID_PREFERENCE = ("A", "B", "TIE", "NEITHER")


class AbError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


@dataclass
class AbPair:
    session: EvaluationSession
    left: dict[str, Any]
    right: dict[str, Any]
    mapping: dict[str, str]


def _neutral_variant(variant, *, side: str) -> dict[str, Any]:
    """Project one variant into the blind view.

    Everything that could identify the planner is dropped: the original title, the
    variant id, the mode, and the provider. The *content* is preserved exactly —
    rewriting the plans to look alike would defeat the comparison.
    """
    return {
        "side": side,
        "sections": [
            {"section_id": s.section_id, "kind": s.kind, "title": s.title}
            for s in variant.sections
        ],
        "shots": [
            {
                "order": i + 1,
                "segment_id": shot.segment_id,
                "source_in_sec": shot.source_in_sec,
                "source_out_sec": shot.source_out_sec,
                "planned_duration_sec": shot.planned_duration_sec,
                "timeline_start_sec": shot.timeline_start_sec,
                "narrative_role": shot.narrative_role,
                "selection_reason": shot.selection_reason,
                # Claims keep their *content* but not their provenance labels,
                # which would otherwise reveal whether rules or a model wrote them.
                "claims": [c.text for c in shot.claims],
                "needs_review": list(shot.needs_review),
                "transition_hint": shot.transition_hint,
                "pacing_hint": shot.pacing_hint,
            }
            for i, shot in enumerate(variant.shots)
        ],
        "total_duration_sec": variant.total_duration_sec,
        "target_duration_sec": variant.target_duration_sec,
        "duration_gap_sec": variant.duration_gap_sec,
        "storyline": {
            "opening": _roles(variant, "opening"),
            "development": _roles(variant, "development"),
            "turning_point": _roles(variant, "turning_point"),
            "climax": _roles(variant, "climax"),
            "closing": _roles(variant, "closing"),
        },
    }


def _roles(variant, role: str) -> list[int]:
    return [i + 1 for i, s in enumerate(variant.shots) if s.narrative_role == role]


#: Fields the real model adapter stamps onto a draft after a successful call.
RECEIPT_FIELDS = ("model_id", "provider", "call_id", "elapsed_ms")


def has_model_receipt(plan: PlanDraft) -> bool:
    """True only for a draft produced by an actual endpoint call.

    ``planning_mode == "model"`` alone is a label any caller can set, so pairing
    relies on this instead. The adapter writes the receipt; a stub, a hand-built
    PlanDraft or an offline plan cannot produce one.
    """
    summary = getattr(plan, "request_summary", None) or {}
    receipt = summary.get("receipt") if isinstance(summary, dict) else None
    if not isinstance(receipt, dict):
        return False
    return all(receipt.get(f) not in (None, "") for f in RECEIPT_FIELDS)


def _fingerprint(plan: PlanDraft) -> str:
    """A stable signature of a plan's content, used for distinctness."""
    raw = plan.model_dump_json(exclude={"warnings", "limitations", "request_summary"})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def check_pairing(offline: PlanDraft, model: PlanDraft, planning_input) -> list[dict]:
    """Every reason this pair must NOT be presented as a fair A/B."""
    problems: list[dict] = []

    if offline.planning_mode != "offline_rules":
        problems.append({"code": "AB_LEFT_NOT_OFFLINE",
                         "message": "the baseline plan is not an offline rules plan"})

    # Comparability first: "these are two different inputs" is a more actionable
    # finding than "the right side is not backed by a call", so it is reported first.
    if offline.project_id != model.project_id:
        problems.append({"code": "AB_PROJECT_MISMATCH",
                         "message": "两个方案属于不同项目"})
    if offline.source_input_digest != model.source_input_digest:
        problems.append({"code": "AB_INPUT_DIGEST_MISMATCH",
                         "message": "两份方案的输入摘要不同，不能称 A/B"})

    if model.planning_mode != "model":
        problems.append({"code": "AB_NO_REAL_MODEL_PLAN",
                         "message": "右侧不是真实模型方案（Mock 或离线），不能作为 B 侧"})
    elif not has_model_receipt(model):
        # The label is not evidence. TSB-370 produced "model" drafts from a stub
        # provider; a plan may only stand as the model side if it carries the
        # receipt the real adapter writes when it actually called the endpoint.
        problems.append({"code": "AB_NO_REAL_MODEL_PLAN",
                         "message": "该方案没有真实模型调用回执，不能作为 B 侧"})

    if not offline.variants or not model.variants:
        problems.append({"code": "AB_EMPTY_PLAN", "message": "有一侧没有任何方案"})
        return problems

    left = offline.variants[0]
    right = model.variants[0]
    if abs(left.target_duration_sec - right.target_duration_sec) > 1e-6:
        problems.append({"code": "AB_TARGET_MISMATCH",
                         "message": "目标时长不同，比较不公平"})

    if _fingerprint(offline) == _fingerprint(model):
        problems.append({"code": "AB_NOT_DISTINCT",
                         "message": "两份方案内容完全相同，不能作为独立对比"})

    # Both plans must still satisfy the shared validator.
    for label, plan in (("left", offline), ("right", model)):
        leftover = plan_validation.validate_plan(plan, planning_input)
        if leftover:
            problems.append({
                "code": "AB_PLAN_INVALID",
                "message": f"{label} 方案未通过校验",
                "problems": [p.as_dict() for p in leftover],
            })
    return problems


def create_ab_session(
    session: Session, project_id: str, offline: PlanDraft, model: PlanDraft,
    planning_input, *, reviewer: str = "",
) -> AbPair:
    """Pair two plans and fix the blind mapping on the server."""
    problems = check_pairing(offline, model, planning_input)
    if problems:
        raise AbError(
            problems[0]["code"],
            problems[0]["message"], 409 if problems[0]["code"] != "AB_NO_REAL_MODEL_PLAN" else 422)

    # Cryptographically random, stored, and only revealed after submission.
    model_on_left = secrets.randbelow(2) == 0
    mapping = {
        "A": "model" if model_on_left else "offline_rules",
        "B": "offline_rules" if model_on_left else "model",
    }

    offline_variant = offline.variants[0]
    model_variant = model.variants[0]
    left_plan, right_plan = ((model, offline) if model_on_left else (offline, model))
    left_variant, right_variant = ((model_variant, offline_variant) if model_on_left
                                   else (offline_variant, model_variant))

    payload = {
        "schema": AB_SCHEMA_VERSION,
        "mapping": mapping,
        "left": _neutral_variant(left_variant, side="A"),
        "right": _neutral_variant(right_variant, side="B"),
        "input_digest": offline.source_input_digest,
        "target_duration_sec": offline_variant.target_duration_sec,
        "offline_fingerprint": _fingerprint(offline),
        "model_fingerprint": _fingerprint(model),
        "model_id": (model.request_summary or {}).get("model_id", ""),
    }

    row = EvaluationSession(
        id=_uid(), project_id=project_id, kind="ab", status="IN_PROGRESS",
        sample_seed="", sampling_rule_version=AB_SCHEMA_VERSION,
        reviewer=reviewer, context_json=payload,
    )
    session.add(row)
    session.flush()
    return AbPair(session=row, left=payload["left"], right=payload["right"],
                  mapping=mapping)


def _uid() -> str:
    import uuid

    return str(uuid.uuid4())


def blind_view(pair_row: EvaluationSession) -> dict[str, Any]:
    """What the browser may see *before* submitting: no provenance anywhere."""
    data = pair_row.context_json or {}
    return {
        "session_id": pair_row.id,
        "project_id": pair_row.project_id,
        "status": pair_row.status,
        "schema": data.get("schema", AB_SCHEMA_VERSION),
        "A": data.get("left"),
        "B": data.get("right"),
        "target_duration_sec": data.get("target_duration_sec"),
        # Deliberately absent: mapping, model_id, planning modes.
        "revealed": False,
    }


def submit_preference(pair_row: EvaluationSession, payload: dict) -> dict[str, Any]:
    """Record the blind judgement, then reveal the mapping."""
    preference = payload.get("preference")
    if preference not in VALID_PREFERENCE:
        raise AbError("AB_PREFERENCE_INVALID",
                      f"preference must be one of {VALID_PREFERENCE}", 422)

    scores: dict[str, dict[str, int]] = {}
    for side in ("A", "B"):
        raw = payload.get(f"{side.lower()}_scores") or {}
        block: dict[str, int] = {}
        for dim in DIMENSIONS:
            value = raw.get(dim)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
                raise AbError("AB_SCORE_INVALID", f"{side}.{dim} must be 0..4", 422)
            block[dim] = value
        scores[side] = block

    data = dict(pair_row.context_json or {})
    if data.get("submission"):
        raise AbError("AB_ALREADY_SUBMITTED",
                      "this session was already submitted; blind results are immutable", 409)

    data["submission"] = {
        "preference": preference,
        "scores": scores,
        "shot_selection": payload.get("shot_selection"),
        "note": str(payload.get("note") or "")[:500],
    }
    pair_row.context_json = data
    pair_row.status = "SUBMITTED"
    return data


def revealed_view(pair_row: EvaluationSession) -> dict[str, Any]:
    """After submission the provenance is shown, together with the blind result."""
    data = pair_row.context_json or {}
    if not data.get("submission"):
        raise AbError("AB_NOT_SUBMITTED", "reveal is only available after submission", 409)

    mapping = data.get("mapping", {})
    submission = data["submission"]
    preference = submission["preference"]

    # A single reviewer can only ever express *their own* preference.
    winner = None
    if preference in ("A", "B"):
        winner = mapping.get(preference)

    return {
        "session_id": pair_row.id,
        "project_id": pair_row.project_id,
        "status": pair_row.status,
        "revealed": True,
        "mapping": mapping,
        "submission": submission,
        "winner_mode": winner,
        "reviewers": 1,
        "interpretation": (
            "仅有 1 位评价者：以下只代表该评价者的偏好，不是人群胜率。"
        ),
        "A": data.get("left"),
        "B": data.get("right"),
        "target_duration_sec": data.get("target_duration_sec"),
    }


def find_ab_session(session: Session, project_id: str,
                    session_id: str) -> EvaluationSession | None:
    row = session.get(EvaluationSession, session_id)
    if row is None or row.project_id != project_id or row.kind != "ab":
        return None
    return row
