# TSB-366-R4-R1 根因分析

## R1-A：V31 偶发 `phase=running`

### 真实失败断言（CI run 42234893140，Ubuntu Required FFmpeg）

```text
tests/test_video_e2e_formal_api.py:125
    assert stats.get("phase") in {"complete", "failed"}, stats.get("phase")
E   AssertionError: running
```

**不是**"Mock 未收到请求"。该次运行中前置断言
（Mock 收到请求、Observation 已持久化）**均已通过**。

> 更正：我在 R4 交付报告里把这次失败写成
> `the Mock model server received no request` —— 那是我本机**并行压测**时
> 的一种失败形态，与 CI 的真实断言不同。本轮以任务书的真实断言为准。

---

### 机制（三步，全部有证据）

#### ① API 返回前就已自动提交任务 —— 已证实

`backend/app/services/job_service.py:238`（`_create_locked()` 内）：

```python
    runner.submit(job.id)
    return job
```

`runner.submit()`（`runner.py:420`）把 `self._run` 提交进
`ThreadPoolExecutor`，并登记 `self._futures[job_id]`。

**受控实验**（真实 `client` + 真实 `runner`，只对 `submit` 加 spy）：

```text
scan create -> 202 ec5206fe-27f6-433f-a64d-1a6dcdca310c
runner.submit called with: ['ec5206fe-27f6-433f-a64d-1a6dcdca310c']
runner._futures keys: ['ec5206fe-27f6-433f-a64d-1a6dcdca310c']
future for this job: <Future at 0x120ab7050 state=running>
future.done() at this instant: False
```

⇒ **API 一返回，该 Job 已经有一个后台线程在跑。**

#### ② 测试又手动执行了第二次 —— 已证实（代码事实）

修复前 V31：

```python
    with session_scope() as s:
        s.get(Job, api_job_id).status = "running"      # 手动改状态
    _run_task(api_job_id)                              # 第二个执行者
```

⇒ 同一 Job 被 `runner._run()` 与测试线程**两个执行者**处理。

#### ③ `stats.phase` 非单调，后写者覆盖 —— 已证实

`video_analyze_task()` 对 `stats` 是**整体覆盖**写入：

| 位置 | 写入 |
| --- | --- |
| ~592 | `stats["phase"] = "starting"` → `update_job(stats=stats)` |
| ~672 | `stats["phase"] = "running"` → `update_job(stats=stats)` |
| ~675 | `stats["phase"] = "complete"` → `update_job(stats=stats)` |

`update_job()` 对 `Job.status` 有终态不可变保护，但**对 `stats.phase` 没有**。
于是两个执行者交错：

```text
starting → running → complete → running     （第二次的 running 盖掉 complete）
```

**受控交错复现（确定性，非概率）**：

```text
  FINAL phase = running
  AssertionError: stuck at running
  assert 'running' in {'complete', 'failed'}
```

与 CI 失败断言**一字不差**。

#### ④ 屏障帮不上忙 —— 已证实

`conftest._no_background_jobs_left_behind` 在 **yield 之后**才
`drain_background_jobs()`。V31 的断言发生在 **test body 内**，
此时后台 future 可能仍在跑。

---

### 根因定性

**测试设计缺陷为主，生产状态机缺一条不变量为辅。**

| 层 | 缺陷 | 是否生产问题 |
| --- | --- | --- |
| 测试 | V31 在 API 自动提交后又手动执行同一 Job | ✅ 测试设计（主因） |
| 测试 | 断言发生在 test body，未等待 API 已调度的 future | ✅ 测试设计 |
| 生产 | `stats.phase` 无单调保护，迟到写入可把终态盖回 `running` | ✅ 生产不变量缺失（使测试缺陷表现为"假运行中"） |

生产是否会**自发**出现双执行者？
`runner.submit()` 用 `future.done()` 防同一 job 重复入池，
**不阻止**外部另起一次执行。本轮未发现生产自发的双执行路径，
因此**没有**重构 runner 的锁或执行器（任务书 §2.3、§5 要求）。

---

### 修复

**1) 生产：`stats.phase` 终态不可变（`runner.py`）**

新增 `TERMINAL_PHASES = ("complete", "failed", "cancelled")` 与
`_phase_is_terminal()`；在 `update_job()` 写 `stats` 前拦截：

```python
if _phase_is_terminal(stored) and not _phase_is_terminal(incoming):
    fields["stats"] = {**(fields["stats"] or {}), "phase": stored}
```

与已有的 `Job.status` 终态不可变规则**同构**。
保留 payload 其余内容，迟到的写入者无法抹掉完成记录。

**2) 测试：V31 改为单一执行者（`test_video_e2e_formal_api.py`）**

- 删掉"手动改 `running` + `_run_task()`"；
- 用 `drain_background_jobs()` 等待 **API 已调度**的那个 future 到终态；
- 新增执行权归属断言：`finished_at is not None` + `status` 必须终态。

**没有**把 `assert stats.phase in {"complete","failed"}` 放宽成接受
`running`，**没有**加 sleep，**没有**重试。

---

## R1-B：Windows 深路径原生视频 E2E

### 缺口（R4 遗留）

R4 新增的 `test_tsb366r4_video_long_paths.py` 是**契约层**测试
（断言访问是否经过 `safe_*`），**没有**一条在 Windows 上真正构造
≥260 字符的**派生 MP4** 走完整原生视频链路。

### 本轮补的测试

`backend/tests/test_tsb366r4r1_native_video_longpath_e2e.py`：

| 断言 | 内容 |
| --- | --- |
| `LongPathsEnabled` | Windows 上读注册表真实值并输出；读不到即失败（不许默默跳过） |
| 派生路径长度 | 断言 `len(str(clip.path)) >= 260`（**实际**产物，不是 planned path） |
| 兼容层可读 | `safe_is_file` / `safe_stat` / `safe_open` |
| probe 合同 | 重新 `probe_clip()`：无音轨、size、SHA-256、duration、宽高 |
| 字节完整性 | `read bytes` 长度 == `clip.size_bytes`，SHA 相等 |
| Mock 收到 | loopback server 恰好 1 次请求，body 含**完整** MP4 的 base64 |
| 原片不变 | SHA-256 / size / mtime 三者前后一致 |
| cleanup | 自有 staging 清理 `ok=True`，派生文件消失 |

平台说明：Windows 上这是**唯一**真正覆盖 MAX_PATH 的证据；
POSIX 上同一套业务断言也执行（路径同样真实 ≥260），
但只证明业务链路，**不**证明 Windows 长路径兼容 —— 报告中分开陈述。

### 本机实测（macOS，非 Windows 证据）

```text
  [paths] project_cache_dir len=361
  [clip]  path len=394
  [platform] Darwin — NO MAX_PATH here; business-chain evidence only
```

### 反向证据

`test_the_compat_helpers_are_what_keep_a_deep_clip_readable`：
在 Windows 上断言普通 `Path.stat` **必然失败**而 `safe_stat` 成功
（直接复现 R4 之前的缺陷）；POSIX 上明确标注为不适用，不伪装成通过。

---

## 验收漏覆盖（本轮发现并修复）

`tests/test_tsb366r3_package_deep.py` **不在** CI 强制媒体清单中
（`grep -c` = 0）—— R3 的深路径 Package 回归**从未在 CI 真实执行过**。
本轮已加入清单。任务书 §7 要求：这类情况必须标为**验收漏覆盖**，
不得用以前的 CI 7/7 冒充已通过。
