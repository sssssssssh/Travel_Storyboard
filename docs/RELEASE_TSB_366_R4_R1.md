# TSB-366-R4-R1 发布记录：视频任务生命周期稳定性与 Windows 深路径端到端验收

**参考基线（任务书）：** `afeefe3b6789abc94c98234b7f092afef6f2b300`
**实测开工 SHA：** `afeefe3b6789abc94c98234b7f092afef6f2b300`（与 `origin/main` 一致，工作区干净）
**最终代码 SHA：** `224e154`
**CI：** [run 42392563283](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42392563283) @ `224e154` — **7/7 success**（含 windows-latest）
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg+ffprobe 9.0.2

> **本机是 macOS，没有 MAX_PATH。** 本机一切绿色只证明业务链路与"没有回归"，
> **不证明** Windows 长路径行为。Windows 证据只来自 CI 的 windows-latest。

---

## 一、两项任务的结论

| 任务 | 结论 |
| --- | --- |
| **R1-A** V31 偶发 `phase=running` | **已定位根因**（测试双执行者 + 生产 `stats.phase` 无单调保护），已修复，有确定性 RED/GREEN 与反向验证 |
| **R1-B** Windows ≥260 字符派生 MP4 E2E | **已新增并通过 CI 强制媒体清单**；Windows 实际执行证据见 CI |
| 附带发现① | `test_tsb366r3_package_deep.py` **从未进入 CI 强制媒体清单** —— 属**验收漏覆盖**，本轮补入 |
| 附带发现② | 补入后该套件在 windows-latest 连续暴露 **2 处生产缺陷**（见第五节），已修复 |

---

## 二、基线核对

| 项 | 值 |
| --- | --- |
| 实测 HEAD / origin/main | `afeefe3` / `afeefe3`（**一致**） |
| 分支 | `main` |
| 工作区 | **clean** |
| 与任务书参考基线 | **逐字符一致** |

CI 三轮对照（历史，仅参考）：`3508aab` 7/7 success · `0fc9a96`（仅 docs）6/7（V31 红）· `0d834d2` success。
**重跑成功不作为"偶发已根除"的证据。**

---

## 三、R1-A：V31 根因

### 3.1 真实失败断言

```text
tests/test_video_e2e_formal_api.py:125
    assert stats.get("phase") in {"complete", "failed"}, stats.get("phase")
E   AssertionError: running
```

前置断言（Mock 收到请求、Observation 已持久化）**均已通过**。

> **自我更正**：R4 报告里我把它写成 "Mock 未收到请求"，那是我本机**并行压测**
> 时的另一种形态，与 CI 真实断言不同。本轮按任务书断言重新定位。

### 3.2 机制（三步均有证据）

**① API 返回前已自动提交** —— `job_service.py:238` `runner.submit(job.id)`。
受控实验（真实 client + 真实 runner + submit spy）：

```text
scan create -> 202 ...
runner.submit called with: ['ec5206fe-...']
future for this job: <Future at 0x... state=running>
future.done() at this instant: False
```

**② 测试又手动执行第二次** —— 旧 V31 手动改 `status="running"` 后 `_run_task(api_job_id)`。

**③ `stats.phase` 非单调** —— `update_job()` 对 `Job.status` 有终态保护，
对 `stats.phase` **没有**；`video_analyze_task` 整体覆盖 `stats`。
交错 `starting→running→complete→running` 即停在 `running`：

```text
  FINAL phase = running
  AssertionError: stuck at running
```

**确定性复现，与 CI 断言一字不差。**

### 3.3 根因定性

| 层 | 缺陷 |
| --- | --- |
| **测试（主因）** | API 自动提交后又手动执行同一 Job；断言未等待已调度的 future |
| **生产（不变量缺失）** | `stats.phase` 无终态保护，迟到写入可把 `complete` 盖回 `running` |

未发现生产**自发**双执行路径（`runner.submit` 有 `future.done()` 防重入），
因此**没有**重构 runner 锁或执行器配置。

### 3.4 修复

1. **生产** `runner.py`：新增 `TERMINAL_PHASES`/`_phase_is_terminal()`，
   `update_job()` 写 `stats` 前拦截"终态→非终态"，与 `Job.status` 规则同构。
2. **测试** V31：删除"手动改 running + `_run_task`"，改为
   `drain_background_jobs()` 等待 **API 已调度**的 future；新增执行权归属断言
   （`finished_at is not None` + 终态 status）。

**未**放宽 `assert phase in {"complete","failed"}`，**未**加 sleep/重试。

### 3.5 RED → GREEN

| 阶段 | 结果 |
| --- | --- |
| 修复前（走生产 `update_job`） | **2 RED**：`Job 停在 'running'`、`assert 'running' == 'cancelled'` |
| 修复后 | **5/5 GREEN** |
| 反向（移除 phase 守卫） | **2 RED** → 恢复后 5/5 |
| 反向（恢复旧 V31 双执行者） | V31 **RED**：`finished_at is None` |

---

## 四、R1-B：Windows 深路径 E2E

新增 `backend/tests/test_tsb366r4r1_native_video_longpath_e2e.py`（2 项）。

### 4.1 断言矩阵

| 编号 | 断言 |
| --- | --- |
| B-1 | Windows 读回 `LongPathsEnabled` 真实值；读不到即**失败**（不默默跳过） |
| B-1 | `len(str(clip.path)) >= 260` —— 断言**实际派生产物**，非 planned path |
| B-2 | `safe_is_file` / `safe_stat` / `safe_open` 均可读 |
| B-2 | 重新 `probe_clip()`：无音轨、size、SHA-256、duration、宽高 |
| B-2 | 读取字节长度 == `clip.size_bytes`，SHA-256 相等 |
| B-3 | loopback Mock 恰好收到 1 次请求，body 含**完整** MP4 的 base64 |
| B-4 | 原片 SHA-256 / size / mtime 前后一致 |
| B-4 | 自有 staging cleanup `ok=True`，派生文件消失 |

### 4.2 本机实测（macOS —— **非** Windows 证据）

```text
  [paths] project_cache_dir len=361
  [clip]  path len=394
  [platform] Darwin — NO MAX_PATH here; business-chain evidence only
```

### 4.3 反向证据

`test_the_compat_helpers_are_what_keep_a_deep_clip_readable`：
Windows 上断言普通 `Path.stat` **必然失败**而 `safe_stat` 成功
（直接复现 R4 之前的缺陷）；POSIX 上明确标注不适用，不伪装成通过。

---

## 五、补入强制清单后暴露的 2 处生产缺陷

把 R3 Package 深路径套件放进 CI 后，windows-latest 连续报错，
追下去都是**生产代码**问题（不是测试写法）：

### P-1 ZIP 是空档：`pkg_dir.rglob("*")` 在深路径下枚举不到任何文件

`review_package._build_package` 用 `Path.rglob` 收集要入 ZIP 的文件。
`rglob` 走**普通** Win32 目录 API，`LongPathsEnabled=0` 时在深路径下一个都
枚举不到 —— ZIP 变成空档，而每个文件其实都已被 `long_path` 正常写盘
（所以 `result["frames"] >= 1` 通过，但 `zf.namelist()` 为空）。

CI run 42389405936：`AssertionError: []`（message 是空列表）。

**修复**：改用 `walk_files(pkg_dir)`（内部 `safe_scandir` + `long_path`），
ZIP 写入与 `total_size` 复用同一份枚举结果。

### P-2 扩展长度前缀泄漏：`walk_files` 原样 yield 长路径形式

修完 P-1 后立刻暴露：`safe_scandir` 回退到 `long_path()` 时，
`DirEntry.path` 带扩展长度前缀；`walk_files` 原样 yield，前缀于是泄漏进
`Path.relative_to(...)`。

CI run 42390622837：`ValueError: '<prefix>C:\Users\runneradmin\...'
is not in the subpath of ...`

扩展长度形式是兼容层的**内部细节**，既不该写库也不该进 API
（AGENTS.md 要求）。**修复**：`walk_files` 增加 `_plain()` 归一化，
剥离前缀后再 yield（内部仍用前缀完成实际 IO）。

两条都新增了平台无关回归锁（源码事实断言 + 前缀泄漏断言），并做了反向验证。

---

## 六、验收漏覆盖修复（任务书 §3.4.2）

`tests/test_tsb366r3_package_deep.py` 在 CI 强制媒体清单中命中数为 **0**
—— **R3 的深路径 Package 回归从未在 CI 真实执行过**。
本轮已将其与新 E2E 一并加入 `Required FFmpeg` 清单。

清单校验（解析折叠命令后的真实参数）：

```text
registered: 72 | passed to pytest: 72
hidden by comments: NONE
inline '#' in block: 0
  tests/test_tsb366r3_package_deep.py: OK
  tests/test_tsb366r4r1_native_video_longpath_e2e.py: OK
```

`test_b10_media_suite_is_explicitly_forced_in_ci` 与 `test_ci_skip_guard.py` 均通过。

---

## 七、回归矩阵

| 编号 | 验证 | 结果 |
| --- | --- | --- |
| A-1 | V31 单独 ×25（CI 条件，无 `.env`） | **25/25** |
| A-2 | 媒体清单（72 文件）连跑 3 次 | **1299 / 0 / 0 / 44** ×3 |
| A-3 | 确定性双执行者竞争 | 5/5 通过，反向 2 RED |
| A-4 | Job 生命周期终态合同 | 由 A-3 + V31 断言覆盖 |
| B-1 | 派生 clip 真 ≥260 | 本机 **394**；Windows 由 CI |
| B-2 | 派生/复核/字节 | 全部断言通过 |
| B-3 | Mock 请求 | 恰好 1 次，完整字节 |
| B-4 | Observation + cleanup + 原片不变 | 通过 |
| B-5 | R3 Package 深路径进强制清单 | **已补入**（原为漏覆盖） |
| C-1 | 现有视频/预算/取消/恢复 | 全量通过 |
| C-2 | 后端全量 | **1741 / 0 / 0 / 48**（本轮前 1731） |
| C-3 | 前端四项 | typecheck ✅ lint ✅ **123 passed** ✅ build ✅ |
| C-4 | 主 CI | **7/7 success** @ `224e154`（[run 42392563283](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42392563283)） |

> 计数口径：`tests` 含 skip，passed 不含。全部数字来自 junitxml 实测。

---

## 八、范围声明

**改动文件**

- `backend/app/services/runner.py`（phase 终态不可变）
- `backend/app/services/review_package.py`（ZIP 枚举走 walk_files）
- `backend/app/ffmpeg.py`（walk_files 剥离扩展长度前缀）
- `backend/tests/test_video_e2e_formal_api.py`（V31 执行权归属）
- `backend/tests/test_tsb366r4r1_job_phase_race.py`（**新增**，5 项）
- `backend/tests/test_tsb366r3_package_deep.py`（夹具长路径兼容 + 2 条回归锁）
- `backend/tests/test_tsb366r4r1_native_video_longpath_e2e.py`（**新增**，2 项）
- `.github/workflows/ci.yml`（两份深路径套件入强制清单）
- 本记录与 `docs/validation/tsb366-r4-r1/`

**明确未做**

- 未修改原片、原始 Asset/Segment、历史 Observation。
- 未接入真实模型端点，未上传真实旅行视频。
- 未改变 `video_native` 协议、能力门禁、上传授权、预算账本、重试策略。
- 未把视频改成 `multiframe` / `image_only` 绕过。
- 未改 TSB-366-R2 的 loopback 代理修复与 408 分类。
- 未改 TSB-367 的 Storyboard/PlanOperation/Alembic。
- 未改 runner 锁顺序或执行器配置。
- 未删除/skip/xfail 测试，未放宽预期终态，未加 sleep 或盲目重试。
- 未缩短目录或降低 260 阈值。
- **未开展 TSB-368。**

---

## 九、遗留风险（诚实列出）

1. **CI 那一次的两个执行者具体交错指令点未逐条证实。** 本机串行 25/25 未复现
   原始交错；受控交错证明的是"该交错**足以**造成此症状"，
   而非"CI 那次**必然**是这条路径"。
2. **Windows ≥260 证据依赖 CI 的 windows-latest**，本机无法产生。
   若 CI 未执行到该用例，必须在报告中标为未验收。
3. `LongPathsEnabled` 读不到时测试会失败（设计如此）；若 CI 环境无法读取注册表，
   该用例会红 —— 这是有意的门禁，不是缺陷。
4. 真实旅行素材 8–10 段端到端验收、60+/200GB 全量性能**仍未执行**。
5. G5 真实端点能力与 G6 真实 A/B **仍为独立未验证门禁**。
6. 本记录中所有本机绿色均**不构成** Windows MAX_PATH 或真机验收证据（C8）。
