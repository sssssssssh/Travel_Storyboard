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
import json
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


def has_model_receipt(plan: PlanDraft, *, session=None,
                      project_id: str = "") -> bool:
    """True only when the plan's receipt names a *server* record of a real call.

    TSB-371 asked whether four fields were non-empty, which a client satisfies by
    typing them. A client-supplied receipt is therefore treated as **no evidence**:
    the only thing that counts is a row in ``model_call_records`` written by the
    trusted planner path, belonging to the same project, for a call that succeeded.

    Without a ``session`` there is no way to check, so the answer is False. Pairing
    must never fall back to trusting the document.
    """
    if session is None:
        return False

    summary = getattr(plan, "request_summary", None) or {}
    receipt = summary.get("receipt") if isinstance(summary, dict) else None
    if not isinstance(receipt, dict):
        return False

    call_id = receipt.get("call_id")
    if not isinstance(call_id, str) or not call_id.strip():
        return False

    from ..models import ModelCallRecord

    row = session.get(ModelCallRecord, call_id.strip())
    if row is None or not row.succeeded:
        return False
    if not row.purpose or row.purpose != "director_plan":
        return False
    if project_id and row.project_id != project_id:
        return False
    # A call recorded for a different project is not evidence for this one.
    if not project_id and row.project_id != plan.project_id:
        return False
    return True


def record_model_call(session, *, call_id: str, channel: str, project_id: str,
                      purpose: str, succeeded: bool, model_id: str = "",
                      result_digest: str = "", request_bytes: int = 0,
                      response_bytes: int = 0, elapsed_ms: int = 0,
                      error_code: str = "") -> None:
    """Write the server-side proof. Called by the trusted planner path only."""
    from ..models import ModelCallRecord

    session.add(ModelCallRecord(
        id=call_id, channel=channel, project_id=project_id, purpose=purpose,
        result_digest=result_digest, succeeded=succeeded, model_id=model_id,
        request_bytes=request_bytes, response_bytes=response_bytes,
        elapsed_ms=elapsed_ms, error_code=error_code,
    ))
    session.flush()


def content_fingerprint(plan: PlanDraft) -> str:
    """What the plan actually *cuts* — and nothing else.

    TSB-371 hashed the whole draft minus a few fields, so ``planning_mode`` and the
    model metadata sat inside the hash. Two plans cutting the same shots from the
    same windows therefore looked different, and an A/B between them was accepted
    even though the reviewer was comparing a plan with itself.

    Included: shot order, segment, source window, narrative role, section.
    Excluded: planning_mode, model/provider/call ids, warnings, limitations,
    selection_reason prose, titles, loglines. Two planners may explain the same cut
    differently; that is not a different edit.
    """
    if not plan.variants:
        return hashlib.sha256(b"empty").hexdigest()[:32]

    variant = plan.variants[0]
    shape = {
        "total_duration_sec": round(float(variant.total_duration_sec), 3),
        "target_duration_sec": round(float(variant.target_duration_sec), 3),
        "sections": [s.section_id for s in variant.sections],
        "shots": [
            {
                "order": index,
                "segment_id": shot.segment_id,
                "asset_id": shot.asset_id,
                "source_in_sec": round(float(shot.source_in_sec), 3),
                "source_out_sec": round(float(shot.source_out_sec), 3),
                "narrative_role": shot.narrative_role,
                "section_id": shot.section_id,
            }
            for index, shot in enumerate(variant.shots)
        ],
    }
    raw = json.dumps(shape, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _fingerprint(plan: PlanDraft) -> str:
    """Deprecated alias kept so older call sites keep working."""
    return content_fingerprint(plan)


def check_pairing(offline: PlanDraft, model: PlanDraft, planning_input,
                  *, session=None, project_id: str = "") -> list[dict]:
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
    elif not has_model_receipt(model, session=session, project_id=project_id):
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

    if content_fingerprint(offline) == content_fingerprint(model):
        problems.append({"code": "AB_NOT_DISTINCT",
                         "message": "两份方案内容完全相同，不能作为独立对比"})

    # Both plans must still satisfy the shared validator. Without a planning input
    # there is nothing to validate against; the caller that omits it is checking
    # pairing rules only, so skip rather than crash.
    for label, plan in (("left", offline), ("right", model)):
        if planning_input is None:
            continue
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
    problems = check_pairing(offline, model, planning_input,
                             session=session, project_id=project_id)
    if problems:
        detail = "; ".join(
            f"{p['code']}:{p.get('message','')}"
            + (": " + str([q.get("code") for q in p.get("problems", [])]) if p.get("problems") else "")
            for p in problems[:3])
        raise AbError(
            problems[0]["code"],
            detail, 409 if problems[0]["code"] != "AB_NO_REAL_MODEL_PLAN" else 422)

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


def current_view(pair_row: EvaluationSession, *, wants_reveal: bool = False) -> dict[str, Any]:
    """What the caller may see, decided by the stored state.

    TSB-371's ``blind_view`` always said ``revealed: False``, so a refresh after
    submitting dropped the reviewer back into the blind — the mapping was still in
    the database, they simply could not see the answer they had earned.

    ``wants_reveal`` is recorded but never overrides the truth in either direction:
    a client cannot reveal early, and cannot hide a result it has already seen.
    """
    data = pair_row.context_json or {}
    submitted = bool(data.get("submission"))
    if submitted:
        return revealed_view(pair_row)
    return blind_view(pair_row)


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
