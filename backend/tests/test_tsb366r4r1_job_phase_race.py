"""TSB-366-R4-R1 / R1-A —— V31 偶发 `phase=running` 的确定性回归。

## 真实失败断言（CI run 42234893140，Ubuntu Required FFmpeg）

```text
tests/test_video_e2e_formal_api.py:125
    assert stats.get("phase") in {"complete", "failed"}, stats.get("phase")
E   AssertionError: running
```

前置断言（Mock 收到请求、Observation 已持久化）**都已通过**，所以不是
"Mock 没收到请求"。

## 机制（见 docs/validation/tsb366-r4-r1/BASELINE.md F2–F5）

1. `job_service.py:238` 在 API 返回前就 `runner.submit(job.id)`，
   受控实验显示 API 一返回该 Job 的后台 future 已经 `state=running`；
2. V31 随后又手动 `_run_task(api_job_id)` —— 同一 Job 有**两个执行者**；
3. `video_analyze_task()` 对 `stats` 是**整体覆盖**写入，`phase` 非单调：
   `starting → running → complete` 之后，另一个执行者再写 `running`
   就把 `complete` 盖掉，于是 Job 停在 `running`。

本文件的用例把这三步变成**确定性**断言，不依赖概率复现。
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session_scope
from app.models import Job, Project


def _make_job(*, status: str = "running") -> tuple[str, str]:
    pid, jid = str(uuid.uuid4()), str(uuid.uuid4())
    with session_scope() as s:
        s.add(Project(id=pid, name="r1a", source_roots=[], output_dir="/tmp/r1a"))
        s.flush()
        s.add(
            Job(
                id=jid, project_id=pid, job_type="video_analyze", status=status,
                config_snapshot={}, stats={}, failures={"count": 0, "items": []},
            )
        )
    return pid, jid


def _write_phase(jid: str, phase: str) -> None:
    """Write the phase through the **production** path.

    Writing the row directly would bypass the guard under test and make these
    cases prove nothing (they would pass or fail regardless of the fix).
    `update_job(stats=...)` is exactly what `video_analyze_task` calls.
    """
    from app.services.runner import update_job

    with session_scope() as s:
        current = dict(s.get(Job, jid).stats or {})
    current["phase"] = phase
    update_job(jid, stats=current)


def _phase(jid: str) -> str:
    with session_scope() as s:
        return str((s.get(Job, jid).stats or {}).get("phase", ""))


# ---------------------------------------------------------------------------
# A-3 — 确定性双执行者竞争
# ---------------------------------------------------------------------------


def test_a3_a_late_running_write_cannot_overwrite_a_terminal_phase(client):
    """核心回归：终态写入之后，迟到的 `running` 不得把它盖回运行中。

    修复前：`starting → running → complete → running` 之后，Job 读到的
    `phase` 是 `running` —— 与 CI 失败断言完全一致。
    修复后：一旦写入终态（complete/failed/cancelled），后续的 `running`
    写入必须被忽略，Job 保持终态。
    """
    _pid, jid = _make_job()

    _write_phase(jid, "starting")
    _write_phase(jid, "running")
    _write_phase(jid, "complete")   # first executor finishes
    _write_phase(jid, "running")    # second executor's later stage (late write)

    final = _phase(jid)
    assert final in {"complete", "failed"}, (
        f"Job 停在 {final!r}；终态被迟到的 running 覆盖了"
        f"（CI 失败的正是这一形态）"
    )


def test_a3b_a_cancelled_job_also_keeps_its_terminal_phase(client):
    """取消终态同样不可被迟到的 `running` 覆盖。"""
    _pid, jid = _make_job(status="cancelling")
    _write_phase(jid, "running")
    _write_phase(jid, "cancelled")
    _write_phase(jid, "running")
    assert _phase(jid) == "cancelled"


def test_a3c_the_terminal_phase_is_idempotent(client):
    """重复写同一终态必须保持不变（幂等），不得被降级。"""
    _pid, jid = _make_job()
    _write_phase(jid, "running")
    _write_phase(jid, "complete")
    _write_phase(jid, "complete")
    assert _phase(jid) == "complete"


def test_a3d_non_terminal_transitions_still_progress(client):
    """反向约束：终态保护不能把正常的阶段性推进也挡住。

    若实现成"第一次写入后就不再接受后续写入"，这里会失败 ——
    那会把 `starting` 永久钉住，同样违反状态机合同。
    """
    _pid, jid = _make_job()
    _write_phase(jid, "starting")
    assert _phase(jid) == "starting"
    _write_phase(jid, "running")
    assert _phase(jid) == "running"
    _write_phase(jid, "complete")
    assert _phase(jid) == "complete"


# ---------------------------------------------------------------------------
# A-3 反向验证入口 —— 生产实现的契约
# ---------------------------------------------------------------------------


def test_a3e_the_job_stats_writer_is_phase_monotonic():
    """接线：生产代码写 `stats.phase` 时必须经过单调保护。

    这条锁住"修复真的接在写入路径上"（AGENTS.md §3.2：底层支持了，
    但生产调用点没接上，缺陷照旧）。它断言的是**可观测的源码事实**：
    视频任务不得再直接 `stats["phase"] = "running"` 后无条件整体写回。
    """
    import inspect

    import app.services.vision.video_job as vj

    src = inspect.getsource(vj.video_analyze_task)
    # Either the task routes phase writes through the guarded helper, or it
    # stops overwriting stats wholesale. Both are acceptable implementations;
    # a bare `stats["phase"] = "running"` + `update_job(stats=stats)` is not.
    guarded = "set_phase(" in src or "merge_job_stats(" in src
    bare_overwrite = 'stats["phase"] = "running"' in src and "update_job(job_id, stats=dict(stats))" in src
    assert guarded or not bare_overwrite, (
        "视频任务必须通过带单调保护的 helper 写 phase，"
        "不能继续整体覆盖 stats 后把终态盖回 running"
    )
