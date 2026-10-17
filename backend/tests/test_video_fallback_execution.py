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

import tempfile

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


# ---------------------------------------------------------------------------
# TSB-346 — 悬空链接不得被当作「从未创建」而静默跳过
# ---------------------------------------------------------------------------


def test_v37_a_dangling_link_is_not_reported_as_never_created():
    """V37: 悬空链接占用着路径 => 不得记为 "stage was never created"。

    旧实现用 ``Path(stage).exists()``，它会**跟随链接**：指向不存在目标的
    符号链接返回 False，于是被归类为「路径从未出现」并以
    ``{'ok': True, 'note': 'stage was never created'}`` 略过 —— 既是 C6 的
    静默跳过，也让这个残留链接永远无人清理、无人报告。
    """
    import os
    from pathlib import Path

    import app.services.vision.video_job as vj

    tmp = Path(tempfile.mkdtemp())
    dangling = tmp / "staging-dangling"
    os.symlink(tmp / "does-not-exist", dangling)

    assert not Path(dangling).exists(), "前提：Path.exists() 跟随链接，悬空时为 False"
    assert vj.path_occupies_disk(dangling), (
        "悬空链接占用着路径，必须交给清理器处理，而不是记为从未创建"
    )


def test_v37b_an_absent_path_is_still_reported_as_never_created():
    """V37b: 真正不存在的路径仍记为「从未创建」（不把审计本身弄坏）。"""
    from pathlib import Path

    import app.services.vision.video_job as vj

    tmp = Path(tempfile.mkdtemp())
    assert not vj.path_occupies_disk(tmp / "never-created")


def test_v37c_a_real_directory_is_reported_as_occupying_disk():
    """V37c: 正常目录必须被交给清理器（防止过度跳过）。"""
    from pathlib import Path

    import app.services.vision.video_job as vj

    tmp = Path(tempfile.mkdtemp())
    (tmp / "real").mkdir()
    assert vj.path_occupies_disk(tmp / "real")


def test_v37d_the_cleanup_site_uses_the_link_aware_check():
    """V37d (接线): 清理点必须用 link-aware 判断，而不是 Path.exists()。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj)
    assert "path_occupies_disk(" in source, "清理点必须使用 link-aware 判断"
    # The old follow-the-link form must be gone from the cleanup loop.
    assert "if not Path(stage).exists():" not in source, (
        "Path.exists() 会跟随链接，无法区分「悬空链接」与「从未创建」"
    )


# ---------------------------------------------------------------------------
# TSB-348 — allow_fallback 必须**真正调度**多帧任务，而不只是记一个 deferred
#
# 修复前：回退分支记录 phase="deferred" 与 fallback_deferred_to 后直接 return，
# 没有任何东西被排队。从用户视角这仍是静默空转：项目永远不会被分析，
# 也没有任何地方说得出"接下来会发生什么"（C6）。
# ---------------------------------------------------------------------------


class _FakePlan:
    def __init__(self, **kw):
        self.segment_ids = kw.get("segment_ids", ["seg-a"])
        self.limit = kw.get("limit", 7)
        self.force = kw.get("force", True)
        self.upload_acknowledged = kw.get("upload_acknowledged", True)


def _project_with_running_video_job(client):
    """A project plus a `running` video_analyze job (the fallback's predecessor)."""
    import uuid as _uuid

    from app.database import session_scope
    from app.models import Job, Project

    pid = str(_uuid.uuid4())
    with session_scope() as s:
        s.add(Project(id=pid, name="tsb348", source_roots=[], output_dir="/tmp/o"))
        s.flush()
        job = Job(
            project_id=pid, job_type="video_analyze", status="running",
            config_snapshot={}, stats={}, failures={"count": 0, "items": []},
        )
        s.add(job)
        s.flush()
        return pid, job.id


def test_v39_a_fallback_actually_schedules_the_motion_review_job(client):
    """V39: 回退必须真的排出一个 motion_review 任务（修复前一个都没有）。"""
    from sqlalchemy import select

    import app.services.vision.video_job as vj
    from app.database import session_scope
    from app.models import Job

    pid, jid = _project_with_running_video_job(client)
    scheduled, error = vj._schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_FakePlan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert scheduled, f"回退必须真正排队，实际失败：{error}"

    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.project_id == pid, Job.job_type == "motion_review")
        ).all()
    assert len(rows) == 1, (
        f"回退必须恰好排出一个 motion_review 任务，实际 {len(rows)}（修复前为 0）"
    )


def test_v39b_the_scheduled_fallback_carries_the_plan_selection(client):
    """V39b: 排出的任务必须分析同一批镜头，而不是整个项目。"""
    from sqlalchemy import select

    import app.services.vision.video_job as vj
    from app.database import session_scope
    from app.models import Job

    pid, jid = _project_with_running_video_job(client)
    vj._schedule_fallback(
        project_id=pid, target_type="motion_review",
        plan=_FakePlan(segment_ids=["s1", "s2"], limit=3),
        upload_acknowledged=True, current_job_id=jid,
    )
    with session_scope() as s:
        row = s.scalars(
            select(Job).where(Job.project_id == pid, Job.job_type == "motion_review")
        ).one()
        config = dict(row.config_snapshot)
    assert config["segment_ids"] == ["s1", "s2"], "必须沿用计划选中的镜头"
    assert config["limit"] == 3


def test_v39c_a_foreign_live_job_still_blocks_the_fallback(client):
    """V39c: 只有自己的前驱可以豁免；其他活跃互斥任务仍必须冲突。

    这是防止 TSB-348 的豁免被滥用的关键断言 —— 互斥语义不得被削弱。
    """
    import uuid as _uuid

    from sqlalchemy import select

    import app.services.vision.video_job as vj
    from app.database import session_scope
    from app.models import Job

    pid, jid = _project_with_running_video_job(client)
    with session_scope() as s:
        s.add(
            Job(
                project_id=pid, job_type="vision_analyze", status="running",
                config_snapshot={}, stats={}, failures={"count": 0, "items": []},
            )
        )
    scheduled, error = vj._schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_FakePlan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert not scheduled, "存在其他活跃互斥任务时必须冲突，而不是照常排队"
    assert "JobConflict" in error

    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.project_id == pid, Job.job_type == "motion_review")
        ).all()
    assert rows == [], "冲突时不得留下半排队的任务"


def test_v39d_only_the_own_predecessor_is_exempted(client):
    """V39d: 豁免必须精确到单个 job id，不能放宽成"任意 video_analyze"。"""
    import inspect

    from app.services import job_service

    source = inspect.getsource(job_service._create_locked)
    assert "allow_own_job_id" in source, "必须显式命名可豁免的前驱"
    assert "j.id != allow_own_job_id" in source, (
        "豁免必须按 job id 精确匹配，不能按 job_type 放宽"
    )


def test_v39e_an_unknown_fallback_target_is_reported_not_swallowed(client):
    """V39e: 无法排队的回退必须报告原因，而不是静默失败（C6）。"""
    import app.services.vision.video_job as vj

    pid, jid = _project_with_running_video_job(client)
    scheduled, error = vj._schedule_fallback(
        project_id=pid, target_type="not_a_real_job_type", plan=_FakePlan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert not scheduled
    assert error, "必须给出可读的失败原因"


def test_v39f_the_worker_threads_its_own_job_id_into_the_scheduler():
    """V39f (接线): 调用点必须把自己的 job id 传进去，否则会自锁冲突。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj.video_analyze_task)
    assert "_schedule_fallback(" in source, "回退分支必须调用调度函数"
    assert "current_job_id=job_id" in source, (
        "必须传入自己的 job id，否则会被自己的 running 状态判为冲突而永远排不出来"
    )
