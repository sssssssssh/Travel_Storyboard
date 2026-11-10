"""TSB-368-B — read-only director endpoints.

Every handler here is a pure read: no Job is created, no model is called, no
file is uploaded and no storyboard row is written. The services underneath are
shared by both endpoints so the two can never disagree about what a candidate is.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .deps import get_db
from ..models import Project
from ..services import director_candidates as dc
from ..services import director_plan_input as dpi
from ..services import director_plan_validation as dpv
from ..services import director_query as dq
from ..services.director_plan_providers import ModelCallNotAuthorized, OfflineRulesPlanner
from ..services.director_plan_schema import PlanPreviewRequest, PlanningBrief
from ..services.director_schemas import CandidatesOut, ContextOut

router = APIRouter()

_ELIGIBILITY = Literal["eligible", "needs_review", "excluded", "all"]
_SORT = Literal["editorial_priority", "timeline"]


def _project_or_404(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/director/candidates", response_model=CandidatesOut)
def list_director_candidates(
    project_id: str,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    eligibility: _ELIGIBILITY = Query(default="eligible"),
    rating: str | None = Query(default=None, max_length=32),
    story_role: str | None = Query(default=None, max_length=64),
    asset_id: str | None = Query(default=None, max_length=64),
    scene_tag: str | None = Query(default=None, max_length=32),
    q: str | None = Query(default=None, max_length=120),
    sort: _SORT = Query(default="editorial_priority"),
    diversify: bool = Query(default=True),
    verify_freshness: bool = Query(
        default=False,
        description="Re-read a bounded sample of each source to detect changes. "
        "Off by default so a plain browse never touches the filesystem.",
    ),
    db: Session = Depends(get_db),
):
    """Paged director candidates with project-wide coverage.

    ``total`` counts matches before paging; ``coverage`` always describes the
    whole project, never the current filter or page.
    """
    _project_or_404(db, project_id)

    candidates = dc.collect_candidates(db, project_id, verify_freshness=verify_freshness)
    items, total = dq.select(
        candidates,
        eligibility=eligibility, rating=rating, story_role=story_role,
        asset_id=asset_id, scene_tag=scene_tag, q=q, sort=sort,
        diversify=diversify, offset=offset, limit=limit,
    )
    cov = dc.coverage(candidates)

    limitations: list[str] = []
    if not cov["coverage_complete"]:
        limitations.append("partial_analysis_coverage")
    if any(
        ch.state == dc.STATE_UNVERIFIED
        for c in candidates
        for ch in (c.static, c.multiframe, c.video_native)
    ):
        limitations.append("freshness_unverified_for_some_readings")

    return {
        "project_id": project_id,
        "query_version": "1.0",
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [c.as_dict() for c in items],
        "coverage": cov,
        "limitations": limitations,
    }


@router.get("/projects/{project_id}/director/context", response_model=ContextOut)
def director_context(
    project_id: str,
    max_candidates: int = Query(default=40, ge=1, le=200),
    theme: str = Query(default="", max_length=200),
    target_duration_sec: float | None = Query(default=None, ge=1, le=36000),
    db: Session = Depends(get_db),
):
    """Normalized planning input for TSB-369.

    This endpoint **does not generate a story**. It returns the candidate set,
    the human labels that must be respected, honest coverage numbers and an
    ``input_digest`` that changes when the underlying input changes.
    """
    _project_or_404(db, project_id)

    candidates = dc.collect_candidates(db, project_id, verify_freshness=False)
    cov = dc.coverage(candidates)
    brief = {"theme": theme, "target_duration_sec": target_duration_sec}
    return dq.build_context(
        project_id=project_id,
        candidates=candidates,
        coverage=cov,
        brief=brief,
        max_candidates=max_candidates,
    )


class PlanRejected(Exception):
    """A plan that failed validation; carries every problem for the client."""

    def __init__(self, problems: list[dict]) -> None:
        self.problems = problems
        super().__init__("plan rejected")


@router.get("/projects/{project_id}/director/plans/input-status")
def director_plan_input_status(
    project_id: str,
    max_candidates: int = Query(default=dpi.DEFAULT_MAX_CANDIDATES, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """The single source of truth for "what would the planner see right now?".

    A browser cannot compute ``input_digest`` itself, and calling ``/context`` to
    guess it risks using a different ``max_candidates`` or brief projection than
    the planner does — which would make the 409 guard either miss real changes or
    fire spuriously. This endpoint runs the **same** assembly the planner runs, so
    the digest it returns is by construction the one the next preview will compare
    against.

    Read-only: it creates no storyboard, version, or job.
    """
    _project_or_404(db, project_id)
    try:
        planning_input = dpi.build_planning_input(
            project_id, max_candidates=max_candidates, session=db
        )
    except dpi.PlanningInputError as exc:
        raise HTTPException(status_code=404,
                            detail={"code": exc.code, "message": exc.message})

    entries = planning_input.entries
    return {
        "project_id": project_id,
        "input_digest": planning_input.source_input_digest,
        "max_candidates": max_candidates,
        "coverage": planning_input.coverage,
        "withheld": planning_input.withheld,
        "limitations": planning_input.limitations,
        "base_storyboard_version_id": planning_input.base_storyboard_version_id,
        "storyboard_absent": planning_input.storyboard_absent,
        "segment_count": len(entries),
        "segments_with_any_valid_reading": sum(
            1 for e in entries if e.has_usable_reading),
        "locked_segments": sum(1 for e in entries if e.locked_in_base),
        "evidence_channels": {
            "static": sum(1 for e in entries
                          if e.static.result_id and e.static.state in (
                              dc.STATE_CURRENT, dc.STATE_UNVERIFIED)),
            "multiframe": sum(1 for e in entries
                              if e.multiframe.result_id and e.multiframe.state in (
                                  dc.STATE_CURRENT, dc.STATE_UNVERIFIED)),
            "video_native": sum(1 for e in entries
                                if e.video_native.result_id
                                and e.video_native.state in (
                                    dc.STATE_CURRENT, dc.STATE_UNVERIFIED)),
        },
    }


@router.post("/projects/{project_id}/director/plans/preview")
def director_plan_preview(
    project_id: str,
    body: PlanPreviewRequest,
    db: Session = Depends(get_db),
):
    """Produce a **read-only draft** plan. Writes nothing, calls no model.

    Status codes are part of the contract:

    * 404 — no such project
    * 409 ``DIRECTOR_INPUT_CHANGED`` — the library or the human choices moved
      since the caller read ``/director/context``
    * 422 ``PLAN_REJECTED`` — the planner produced something invalid (the
      problems are listed so the client can show them)
    * 403 ``MODEL_CALL_NOT_AUTHORIZED`` — model planning was requested
    * 200 — a draft, which may itself be ``insufficient_evidence``
    """
    _project_or_404(db, project_id)

    # Model planning needs an explicit grant on THIS request, on top of the
    # global mode switch and the endpoint checks. Absent -> refuse, zero network.
    if body.planning_mode == "model" and not body.upload_acknowledged:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MODEL_CALL_NOT_AUTHORIZED",
                "message": "真实模型策划未获授权；本次请求不会发起任何外部模型调用。",
            },
        )

    brief = PlanningBrief(
        theme=body.theme,
        target_duration_sec=body.target_duration_sec,
        style=body.style,
        variant_count=body.variant_count,
    )

    try:
        planning_input = dpi.build_planning_input(
            project_id, max_candidates=body.max_candidates, session=db
        )
    except dpi.PlanningInputError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code,
                                                     "message": exc.message})

    if (body.expected_input_digest
            and body.expected_input_digest != planning_input.source_input_digest):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "DIRECTOR_INPUT_CHANGED",
                "message": "素材或人工选择已变化，请重新获取候选后再生成草案。",
                "current_input_digest": planning_input.source_input_digest,
            },
        )

    if body.planning_mode == "model":
        plan = _plan_with_model(planning_input, brief)
    else:
        plan = OfflineRulesPlanner().plan(planning_input, brief)

    problems = dpv.validate_plan(plan, planning_input)
    if problems:
        # A rejected plan is a defect in the planner, not in the request; the
        # client still gets the detail so it is never a silent blank screen.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "PLAN_REJECTED",
                "message": "生成的草案未通过校验，未返回任何方案。",
                "problems": [p.as_dict() for p in problems],
            },
        )

    return plan.model_dump(mode="json")


def _plan_with_model(planning_input, brief):
    """Run the real model planner through the existing authorization gate.

    Every refusal keeps a stable code so the UI can explain itself:
    ``MODEL_CALL_NOT_AUTHORIZED`` (no grant / wrong endpoint),
    ``MODEL_INVALID_OUTPUT`` (unparseable reply), ``MODEL_CALL_FAILED`` (transport).
    """
    from ..services.director_plan_model import ModelPlanError
    from ..services.director_plan_providers import ExternalPlannerProvider
    from ..services.vision.authorization import (
        AuthorizedModelContext, authorization_denial_reason,
    )
    from ..services.vision.errors import ModelError
    from ..services.vision import get_provider

    reason = authorization_denial_reason(
        upload_acknowledged=True, project_id=planning_input.project_id)
    if reason is not None:
        code = reason.split(":", 1)[0]
        raise HTTPException(status_code=403,
                            detail={"code": code, "message": reason})

    try:
        provider = get_provider()
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "code": "MODEL_UNAVAILABLE",
            "message": f"模型端点不可用：{str(exc)[:160]}"})

    from datetime import datetime, timezone

    ctx = AuthorizedModelContext(
        project_id=planning_input.project_id,
        purpose="director_plan",
        acknowledged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    planner = ExternalPlannerProvider(authorized=ctx, provider=provider)
    try:
        return planner.plan(planning_input, brief)
    except ModelError as exc:
        raise HTTPException(status_code=502, detail={
            "code": getattr(exc, "code", "MODEL_CALL_FAILED"),
            "message": str(exc)[:200]})
    except ModelPlanError as exc:
        raise HTTPException(status_code=422, detail={
            "code": exc.code, "message": exc.message})
