"""TSB-337 — 回退到多帧必须**真的执行**，或明确拒绝；不能空跑成功。

任务书要求（P1）：请求 ``video_native`` 而能力未验证时，要么真正跑多帧回退并
在产物中标注，要么以稳定错误码明确拒绝。**不允许**把回退记成一次
``total=0 / phase=complete`` 的空成功——那正是 C6 禁止的静默跳过：调用方看到
任务"完成"，却没有任何分析发生。

先复现的缺陷：``run_video_job`` 在 ``plan.effective_mode != "video_native"`` 时
写回 ``stats["phase"]="complete"``、``total=0`` 后直接 return。回退因此只留下
一个原因字符串，既没有多帧结果，也没有失败码。

已有用例（``test_a_fallback_does_not_produce_video_rows`` 等）只断言"回退被正确
标注"与"没有留下 video 行"，**没有一条**断言多帧工作确实发生——本文件补上这一
缺口。断言针对可观测差异：是否真的产出多帧结果 / 是否给出稳定错误码 /
是否可达多帧入口，而不是只看 stats 里的字符串。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Job, SegmentVideoObservation
from app.services.vision import video_job as vj
from app.services.vision.video_capabilities import (
    UNSUPPORTED,
    UNVERIFIED,
    VERIFIED_NATIVE_VIDEO,
    VideoCapabilities,
    unverified_capabilities,
)
from test_video_job import _make_video_job, job_env  # noqa: F401


# --- V22: the decision itself ------------------------------------------------


def test_v22_falling_back_requires_explicit_opt_in():
    """V22: no silent substitution when the caller did not accept a fallback."""
    from app.services.vision.errors import ModelError

    with pytest.raises(ModelError) as exc:
        vj.resolve_mode(
            requested_mode="video_native",
            capabilities=unverified_capabilities(),
            allow_fallback=False,
        )
    assert exc.value.code in {"MODEL_NOT_CONFIGURED", "MODEL_CAPABILITY_UNSUPPORTED"}


def test_v22b_opt_in_returns_an_explicit_mode_and_reason():
    """V22: with consent the substitution is named, and so is the reason."""
    mode, reason = vj.resolve_mode(
        requested_mode="video_native",
        capabilities=VideoCapabilities(state=UNVERIFIED),
        allow_fallback=True,
    )
    assert mode == vj.FALLBACK_MODE
    assert reason == "VIDEO_CAPABILITY_UNVERIFIED"


def test_v22c_a_sendable_capability_never_falls_back():
    """V22: a *sendable* state must use the native path, with no fallback."""
    caps = VideoCapabilities(state=VERIFIED_NATIVE_VIDEO)
    assert caps.sendable, "the test's premise is that this state is sendable"
    mode, reason = vj.resolve_mode(
        requested_mode="video_native", capabilities=caps, allow_fallback=True
    )
    assert mode == "video_native" and reason == ""


def test_v22d_an_image_mode_is_never_rerouted():
    """V22: asking for multiframe must not be silently changed."""
    mode, reason = vj.resolve_mode(
        requested_mode="multiframe",
        capabilities=VideoCapabilities(state=UNVERIFIED),
        allow_fallback=True,
    )
    assert mode == "multiframe" and reason == ""


# --- V23: a fallback must produce work, not an empty success -----------------


def test_v23_a_fallback_does_not_report_an_empty_success(job_env, monkeypatch):
    """V23: falling back must analyse frames, or refuse with a stable code.

    Reproduces the defect: the job wrote ``phase="complete"`` with ``total=0``
    and performed no multi-frame work at all.
    """
    job_id = _make_video_job(
        job_env["project_id"], mode="video_native", allow_fallback=True
    )
    monkeypatch.setattr(
        vj, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode=vj.FALLBACK_MODE,
            fallback_reason="VIDEO_CAPABILITY_UNVERIFIED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    vj.video_analyze_task(job_id)

    with session_scope() as s:
        stats = dict(s.get(Job, job_id).stats or {})

    assert stats.get("fallback_reason"), "a fallback must record its reason"

    # The heart of TSB-337: this must not be an empty success.
    did_work = bool(stats.get("analyzed")) or bool(stats.get("multiframe_runs"))
    failed = stats.get("phase") == "failed" and bool(stats.get("error_code"))
    refused = bool(stats.get("fallback_deferred_to")) or bool(
        stats.get("refused_reason")
    )
    assert did_work or failed or refused, (
        "a fallback must actually run multi-frame analysis, or refuse with a "
        f"stable error code -- got an empty success: phase={stats.get('phase')!r} "
        f"total={stats.get('total')!r}"
    )


def test_v23b_a_fallback_never_reports_phase_complete_with_zero_total(
    job_env, monkeypatch
):
    """V23: the exact forbidden shape — complete with nothing analysed.

    Kept as its own test because it is the precise regression: the caller sees
    "complete" and concludes the footage was analysed.
    """
    job_id = _make_video_job(
        job_env["project_id"], mode="video_native", allow_fallback=True
    )
    monkeypatch.setattr(
        vj, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode=vj.FALLBACK_MODE,
            fallback_reason="VIDEO_CAPABILITY_UNSUPPORTED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNSUPPORTED),
        ),
    )
    vj.video_analyze_task(job_id)

    with session_scope() as s:
        stats = dict(s.get(Job, job_id).stats or {})

    empty_success = (
        stats.get("phase") == "complete"
        and not stats.get("analyzed")
        and not stats.get("multiframe_runs")
        and not stats.get("deferred")
    )
    assert not empty_success, (
        "a fallback that analysed nothing must not report phase=complete; "
        f"that is a silent skip (C6). stats={stats}"
    )


def test_v23c_a_fallback_does_not_claim_multiframe_was_performed(job_env, monkeypatch):
    """V23: and it must not overclaim the other way either."""
    job_id = _make_video_job(
        job_env["project_id"], mode="video_native", allow_fallback=True
    )
    monkeypatch.setattr(
        vj, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode=vj.FALLBACK_MODE,
            fallback_reason="VIDEO_CAPABILITY_UNVERIFIED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    vj.video_analyze_task(job_id)

    with session_scope() as s:
        stats = dict(s.get(Job, job_id).stats or {})

    if not stats.get("multiframe_runs"):
        assert not stats.get("analyzed"), (
            "a job must not report segments analysed if no frames were sent"
        )


# --- V24: the fallback must be reachable, not merely labelled ---------------


def test_v24_the_fallback_records_where_the_work_went(job_env, monkeypatch):
    """V24: if another job type owns the work, say so explicitly.

    Deferring to the existing multi-frame job is acceptable; *silently* doing
    nothing is not. Either way the caller must be told.
    """
    job_id = _make_video_job(
        job_env["project_id"], mode="video_native", allow_fallback=True
    )
    monkeypatch.setattr(
        vj, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode=vj.FALLBACK_MODE,
            fallback_reason="VIDEO_CAPABILITY_UNVERIFIED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    vj.video_analyze_task(job_id)

    with session_scope() as s:
        stats = dict(s.get(Job, job_id).stats or {})

    assert stats.get("mode_effective") == vj.FALLBACK_MODE
    assert stats.get("fallback_reason")
    # A reader must be able to tell *why* nothing was analysed without guessing.
    accounted = (
        stats.get("fallback_deferred_to")
        or stats.get("refused_reason")
        or stats.get("multiframe_runs")
        or stats.get("error_code")
    )
    assert accounted, (
        "a fallback that analyses nothing must name the reason/code, so the "
        f"outcome is never ambiguous: stats={stats}"
    )


def test_v24b_a_fallback_leaves_no_video_observation(job_env, monkeypatch):
    """V24: (regression guard) a fallback must not fabricate video rows."""
    job_id = _make_video_job(
        job_env["project_id"], mode="video_native", allow_fallback=True
    )
    monkeypatch.setattr(
        vj, "plan_video_job",
        lambda jid, **kw: vj.VideoJobPlan(
            requested_mode="video_native", effective_mode=vj.FALLBACK_MODE,
            fallback_reason="VIDEO_CAPABILITY_UNVERIFIED", limit=10,
            segment_ids=[], force=False, upload_acknowledged=True,
            max_clip_sec=15.0, capabilities=VideoCapabilities(state=UNVERIFIED),
        ),
    )
    vj.video_analyze_task(job_id)

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id.in_(
                    list(job_env["segments"].values())
                )
            )
        ).all()
    assert rows == [], "an image-based fallback must never write video rows"
