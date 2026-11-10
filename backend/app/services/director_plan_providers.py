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
from .director_plan_rules import plan_offline
from .director_plan_schema import PlanDraft, PlanningBrief, VariantDraft


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
    """A placeholder that refuses. No client library is imported here.

    Deliberate: importing a provider SDK "just in case" would create a code path
    that could accidentally make a call. When model planning is authorized, this
    class is where the integration goes — behind the same validator, and with the
    existing per-call authorization and budget checks applied.
    """

    name = "model"

    def propose(self, planning_input: PlanningInput,
                brief: PlanningBrief) -> list[VariantDraft]:
        raise ModelCallNotAuthorized()


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
