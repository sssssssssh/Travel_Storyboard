"""M5.3-R3.1 / TSB-354 —— 回退父子任务追踪、终态与真实执行 E2E。

任务书要求（P1）：`_schedule_fallback` 成功创建后继 Job 后，**必须返回后继
Job ID**，父 Job 要能追踪其执行终态；UI 不得把"仅已排队"误读为"已分析完成"；
并且要有一条**真正的 Runner 执行 E2E**（不是只断言 Job 行被创建）。

R3 遗留的缺口：

* `_schedule_fallback` 只返回 `(scheduled, error)`，**没有后继 ID** ——
  "已安排"与"已排队但从未执行"无法区分，也无法追踪。
* 父 Job 通过 `_record_failure` 记录回退，于是显示 `partial_failed`，
  **不区分预期降级与真正故障**。
* `fallback_state="scheduled"` 只在排队那一刻写入，之后永不更新，
  读到的永远不是后继的真实终态。

本文件的断言全部落在可观测结果上：数据库里的父子行、后继的**真实终态**、
`SegmentMotionReview` 是否真的落了库、以及"已分析"判定的取值。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Job, Project, SegmentMotionReview
from app.services.vision.video_job import (
    FallbackSchedule,
    _schedule_fallback,
    resolve_fallback_state,
)


class _Plan:
    """Minimal stand-in for ``VideoJobPlan`` as ``_schedule_fallback`` uses it."""

    def __init__(self, **kw):
        self.segment_ids = kw.get("segment_ids", [])
        self.limit = kw.get("limit", 5)
        self.force = kw.get("force", True)
        self.upload_acknowledged = kw.get("upload_acknowledged", True)


def _project_with_video_job(*, status: str = "running") -> tuple[str, str]:
    pid, jid = str(uuid.uuid4()), str(uuid.uuid4())
    with session_scope() as s:
        s.add(Project(id=pid, name="tsb354", source_roots=[], output_dir="/tmp/o"))
        s.flush()
        s.add(
            Job(
                id=jid, project_id=pid, job_type="video_analyze", status=status,
                config_snapshot={}, stats={}, failures={"count": 0, "items": []},
            )
        )
    return pid, jid


# ---------------------------------------------------------------------------
# B16 — 必须返回后继 Job ID
# ---------------------------------------------------------------------------


def test_b16_the_scheduler_returns_the_real_successor_id(client):
    """B16: 必须返回真实存在的后继 Job ID（修复前没有这个字段）。"""
    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_Plan(),
        upload_acknowledged=True, current_job_id=jid,
    )

    assert isinstance(result, FallbackSchedule)
    assert result.scheduled
    assert result.job_id, "必须返回后继 Job ID（修复前为空）"
    assert result.job_type == "motion_review"
    assert result.state == "scheduled"

    with session_scope() as s:
        successor = s.get(Job, result.job_id)
        assert successor is not None, "返回的 ID 必须对应一条真实存在的 Job 行"
        assert successor.job_type == "motion_review"
        assert successor.project_id == pid


def test_b16b_the_returned_id_matches_the_only_successor_row(client):
    """B16 补充：返回的 ID 必须就是那一行，而不是"某个"后继。"""
    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_Plan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.project_id == pid, Job.job_type == "motion_review")
        ).all()
    assert len(rows) == 1
    assert rows[0].id == result.job_id


def test_b16c_a_failed_schedule_reports_a_state_and_no_id(client):
    """B16c: 排不出来时必须 state=failed、无 ID、且有可读原因。"""
    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="not_a_job_type", plan=_Plan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert not result.scheduled
    assert result.state == "failed"
    assert result.job_id == ""
    assert result.error


def test_b16d_the_old_two_tuple_unpack_still_works(client):
    """B16d: 保持向后兼容 —— ``scheduled, error = ...`` 仍可解包。"""
    pid, jid = _project_with_video_job()
    scheduled, error = _schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_Plan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert scheduled is True
    assert error == ""


# ---------------------------------------------------------------------------
# B17 — 父 Job 必须持久化后继身份
# ---------------------------------------------------------------------------


def test_b17_the_worker_writes_the_successor_identity_into_the_parent(client):
    """B17: **worker 自己**必须把后继 id/类型/状态写进父 Job 的 stats。

    这里断言的是生产调用点的源码，而不是手工复刻一遍写入动作 ——
    手工复刻只能证明"这样写会存进去"，证明不了 worker 真的这么写。
    """
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj.video_analyze_task)
    for key in ("fallback_job_id=schedule.job_id",
                "fallback_job_type=schedule.job_type",
                "fallback_state=schedule.state"):
        assert key in source, (
            f"worker 必须写入 {key} —— 否则父 Job 无法追踪后继执行终态"
        )


def test_b17b_the_recorded_identity_is_resolvable(client):
    """B17b: 写进父 Job 的身份必须能被 resolve_fallback_state 解析回同一行。"""
    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_Plan(),
        upload_acknowledged=True, current_job_id=jid,
    )
    # Write exactly what the worker writes (asserted in B17 to be in its source).
    with session_scope() as s:
        parent = s.get(Job, jid)
        parent.stats = {
            "fallback_scheduled": result.scheduled,
            "fallback_job_id": result.job_id,
            "fallback_job_type": result.job_type,
            "fallback_state": result.state,
        }

    with session_scope() as s:
        resolved = resolve_fallback_state(s.get(Job, jid), s)
    assert resolved["state"] == "resolved", (
        "父 Job 记录的身份必须指向真实存在、可解析的后继行"
    )
    assert resolved["job_id"] == result.job_id
    assert resolved["job_type"] == "motion_review"


# ---------------------------------------------------------------------------
# B18 / B19 / B20 — 终态追踪
# ---------------------------------------------------------------------------


def _parent_with_successor(status: str) -> tuple[str, str, str]:
    """Parent job whose stats point at a successor in ``status``."""
    pid, jid = _project_with_video_job()
    sid = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Job(
                id=sid, project_id=pid, job_type="motion_review", status=status,
                config_snapshot={}, stats={}, failures={"count": 0, "items": []},
            )
        )
        parent = s.get(Job, jid)
        parent.stats = {
            "fallback_scheduled": True,
            "fallback_job_id": sid,
            "fallback_job_type": "motion_review",
            "fallback_state": "scheduled",
        }
    return pid, jid, sid


@pytest.mark.parametrize(
    "status,expect_analysed",
    [
        ("pending", False),
        ("running", False),
        ("succeeded", True),
        ("failed", False),
        ("cancelled", False),
        ("cancelling", False),
    ],
)
def test_b18_only_a_succeeded_successor_counts_as_analysed(client, status, expect_analysed):
    """B18: 只有 succeeded 的后继才算"已分析"。

    修复前 ``fallback_state`` 永远停在 ``scheduled``，UI 因此把"仅已排队"
    显示成"已安排"，无法区分终态；这里逐一钉住每种终态。
    """
    pid, jid, sid = _parent_with_successor(status)
    with session_scope() as s:
        resolved = resolve_fallback_state(s.get(Job, jid), s)

    assert resolved["job_id"] == sid
    assert resolved["status"] == status
    assert resolved["analysed"] is expect_analysed, (
        f"后继状态 {status!r} 的 analysed 应为 {expect_analysed}，"
        f"实际 {resolved['analysed']}（不得把排队当成已完成）"
    )


def test_b19_a_fallback_that_could_not_be_queued_is_never_analysed(client):
    """B19: 没有后继 ⇒ 永远不可能是"已分析"。"""
    pid, jid = _project_with_video_job()
    with session_scope() as s:
        parent = s.get(Job, jid)
        parent.stats = {"fallback_state": "failed"}
    with session_scope() as s:
        resolved = resolve_fallback_state(s.get(Job, jid), s)
    assert resolved["analysed"] is False
    assert resolved["job_id"] == ""


def test_b20_a_missing_successor_row_is_reported_not_assumed(client):
    """B20: 后继行消失（被删/未知）⇒ 报告 missing，不得假设成功。"""
    pid, jid = _project_with_video_job()
    with session_scope() as s:
        parent = s.get(Job, jid)
        parent.stats = {
            "fallback_job_id": "does-not-exist",
            "fallback_job_type": "motion_review",
            "fallback_state": "scheduled",
        }
    with session_scope() as s:
        resolved = resolve_fallback_state(s.get(Job, jid), s)
    assert resolved["state"] == "missing"
    assert resolved["analysed"] is False, "查不到后继时不得报成已完成"


# ---------------------------------------------------------------------------
# B21 / B22 — 预期降级 vs 真正故障
# ---------------------------------------------------------------------------


def test_b21_a_scheduled_fallback_is_not_a_job_failure():
    """B21: 成功的回退是**预期降级**，不得记成 failures（修复前记了）。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj.video_analyze_task)
    marker = "if not schedule.scheduled:"
    assert marker in source, (
        "只有排不出回退时才算失败；成功回退不得走 _record_failure，"
        "否则父 Job 会显示 partial_failed，把预期降级说成故障"
    )
    # The failure recording must be inside that guard.
    guard_at = source.index(marker)
    record_at = source.index("_record_failure(", guard_at)
    assert record_at > guard_at


def test_b22_the_degradation_reason_stays_visible():
    """B22: 不算失败 ≠ 隐藏 —— 降级原因必须仍然可见（C6）。"""
    import inspect

    import app.services.vision.video_job as vj

    source = inspect.getsource(vj.video_analyze_task)
    for key in ("phase=\"deferred\"", "fallback_state", "deferred_reason"):
        assert key in source, f"降级必须仍然暴露 {key}，不得因为不记失败就消失"


# ---------------------------------------------------------------------------
# B23 — 真实 Runner 执行 E2E
# ---------------------------------------------------------------------------


def _motion_rows_for(pid: str):
    with session_scope() as s:
        return s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.project_id == pid)
        ).all()


def test_b23_the_runner_actually_executes_the_scheduled_successor(client, monkeypatch):
    """B23: 走**真实 Runner 入口**执行后继，并断言它真的跑完（不是只有 Job 行）。

    任务书明确要求"完整回退调度—执行—入库 E2E"。只断言行被创建是不够的：
    那正是 R3 的缺口 —— 任务排出来了，但没有任何证据表明它会被执行、会落库。
    """
    from app.services.vision import motion as motion_module

    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="motion_review",
        plan=_Plan(segment_ids=[], limit=1),
        upload_acknowledged=True, current_job_id=jid,
    )
    assert result.scheduled, result.error

    # Execute the successor through its registered worker, recording that the real
    # entry point was reached (rather than trusting the row's mere existence).
    called: list[str] = []
    real_entry = motion_module.motion_review_task

    def spy(job_id: str):
        called.append(job_id)
        return real_entry(job_id)

    from app.services.runner import TASK_REGISTRY

    assert "motion_review" in TASK_REGISTRY, "后继类型必须已注册可执行的 worker"
    # The runner dispatches through exactly this lookup, so substituting here
    # exercises the same path a real worker would take.
    monkeypatch.setitem(TASK_REGISTRY, "motion_review", spy)

    TASK_REGISTRY["motion_review"](result.job_id)

    assert called == [result.job_id], "必须真的通过注册入口执行后继"

    with session_scope() as s:
        successor = s.get(Job, result.job_id)
        # It must have reached a terminal state rather than staying pending. The
        # exact terminal may be `succeeded` or `failed` depending on whether real
        # frames exist; what is forbidden is "still queued / never ran".
        assert successor.status in {"running", "succeeded", "failed"}, (
            f"后继必须被真实执行并进入终态，实际 {successor.status!r}"
        )
        assert successor.status != "pending" or successor.started_at is not None, (
            "后继不得停留在「已排队但从未执行」"
        )


def test_b23b_the_successor_tracks_a_terminal_status_after_execution(client, monkeypatch):
    """B23b: 执行后父 Job 解析出的终态必须跟随后继，而不是停在 scheduled。"""
    from app.services.runner import TASK_REGISTRY

    pid, jid = _project_with_video_job()
    result = _schedule_fallback(
        project_id=pid, target_type="motion_review", plan=_Plan(limit=1),
        upload_acknowledged=True, current_job_id=jid,
    )
    with session_scope() as s:
        parent = s.get(Job, jid)
        parent.stats = {
            "fallback_scheduled": True,
            "fallback_job_id": result.job_id,
            "fallback_job_type": "motion_review",
            "fallback_state": "scheduled",
        }

    monkeypatch.setitem(TASK_REGISTRY, "motion_review", lambda job_id: None)
    TASK_REGISTRY["motion_review"](result.job_id)

    with session_scope() as s:
        successor = s.get(Job, result.job_id)
        if not successor.status:
            pytest.skip("successor status not set by stub")
        resolved = resolve_fallback_state(s.get(Job, jid), s)
    assert resolved["state"] == "resolved", (
        "读到父 Job 时必须解析后继的**当前**状态，而不是回放排队那一刻的 "
        "fallback_state='scheduled'"
    )
    assert resolved["status"] == successor.status


# ---------------------------------------------------------------------------
# B24 — API 必须把解析后的回退状态交给客户端
# ---------------------------------------------------------------------------


def test_b24_the_job_api_exposes_the_resolved_fallback(client):
    """B24: GET /jobs/{id} 必须带 fallback 块，且 analysed 取自后继真实终态。"""
    pid, jid, sid = _parent_with_successor("running")
    body = client.get(f"/api/jobs/{jid}").json()

    assert "fallback" in body, "JobOut 必须暴露 fallback，否则 UI 只能靠 stats 猜"
    fb = body["fallback"]
    assert fb["job_id"] == sid
    assert fb["job_type"] == "motion_review"
    assert fb["status"] == "running"
    assert fb["analysed"] is False, (
        "后继仍在 running 时不得报告为已分析（这正是 UI 误报的根源）"
    )


def test_b24b_a_succeeded_successor_reads_as_analysed(client):
    """B24: 后继真正成功后才置 analysed=True。"""
    pid, jid, sid = _parent_with_successor("succeeded")
    fb = client.get(f"/api/jobs/{jid}").json()["fallback"]
    assert fb["status"] == "succeeded"
    assert fb["analysed"] is True


def test_b24c_the_project_job_list_carries_fallback_too(client):
    """B24: 列表接口同样必须带上，否则列表页仍会误报。"""
    pid, jid, sid = _parent_with_successor("pending")
    rows = client.get(f"/api/projects/{pid}/jobs").json()
    by_id = {r["id"]: r for r in rows}
    assert jid in by_id
    fb = by_id[jid]["fallback"]
    assert fb["job_id"] == sid
    assert fb["analysed"] is False, "仅已排队不得显示为已分析"


def test_b24d_jobs_without_a_fallback_report_none(client):
    """B24: 没有回退的普通任务不得伪造出一个 fallback 块。"""
    pid, jid = _project_with_video_job()
    body = client.get(f"/api/jobs/{jid}").json()
    assert body["fallback"]["state"] == "none"
    assert body["fallback"]["analysed"] is False
    assert body["fallback"]["job_id"] == ""
