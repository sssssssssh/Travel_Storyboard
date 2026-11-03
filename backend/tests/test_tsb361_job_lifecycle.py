"""TSB-361 §4.3 — 后台 Job 生命周期与 tracker 隔离回归（A01–A08）。

缺陷 A 的现场形态是**跨用例**的：前一个用例结束后，它提交到模块级 runner 上的
``motion_review`` 子任务仍在跑；等它终于调用 ``motion.extract_frame`` 时，落进的
是**后一个用例**刚装的 tracker（``test_b01_...`` 的 ``e.extracted``），于是断言里
出现别人的 ``src.mp4``。

因此这里的用例大多是**成对**的：前一个故意不等、把真实 Job 留在 runner 上，后一个
断言上一轮的 Job 已经是终态、而它自己的 tracker 干净。只断言"fixture 被调用过"是
不够的——每个场景用的都是真实的 Job 行、真实的注册任务和真实的 runner。

同文件内的执行顺序由 pytest 按定义顺序保证；后一个用例在前一个没有执行时会显式
失败，不会因为乱序而静默通过。
"""

from __future__ import annotations

import threading
import time

import pytest

import conftest
from app.database import session_scope
from app.models import Job, Project
from app.services import runner as runner_mod

#: 相邻两个用例之间唯一的共享物。它与生产代码无关，只用来把"上一个用例留下的
#: 东西"交给下一个用例检查；用模块级字典而不是 fixture，正是因为要跨越 teardown。
_STATE: dict[str, dict] = {}

#: 模拟"进程级全局 tracker"：缺陷 A 里被晚到事件写脏的就是这种东西。
_TRACKERS: dict[str, list | None] = {"active": None}


def _job(job_id: str):
    with session_scope() as s:
        row = s.get(Job, job_id)
        assert row is not None, f"Job {job_id} 不存在"
        return {
            "id": row.id,
            "status": row.status,
            "error": row.error or "",
            "stats": dict(row.stats or {}),
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }


def _new_job(job_type: str, project_id: str | None = None) -> tuple[str, str]:
    """Insert a real, runnable Job row (and its project) the runner can pick up."""
    with session_scope() as s:
        if project_id is None:
            project = Project(name="tsb361-lifecycle")
            s.add(project)
            s.flush()
            project_id = project.id
        job = Job(
            project_id=project_id, job_type=job_type, config_snapshot={},
            stats={}, failures={"count": 0, "items": []},
        )
        s.add(job)
        s.flush()
        return project_id, job.id


@pytest.fixture()
def probe(monkeypatch):
    """Register a real task type and submit it on the *global* runner.

    The global runner is the one the product uses, and the one a test that calls
    a task function directly (``video_analyze_task(job_id)``) ends up leaking
    work onto. Using a private ``JobRunner()`` here would test nothing.
    """

    def submit(name: str, worker, *, project_id: str | None = None) -> tuple[str, str]:
        job_type = f"tsb361_probe_{name}"
        monkeypatch.setitem(runner_mod.TASK_REGISTRY, job_type, worker)
        pid, jid = _new_job(job_type, project_id)
        runner_mod.runner.submit(jid)
        return pid, jid

    return submit


def _previous(key: str) -> dict:
    state = _STATE.get(key)
    if state is None:
        pytest.fail(
            f"{key.upper()} 必须整文件运行：前一个用例没有执行，本用例无从验证。"
            "单独运行本用例不算验证（任务书 §10 特别要求）。"
        )
    return state


# ---------------------------------------------------------------------------
# A01 — 测试创建后台 Job 后正常结束 ⇒ teardown 等待 Job 终态
# ---------------------------------------------------------------------------


def test_a01_a_a_test_returns_while_its_job_is_still_running(probe):
    """A01 前：留下一个**真的还在跑**的后台 Job 就返回。"""
    finished = threading.Event()

    def worker(job_id):
        time.sleep(0.25)  # 保证 Job 跨越测试边界，而不是靠运气
        runner_mod.update_job(job_id, stats={"probe": "a01-finished"})
        finished.set()

    _pid, jid = probe("a01", worker)
    _STATE["a01"] = {"jid": jid, "finished": finished}
    assert not finished.is_set(), "前提：Job 在本用例体内必须尚未结束"


def test_a01_b_teardown_waited_for_the_real_terminal_state():
    """A01 后：Job 是 ``succeeded``（真跑完），不是被兜底取消掉的 ``cancelled``。"""
    state = _previous("a01")
    row = _job(state["jid"])
    assert state["finished"].is_set(), "teardown 必须在 worker 真正退出后才放行"
    assert row["status"] == "succeeded", row
    assert row["stats"].get("probe") == "a01-finished", "worker 的统计必须落库"
    assert row["started_at"] is not None and row["finished_at"] is not None


# ---------------------------------------------------------------------------
# A02 — 前序 Job 延迟产生抽帧事件 ⇒ 不得污染后续测试
# ---------------------------------------------------------------------------


def test_a02_a_a_job_emits_a_frame_event_after_the_test_body(probe):
    """A02 前：Job 在测试体返回**之后**才发事件（真实抽帧发生在异步阶段）。"""
    tracker = []
    _TRACKERS["active"] = tracker

    def worker(job_id):
        time.sleep(0.25)
        # 缺陷现场：事件写进"当前"tracker，而不是发起它的那个用例的 tracker。
        _TRACKERS["active"].append({"job_id": job_id, "phase": "extract_frame"})

    _pid, jid = probe("a02", worker)
    _STATE["a02"] = {"jid": jid, "tracker": tracker}


def test_a02_b_the_late_event_landed_in_its_own_test_not_here():
    """A02 后：晚到事件属于上一个用例，本用例的 tracker 一条都不能有。"""
    state = _previous("a02")
    mine = []
    _TRACKERS["active"] = mine
    events = [e for e in state["tracker"] if e["job_id"] == state["jid"]]
    assert len(events) == 1 and events[0]["phase"] == "extract_frame", state["tracker"]
    assert mine == [], f"晚到事件污染了后续用例的 tracker: {mine}"


# ---------------------------------------------------------------------------
# A03 — 两个测试并发运行 ⇒ tracker 完全隔离
# ---------------------------------------------------------------------------


def test_a03_two_concurrent_jobs_keep_their_own_trackers(probe):
    """A03：两个 Job 真的同时执行，各自的事件只落到自己的 tracker。"""
    # 不同的 project ⇒ 不同的 project_lock，这才是"并发"而不是排队。
    trackers: dict[str, list] = {"a": [], "b": []}
    both_running = threading.Barrier(2, timeout=15)

    def make(name):
        def worker(job_id):
            both_running.wait()  # 两个 Job 必须同时在跑，否则本用例没有意义
            for index in range(5):
                trackers[name].append((job_id, index))
                time.sleep(0.01)
        return worker

    projects = []
    with session_scope() as s:
        for name in ("a", "b"):
            project = Project(name=f"tsb361-a03-{name}")
            s.add(project)
            s.flush()
            projects.append(project.id)
    jobs = [probe(name, make(name), project_id=pid)[1]
            for name, pid in zip(("a", "b"), projects)]

    leftover = conftest.drain_background_jobs()
    assert leftover == [], f"并发 Job 未能收敛: {leftover}"

    rows = [_job(jid) for jid in jobs]
    assert [row["status"] for row in rows] == ["succeeded", "succeeded"], rows
    for name, jid in zip(("a", "b"), jobs):
        seen = trackers[name]
        assert len(seen) == 5, seen
        assert {job_id for job_id, _ in seen} == {jid}, "tracker 里出现了别人的事件"
    assert {e[0] for e in trackers["a"]}.isdisjoint({e[0] for e in trackers["b"]})


# ---------------------------------------------------------------------------
# A04 — 后台任务执行中取消 ⇒ 等待真正退出
# ---------------------------------------------------------------------------


def test_a04_a_a_running_job_is_asked_to_stop(probe):
    """A04 前：Job 正在执行时请求取消，然后立刻返回。"""
    entered, exited = threading.Event(), threading.Event()

    def worker(job_id):
        entered.set()
        while not runner_mod.is_cancelled(job_id):
            time.sleep(0.01)
        exited.set()

    _pid, jid = probe("a04", worker)
    assert entered.wait(15), "Job 没有开始执行"
    assert runner_mod.update_job(jid, status="cancelling"), "取消请求必须被接受"
    _STATE["a04"] = {"jid": jid, "exited": exited}


def test_a04_b_cancel_waited_for_the_worker_to_actually_leave():
    """A04 后：worker 真的退出了，行落在 ``cancelled``，且没有晚到写入。"""
    state = _previous("a04")
    row = _job(state["jid"])
    assert state["exited"].is_set(), "取消后必须等 worker 自己退出，而不是先写终态"
    assert row["status"] == "cancelled", row
    assert row["started_at"] is not None and row["finished_at"] is not None


# ---------------------------------------------------------------------------
# A05 — 测试主体异常退出 ⇒ teardown 仍执行清理
# ---------------------------------------------------------------------------


def test_a05_cleanup_runs_even_when_the_body_fails(probe):
    """A05：主体断言失败时，屏障照样把后台 Job 收干净。

    直接驱动屏障 fixture 的生成器：pytest 在用例失败后对 fixture 做的仍然是
    "正常推进到 ``yield`` 之后"，与用例体的成败无关。这里复刻同一步骤，并在
    "失败"之后再检查 Job 是否真的收敛。
    """
    barrier = getattr(conftest._no_background_jobs_left_behind, "__wrapped__",
                      conftest._no_background_jobs_left_behind)
    generator = barrier()
    next(generator)  # fixture setup（相当于用例开始）
    try:
        finished = threading.Event()

        def worker(job_id):
            time.sleep(0.15)
            finished.set()

        _pid, jid = probe("a05", worker)
        with pytest.raises(AssertionError):
            raise AssertionError("模拟用例主体失败")
        # 主体已"失败"，回收仍必须发生——这就是 pytest 对 fixture 的实际行为。
        with pytest.raises(StopIteration):
            next(generator)
    finally:
        generator.close()

    row = _job(jid)
    assert finished.is_set(), "主体失败不得跳过 Job 收敛"
    assert row["status"] == "succeeded", row
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# A06 — Job 完成后晚到回调 ⇒ 不得写入已结束测试的 tracker
# ---------------------------------------------------------------------------


def test_a06_a_a_done_callback_fires_after_the_test_body(probe):
    """A06 前：给 future 挂一个完成回调，它在用例体之后才触发。"""
    tracker = []
    _TRACKERS["active"] = tracker

    def worker(job_id):
        time.sleep(0.25)

    _pid, jid = probe("a06", worker)
    future = runner_mod.runner._futures[jid]
    future.add_done_callback(lambda _f: _TRACKERS["active"].append({"late": jid}))
    _STATE["a06"] = {"jid": jid, "tracker": tracker}


def test_a06_b_the_late_callback_did_not_reach_this_test():
    """A06 后：回调落在发起它的用例里，没有写进后来者的 tracker。"""
    state = _previous("a06")
    mine = []
    _TRACKERS["active"] = mine
    assert mine == [], f"完成回调写进了错误的 tracker: {mine}"
    assert [e["late"] for e in state["tracker"]] == [state["jid"]]


# ---------------------------------------------------------------------------
# A07 — 后台任务抛出异常 ⇒ 错误可见，不得假装成功
# ---------------------------------------------------------------------------


def test_a07_a_a_background_job_raises(probe):
    """A07 前：后台 Job 抛异常，用例体不等它。"""

    def worker(job_id):
        time.sleep(0.1)
        raise RuntimeError("synthetic probe failure")

    _pid, jid = probe("a07", worker)
    _STATE["a07"] = {"jid": jid}


def test_a07_b_the_failure_is_visible_and_never_a_fake_success():
    """A07 后：行是 ``failed``、错误原文可见、终态时间存在。"""
    state = _previous("a07")
    row = _job(state["jid"])
    assert row["status"] == "failed", row
    assert "synthetic probe failure" in row["error"], row
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# A08 — 多次连续执行同一测试 ⇒ 结果一致
# ---------------------------------------------------------------------------


def test_a08_repeating_the_same_scenario_yields_the_same_outcome(probe):
    """A08：同一场景连续跑 3 轮，终态、统计与收敛性每次完全一致。"""
    outcomes = []
    for round_index in range(3):
        def worker(job_id, index=round_index):
            time.sleep(0.05)
            runner_mod.update_job(job_id, stats={"round": index})

        _pid, jid = probe(f"a08_{round_index}", worker)
        leftover = conftest.drain_background_jobs()
        assert leftover == [], f"第 {round_index} 轮残留后台执行: {leftover}"
        row = _job(jid)
        outcomes.append((row["status"], row["stats"].get("round"),
                         row["started_at"] is not None, row["finished_at"] is not None))

    assert outcomes == [
        ("succeeded", 0, True, True),
        ("succeeded", 1, True, True),
        ("succeeded", 2, True, True),
    ], outcomes


# ---------------------------------------------------------------------------
# A09 — 补充：**尚未派发**的行也不能被下一个测试的启动恢复拉起来
# ---------------------------------------------------------------------------


def test_a09_a_pending_row_is_never_resumed_into_a_later_test():
    """问题 B 的另一半机制：遗留的 ``pending`` 行会被下一个用例"恢复"执行。

    ``app.main.lifespan`` 每次启动都调用 ``resume_interrupted_jobs()``，而
    ``client`` fixture 每个用例启动一次 lifespan。直接调用任务函数的用例
    （``video_analyze_task(job_id)``）从不把行改成 ``running``，于是下一个用例的
    启动会把它当作"上一个进程崩溃遗留"重新派发，并在**那个用例体内**执行。

    这里验证屏障的收尾：遗留行被落成终态之后，恢复扫描不再选中它。
    """
    _pid, jid = _new_job("tsb361_probe_never_dispatched")  # 从未 submit 过
    assert _job(jid)["status"] == "pending"
    settled = conftest.settle_unfinished_jobs("a09")
    assert jid in settled, settled
    assert _job(jid)["status"] == "cancelled"
    assert jid not in runner_mod.resume_interrupted_jobs(), "终态行不能被当作中断任务恢复"
