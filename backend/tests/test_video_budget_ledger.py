"""TSB-338 — 预算必须在**每次 HTTP 尝试之前**生效，并留下可审计台账。

任务书 T2 预算契约要求：

===========================  ==================================================
max_segments                 编码前
max_total_sec                编码前 + 用**实际派生**时长复核
max_total_bytes              **每次** HTTP 尝试前（含 Base64/JSON/每次重试）
max_calls                    **每次** HTTP 尝试前
model_max_request_bytes      构建完整 Body 后、发送前
provider_max_clip_bytes      读取/构建请求前
===========================  ==================================================

先复现的缺陷：``VideoBudget.check()`` 只在**计划阶段**跑一次，覆盖
``max_segments`` / ``max_total_sec`` / ``max_total_bytes``；``max_calls`` 从
配置里读出来却**从未被使用**（死配置）。因此：

* 重试与并发会把实际发送量推过上限，而计划检查毫无察觉；
* 没有"已用多少"的运行台账，事后无法审计一次任务到底外发了多少字节/调用。

断言针对可观测差异：超限时是否在**第 N 次尝试之前**就抛出稳定错误码、
台账里的计数是否与真实尝试次数一致。
"""

from __future__ import annotations

import pytest

from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError
from app.services.vision.video_job import VideoBudget
from test_video_job import job_env  # noqa: F401


# --- V25: max_calls must actually be enforced -------------------------------


def test_v25_max_calls_is_enforced_at_all():
    """V25: ``max_calls`` was parsed and never used — that is the defect."""
    budget = VideoBudget(max_calls=3)
    for i in range(3):
        budget.note_attempt(calls=1, bytes_sent=10)
    with pytest.raises(ModelError) as exc:
        budget.note_attempt(calls=1, bytes_sent=10)
    assert exc.value.code == MODEL_BUDGET_EXCEEDED
    assert "call" in str(exc.value).lower()


def test_v25b_the_call_ledger_counts_every_attempt_including_retries():
    """V25: retries are attempts; they must be counted."""
    budget = VideoBudget(max_calls=2)
    budget.note_attempt(calls=1, bytes_sent=1)  # first try
    budget.note_attempt(calls=1, bytes_sent=1)  # retry
    assert budget.used_calls == 2
    with pytest.raises(ModelError):
        budget.note_attempt(calls=1, bytes_sent=1)


# --- V26: bytes must be checked before every attempt ------------------------


def test_v26_bytes_are_checked_before_each_attempt():
    """V26: the Base64 body is ~4/3 the clip, so per-attempt bytes matter."""
    budget = VideoBudget(max_total_bytes=100, max_calls=99)
    budget.note_attempt(calls=1, bytes_sent=60)
    # A second attempt would cross the cap; it must be refused *before* sending.
    with pytest.raises(ModelError) as exc:
        budget.note_attempt(calls=1, bytes_sent=60)
    assert exc.value.code == MODEL_BUDGET_EXCEEDED
    assert budget.used_bytes == 60, "a refused attempt must not be counted as sent"


def test_v26b_a_single_attempt_over_the_cap_is_refused():
    """V26: even the first attempt must be checked, not only the sum."""
    budget = VideoBudget(max_total_bytes=50, max_calls=9)
    with pytest.raises(ModelError):
        budget.note_attempt(calls=1, bytes_sent=51)


# --- V27: the ledger is auditable -------------------------------------------


def test_v27_the_ledger_reports_what_was_actually_sent():
    """V27: an auditor must be able to see calls/bytes without guessing."""
    budget = VideoBudget(max_calls=10, max_total_bytes=1000)
    budget.note_attempt(calls=1, bytes_sent=100)
    budget.note_attempt(calls=1, bytes_sent=250)
    ledger = budget.ledger()
    assert ledger["calls"] == 2
    assert ledger["bytes"] == 350
    assert ledger["remaining_calls"] == 8
    assert ledger["remaining_bytes"] == 650


def test_v27b_the_ledger_is_serialisable_and_carries_no_content():
    """V27: the ledger goes into job stats, so it must be plain data."""
    import json

    budget = VideoBudget(max_calls=5, max_total_bytes=500)
    budget.note_attempt(calls=1, bytes_sent=64)
    ledger = budget.ledger()
    assert json.loads(json.dumps(ledger)) == ledger
    for key in ("api_key", "token", "authorization", "path", "url"):
        assert key not in json.dumps(ledger).lower()


# --- V28: the existing plan-level checks still hold -------------------------


def test_v28_segment_and_duration_caps_still_enforced():
    """V28: adding the ledger must not weaken the plan-level checks."""
    budget = VideoBudget(max_segments=2, max_total_sec=10, max_total_bytes=10_000)
    with pytest.raises(ModelError):
        budget.check([(5.0, 100), (5.0, 100), (5.0, 100)])  # 3 segments
    with pytest.raises(ModelError):
        budget.check([(8.0, 100), (8.0, 100)])  # 16s
    with pytest.raises(ModelError):
        budget.check([(1.0, 9_000), (1.0, 9_000)])  # 18000 bytes


def test_v28b_actual_derived_duration_is_validated():
    """V28: the *encoder* decides the real clip length; check what came back."""
    budget = VideoBudget(max_segments=5, max_total_sec=6.0)
    # The plan said 4s total, but the derived clips are 5s and 5s.
    budget.check([(2.0, 10), (2.0, 10)])
    with pytest.raises(ModelError) as exc:
        budget.note_derived([(5.0, 10), (5.0, 10)])
    assert exc.value.code == MODEL_BUDGET_EXCEEDED


def test_v28c_derived_duration_within_plan_is_accepted():
    """V28: and a normal run must not trip the derived check."""
    budget = VideoBudget(max_segments=5, max_total_sec=6.0)
    budget.note_derived([(2.0, 10), (1.5, 10)])
    assert budget.derived_seconds == pytest.approx(3.5)


def test_v28d_a_plan_within_budget_still_passes():
    """V28: no over-refusal on the happy path."""
    budget = VideoBudget(max_segments=5, max_total_sec=30, max_total_bytes=10_000)
    totals = budget.check([(5.0, 1000), (5.0, 1000)])
    assert totals["segments"] == 2 and totals["total_bytes"] == 2000


# --- wiring: the send site must actually charge the ledger ------------------


def test_w5_the_send_site_charges_the_budget_before_calling_the_provider(
    job_env, monkeypatch
):
    """Wiring (§3.2): a ledger nobody calls is dead code.

    The production ``_analyze_one`` must charge ``plan.budget`` before it calls
    the provider. This asserts on the real source rather than on a
    re-implementation: the charge has to sit in the send path.
    """
    import inspect

    from app.services.vision import video_job as vj

    source = inspect.getsource(vj._analyze_one)
    assert "note_attempt(" in source, (
        "_analyze_one must charge the running budget before sending; otherwise "
        "max_calls and max_total_bytes are never enforced during a real run"
    )
    assert "note_derived(" in source, (
        "_analyze_one must re-check the *derived* clip duration against the cap"
    )
    # And the charge must precede the provider call in source order.
    assert source.index("note_attempt(") < source.index("complete_video("), (
        "the budget must be charged *before* the request is made, not after"
    )


def test_w6_a_job_that_exceeds_the_call_cap_stops_before_sending():
    """Wiring: once the cap is hit the attempt is refused with a stable code."""
    from app.services.vision import video_job as vj

    budget = vj.VideoBudget(max_calls=1, max_total_bytes=10_000)
    budget.note_attempt(calls=1, bytes_sent=10)
    with pytest.raises(ModelError) as exc:
        budget.note_attempt(calls=1, bytes_sent=10)
    assert exc.value.code == MODEL_BUDGET_EXCEEDED
    assert budget.used_bytes == 10, "nothing extra may be charged"


def test_w7_the_request_size_helper_counts_base64_inflation():
    """The charged size is the wire size, not the raw clip size."""
    from app.services.vision.video_job import _request_body_bytes

    class _Video:
        data = b"x" * 300

    class _Req:
        video = _Video()
        prompt = "describe this clip"

    wired = _request_body_bytes(_Req())
    raw = 300
    assert wired > raw, "Base64 + envelope must be larger than the raw clip"
    # Base64 is 4/3, so ~400 for the payload alone.
    assert wired >= (raw * 4) // 3


# ---------------------------------------------------------------------------
# TSB-347 — 预算必须在**每一次真实 HTTP 尝试**前预留
#
# 修复前的缺口：Job 层只在调用 Provider *之前* 记一次账，而 Provider 内部
# 有 `for attempt in range(1, max_retries + 2)` 重试循环。于是 max_retries=3 时
# 单个镜头最多发出 4 个请求，台账却只记 1 次 —— max_calls / max_total_bytes
# 对重试完全无效。
# ---------------------------------------------------------------------------


def _budget(**kw):
    import app.services.vision.video_job as vj

    base = dict(
        max_segments=10, max_total_sec=300.0, max_total_bytes=10_000_000,
        max_calls=10,
    )
    base.update(kw)
    return vj.VideoBudget(**base)


def test_v38_every_retry_is_charged(monkeypatch):
    """V38: Provider 内部每次重试都要单独计数（修复前只记 1 次）。"""
    from app.services.vision.providers import native_video as nv

    budget = _budget(max_calls=3, max_retries=5)
    attempts: list[int] = []

    def guard(attempt: int) -> None:
        attempts.append(attempt)
        budget.note_attempt(calls=1, bytes_sent=1000)

    # Simulate the provider's retry loop making three real attempts.
    for attempt in range(1, 4):
        guard(attempt)

    assert attempts == [1, 2, 3], "每次真实尝试都必须经过 guard"
    assert budget.ledger()["calls"] == 3, (
        f"三次真实 HTTP 尝试必须计 3 次，实际 {budget.ledger()['calls']}"
    )


def test_v38b_the_fourth_attempt_is_refused_before_sending():
    """V38b: 超出 max_calls 的那次尝试必须在发出**之前**被拒绝。"""
    from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError

    budget = _budget(max_calls=3)
    for _ in range(3):
        budget.note_attempt(calls=1, bytes_sent=1000)

    with pytest.raises(ModelError) as exc:
        budget.note_attempt(calls=1, bytes_sent=1000)
    assert exc.value.code == MODEL_BUDGET_EXCEEDED
    assert budget.ledger()["calls"] == 3, "被拒绝的尝试不得计数"


def test_v38c_retry_bytes_are_charged_every_attempt():
    """V38c: 每次重试的字节都要计入（重试不是免费的）。"""
    budget = _budget(max_total_bytes=2500)
    budget.note_attempt(calls=1, bytes_sent=1000)
    budget.note_attempt(calls=1, bytes_sent=1000)
    assert budget.ledger()["bytes"] == 2000

    from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError

    with pytest.raises(ModelError) as exc:
        budget.note_attempt(calls=1, bytes_sent=1000)
    assert exc.value.code == MODEL_BUDGET_EXCEEDED
    assert budget.ledger()["bytes"] == 2000, "被拒绝的尝试不得计入字节"


def test_v38d_the_provider_invokes_the_guard_before_each_http_post(monkeypatch):
    """V38d (接线): Provider 必须在每次 httpx.post **之前**调用 guard。"""
    import inspect

    from app.services.vision.providers import native_video as nv

    source = inspect.getsource(nv.NativeVideoVisionProvider.complete_video)
    assert "attempt_guard(" in source, "Provider 必须接受并调用 attempt_guard"
    guard_at = source.index("attempt_guard(")
    post_at = source.index("httpx.post(")
    assert guard_at < post_at, (
        "guard 必须出现在 httpx.post 之前：在请求之后计费等于没有计费"
    )


def test_v38e_the_job_passes_the_guard_to_the_provider():
    """V38e (接线): Job 调用点必须把 guard 传进 complete_video。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj)
    assert "attempt_guard=charge_attempt" in source, (
        "调用点必须传入 attempt_guard，否则 Provider 的重试仍然绕过预算"
    )


def test_v38f_derived_seconds_accumulate_across_clips():
    """V38f: 派生时长必须**逐镜头累积**（修复前只保留最后一个镜头）。"""
    budget = _budget(max_total_sec=300.0)
    for _ in range(10):
        budget.note_derived([(5.0, 1000)])

    assert budget.ledger()["derived_seconds"] == 50.0, (
        f"十个 5 秒镜头必须累计为 50 秒，实际 "
        f"{budget.ledger()['derived_seconds']}（修复前是 5.0）"
    )
    assert budget.ledger()["derived_bytes"] == 10000


def test_v38g_cumulative_derived_seconds_trip_the_limit():
    """V38g: 累积超过 max_total_sec 必须报 MODEL_BUDGET_EXCEEDED。"""
    from app.services.vision.errors import MODEL_BUDGET_EXCEEDED, ModelError

    budget = _budget(max_total_sec=12.0)
    budget.note_derived([(5.0, 1000)])
    budget.note_derived([(5.0, 1000)])
    with pytest.raises(ModelError) as exc:
        budget.note_derived([(5.0, 1000)])  # 15s > 12s
    assert exc.value.code == MODEL_BUDGET_EXCEEDED


def test_v38h_the_ledger_reports_remaining_seconds():
    """V38h: 台账必须能回答「还剩多少秒」，便于事后审计。"""
    budget = _budget(max_total_sec=100.0)
    budget.note_derived([(30.0, 500)])
    ledger = budget.ledger()
    assert ledger["remaining_seconds"] == 70.0
    assert ledger["limits"]["max_total_sec"] == 100.0
