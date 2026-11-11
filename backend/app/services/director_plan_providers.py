"""TSB-369-B — planner providers: interface, offline rules, mock, and a gate.

The interface exists so that "who produced this plan" is explicit. Three
implementations matter:

* :class:`OfflineRulesPlanner` — real, deterministic, no network. This is what
  the preview endpoint runs in production.
* :class:`MockPlannerProvider` — test-only. It exists to prove the *validator*
  rejects bad plans; it is deliberately not reachable from HTTP.
* :class:`ExternalPlannerProvider` — a gate, not an implementation. It raises
  ``MODEL_CALL_NOT_AUTHORIZED`` without touching the network, so the authorization
  requirement is enforced in code rather than in a document.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .director_plan_input import PlanningInput
from ..config import get_settings
from .director_plan_rules import plan_offline
from .director_plan_schema import PlanDraft, PlanningBrief, VariantDraft

#: Imported lazily inside ``plan`` to avoid a module cycle; re-exported here so the
#: prompt lives beside the parser that enforces it.
SYSTEM_PROMPT_TEXT = (
    "You are a video editing planner. You receive numbered shots with evidence "
    "recorded by a vision model. Rules: only reference shots by their exact "
    "segment_id; describe content only when citing that shot's evidence ids; the "
    "user's theme is a creative constraint, never a fact about the footage; audio "
    "was not analysed, so never mention music, beats or sound; output JSON only."
)


class PlannerProvider(Protocol):
    """Every planner returns candidate variants; none of them may bypass validation."""

    name: str

    def propose(
        self, planning_input: PlanningInput, brief: PlanningBrief
    ) -> list[VariantDraft]: ...


class OfflineRulesPlanner:
    """Deterministic structural planning. Production default."""

    name = "offline_rules"

    def propose(self, planning_input: PlanningInput,
                brief: PlanningBrief) -> list[VariantDraft]:
        return plan_offline(planning_input, brief).variants

    def plan(self, planning_input: PlanningInput, brief: PlanningBrief) -> PlanDraft:
        return plan_offline(planning_input, brief)


class MockPlannerProvider:
    """Test-only planner: returns whatever the test hands it.

    Its purpose is adversarial: feed it a plan that cites an invented shot, a
    confident claim with no evidence, or a window outside the source, and confirm
    the validator refuses. It must never be wired to a public request field.
    """

    name = "mock"

    def __init__(self, factory: Callable[[PlanningInput, PlanningBrief], list[VariantDraft]]):
        self._factory = factory

    def propose(self, planning_input: PlanningInput,
                brief: PlanningBrief) -> list[VariantDraft]:
        return self._factory(planning_input, brief)


class ModelCallNotAuthorized(Exception):
    """Raised without any network activity when model planning is not permitted."""

    code = "MODEL_CALL_NOT_AUTHORIZED"

    def __init__(self, message: str = (
        "真实模型策划未获授权：本轮不会发起任何外部模型请求。"
        "如需启用，请单独批准服务端点、发送内容、段数与费用上限。"
    )) -> None:
        super().__init__(message)
        self.message = message


class ExternalPlannerProvider:
    """The real model planner, active only with an explicit per-request grant.

    Constructing it does nothing. It only reaches the network inside
    :meth:`plan`, and only after the caller has presented an
    :class:`AuthorizedModelContext` produced by ``authorize_upload`` — the same gate
    every other outbound path uses. With no context it raises
    :class:`ModelCallNotAuthorized` and touches nothing.
    """

    name = "model"

    def __init__(self, *, authorized=None, provider=None) -> None:
        self._authorized = authorized
        self._provider = provider

    def propose(self, planning_input: PlanningInput,
                brief: PlanningBrief) -> list[VariantDraft]:
        return self.plan(planning_input, brief).variants

    def plan(self, planning_input: PlanningInput, brief: PlanningBrief):
        from .director_plan_model import (
            ModelPlanError, build_evidence_text, extract_json, parse_model_plan,
        )

        if self._authorized is None:
            raise ModelCallNotAuthorized()
        if self._provider is None:
            raise ModelCallNotAuthorized("模型端点未配置，无法发起策划请求。")

        text, summary = build_evidence_text(planning_input, brief)

        # Planning is its own budget bucket. Without this the POST would be booked
        # to whatever channel the previous job left behind (or the default), which
        # would let a planning call consume the vision allowance.
        from .vision.providers.openai_compatible import ledger_channel

        with ledger_channel("plan_text"):
            return self._plan_locked(planning_input, brief, text, summary)

    def _plan_locked(self, planning_input: PlanningInput, brief: PlanningBrief,
                     text: str, summary: dict):
        from .director_plan_model import (
            ModelPlanError, extract_json, parse_model_plan,
        )
        from .vision.providers.openai_compatible import VisionRequest

        request = VisionRequest(
            prompt=text,
            images=[],
            system=SYSTEM_PROMPT_TEXT,
            json_object=True,
            max_tokens=get_settings().vision_max_tokens,
            temperature=0.4,
            authorization=self._authorized,
            text_only=True,
        )
        from .vision.errors import ModelError

        try:
            response = self._provider.complete(request)
        except ModelError:
            raise
        except Exception as exc:
            raise ModelPlanError(getattr(exc, "code", "MODEL_CALL_FAILED"),
                                 str(exc)[:300]) from exc

        payload = extract_json(getattr(response, "text", "") or "")
        plan = parse_model_plan(
            payload, planning_input, brief,
            model_id=get_settings().model_name or "?",
            provider_name="openai_compatible",
        )
        return plan.model_copy(update={"request_summary": summary})


def provider_for(mode: str) -> PlannerProvider:
    """Map a request mode to a provider, refusing anything not enabled.

    ``mock`` is intentionally unreachable: there is no public request value that
    selects it, and asking for it here raises rather than silently falling back.
    """
    if mode == "offline_rules":
        return OfflineRulesPlanner()
    if mode == "model":
        return ExternalPlannerProvider()
    raise ValueError(f"unsupported planning mode {mode!r}")
