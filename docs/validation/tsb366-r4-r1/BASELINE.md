# TSB-366-R4-R1 阶段 0 基线核对

## 1. 开工基线

| 项 | 值 |
| --- | --- |
| 实测 HEAD | `afeefe3b6789abc94c98234b7f092afef6f2b300` |
| 实测 origin/main | `afeefe3b6789abc94c98234b7f092afef6f2b300`（**一致**） |
| 分支 | `main` |
| 工作区 | **clean**（`git status --short` 空） |
| 任务书参考基线 | `afeefe3` —— **逐字符一致** |

## 2. CI 三轮对照（历史，仅作参考）

| run | SHA | 结果 | 说明 |
| --- | --- | --- | --- |
| 42233008689 | `3508aab`（R4 代码） | 7/7 success | 同一 V31 用例通过 |
| 42234893140 | `0fc9a96`（**仅改 docs**） | 6/7 | Ubuntu Required FFmpeg 的 V31 失败 |
| 42238125461 | `0d834d2`（空提交重跑） | success | 重跑通过 |

**重跑通过不构成"偶发已根除"的证据**（任务书 §1.2、§4 明确要求）。

## 3. 已证实的事实

### F1. V31 失败的真实断言（任务书 §0 已给出，本轮核对一致）

```text
tests/test_video_e2e_formal_api.py:125
    assert stats.get("phase") in {"complete", "failed"}, stats.get("phase")
E   AssertionError: running
```

**不是**"Mock 未收到请求"。CI 日志显示前置断言
（Mock 收到请求、Observation 已持久化）**均已通过**。
`test_video_e2e_formal_api.py:125` 正是 `assert stats.get("phase") in {"complete","failed"}`。

> 旁注：我在上一轮 R4 的交付报告里把这次失败描述成
> `the Mock model server received no request` —— 那是**我本机并行压测时**
> 看到的一种失败形态，与 CI 的真实断言不同。本轮以任务书给出的真实
> 断言为准，并已据此重新定位。

### F2. 生产代码**确实**在 API 返回前自动提交任务

`backend/app/services/job_service.py:238`（在 `_create_locked()` 内）：

```python
    runner.submit(job.id)
    return job
```

`backend/app/services/runner.py:420-435`：

```python
    def submit(self, job_id: str) -> None:
        with self._submit_lock:
            future = self._futures.get(job_id)
            if future is not None and not future.done():
                return                       # <-- 只是防同一 job 重复入池
            ...
            self._futures[job_id] = self._pool().submit(self._run, job_id, task)
```

### F3. 受控实验：API 返回后后台 future **正在运行**

实验（在 `tests/` 内跑，使用真实 `client` 与真实 `runner`，只对
`runner.submit` 加 spy 记录）：

```text
scan create -> 202 ec5206fe-27f6-433f-a64d-1a6dcdca310c
runner.submit called with: ['ec5206fe-27f6-433f-a64d-1a6dcdca310c']
runner._futures keys: ['ec5206fe-27f6-433f-a64d-1a6dcdca310c']
future for this job: <Future at 0x120ab7050 state=running>
future.done() at this instant: False
```

⇒ **API 一返回，该 Job 已经有一个后台线程在跑。**
V31 随后又：

```python
    _VideoServer.seen = []
    monkeypatch.setattr(module, "plan_video_job", ...)
    with session_scope() as s:
        s.get(Job, api_job_id).status = "running"      # 手动改状态
    _run_task(api_job_id)                              # 手动执行第二次
```

⇒ 同一 Job 被**两个执行者**处理：后台 `runner._run()` 与测试线程 `_run_task()`。

### F4. 为什么两个执行者会让 `phase` 停在 `running`

`video_analyze_task()` 内部**多次**整体覆盖 `stats`：

| 行 | 写入 |
| --- | --- |
| ~592 | `stats["phase"] = "starting"` → `update_job(stats=stats)` |
| ~672 | `stats["phase"] = "running"` → `update_job(stats=stats)` |
| ~675 | `stats["phase"] = "complete"` → `update_job(stats=stats)` |

`phase` 是**非单调**的：每次都整体覆盖，没有"终态一旦写入就不再回退"的保护。
两个执行者的写入交错即可复现：

### F5. 受控交错复现（确定性，非概率）

按 `starting → running → complete → running` 顺序写同一 Job 的 `stats.phase`：

```text
  FINAL phase = running
  AssertionError: stuck at running
  assert 'running' in {'complete', 'failed'}
```

⇒ **`phase=running` 可由"两个执行者交错写 `stats`"完整解释**，且与 CI 失败
断言一字不差。这是确定性复现，不依赖概率。

### F6. 屏障只在 teardown 生效，管不到 test body

`tests/conftest.py::_no_background_jobs_left_behind` 在 **yield 之后**才
`drain_background_jobs()`。V31 的断言发生在 **test body 内**，
此时后台 future 可能仍在执行 ⇒ 屏障帮不上忙。

## 4. 尚待确认 / 本轮不下结论的部分

| 项 | 状态 |
| --- | --- |
| CI 上两个执行者**具体**在哪个指令点交错 | **未逐指令证实**。本机 25/25 串行未复现原始交错；F5 证明的是"该交错足以造成此症状"，不是"CI 那一次必然是这条路径" |
| 是否存在生产侧缺陷（非仅测试） | 需按任务书 §2.3 判断：若生产不会有两次执行，则属**测试设计**缺陷；`runner.submit()` 对同一 job 有 `future.done()` 防重入，但**不阻止**外部（测试）另起一次执行 |
| 其它可能（Mock handler 共享、fixture 生命周期、CI 环境） | F3/F4/F5 已给出**充分**机制；其它因素未排除，但无需引入即可解释现象 |

## 5. 原始日志位置

| 内容 | 位置 |
| --- | --- |
| 任务书给出的 CI 失败断言 | 任务书 §0（本文 F1 转录） |
| 受控实验原始输出 | 本文 F3 / F5 |
| 本机 R4 期间观察到的另一种失败形态 | `docs/validation/tsb366-r4/`（已注明与 CI 真实断言不同） |

## 6. 路径覆盖缺口（R1-B 依据）

R4 新增的 `test_tsb366r4_video_long_paths.py` 是**契约层**测试
（断言访问是否经过 `safe_*`），**没有**一条在 Windows 上真正构造
≥260 字符的**派生 MP4** 走完整原生视频链路。这是 R1-B 要补的缺口。
