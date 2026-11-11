"""TSB-371-0 — the A/B driver must compare the same input, not two different ones.

The TSB-370 driver read ``input-status`` with ``max_candidates=12`` and then asked
for the offline plan with ``max_candidates=40``. Two consequences, both bad:

* the digest check fires 409 for the offline request (so the baseline silently
  disappears from the comparison), or
* if the check were relaxed, the "A/B" would be two *different* candidate sets —
  not an A/B at all.

The task book (§1.2) is explicit: only the planner implementation may differ.
These tests pin that by parsing the driver's request bodies rather than trusting
the script to be tidy.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

DRIVER = Path(__file__).resolve().parents[1] / "tools" / "tsb370_plan_ab.py"


def _driver_source() -> str:
    assert DRIVER.is_file(), f"driver missing: {DRIVER}"
    return DRIVER.read_text(encoding="utf-8")


def _load_driver():
    """Import the driver module (it must be importable without a server)."""
    import importlib

    return importlib.import_module("tools.tsb370_plan_ab")


def test_the_driver_exists():
    assert DRIVER.is_file()


def test_every_request_uses_the_same_candidate_budget():
    """The actual bug: digest read at 12, offline requested at 40."""
    mod = _load_driver()
    bodies = [
        mod._request(planning_mode="offline_rules", digest="d"),
        mod._request(planning_mode="offline_rules", style="highlights_first", digest="d"),
        mod._request(planning_mode="model", digest="d", ack=True),
    ]
    budgets = {b["max_candidates"] for b in bodies}
    assert len(budgets) == 1, f"max_candidates differs between requests: {budgets}"
    assert bodies[0]["max_candidates"] == mod.INPUT["max_candidates"]


def test_the_digest_is_read_with_the_same_budget():
    src = _driver_source()
    assert "INPUT['max_candidates']" in src or 'INPUT["max_candidates"]' in src, (
        "the input-status query must use the shared constant, not a literal")


def test_theme_and_target_are_shared_across_both_planners():
    mod = _load_driver()
    off = mod._request(planning_mode="offline_rules", digest="d")
    mdl = mod._request(planning_mode="model", digest="d", ack=True)
    assert off["theme"] == mdl["theme"] == mod.INPUT["theme"]
    assert off["target_duration_sec"] == mdl["target_duration_sec"] == \
        mod.INPUT["target_duration_sec"]
    assert off["variant_count"] == mdl["variant_count"]


def test_only_the_planner_differs_between_the_two_sides():
    """Everything except planning_mode must be identical — that is what makes it A/B."""
    mod = _load_driver()
    off = mod._request(planning_mode="offline_rules", digest="d")
    mdl = mod._request(planning_mode="model", digest="d", ack=True)
    differing = {k for k in set(off) | set(mdl)
                 if off.get(k) != mdl.get(k)}
    assert differing <= {"planning_mode", "upload_acknowledged"}, (
        f"the two sides differ in more than the planner: {differing}")


def test_every_preview_request_carries_the_digest():
    mod = _load_driver()
    for mode, ack in (("offline_rules", False), ("model", True)):
        body = mod._request(planning_mode=mode, digest="abc123", ack=ack)
        assert body["expected_input_digest"] == "abc123"


def test_the_model_request_requires_an_explicit_acknowledgement():
    mod = _load_driver()
    plain = mod._request(planning_mode="model", digest="d")
    acked = mod._request(planning_mode="model", digest="d", ack=True)
    assert "upload_acknowledged" not in plain or plain["upload_acknowledged"] is False
    assert acked["upload_acknowledged"] is True


def test_the_driver_files_both_plans_under_stable_names():
    src = _driver_source()
    assert "ab_result.json" in src
    assert "offline_" in src and "model_plan.json" in src
