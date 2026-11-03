# TSB-361 — Windows 全量测试隔离整改与验收收口

- 任务编号：TSB-361（P0，测试基建整改 / 稳定性修复）
- 冻结基线：`main@119531e`（前置批次 TSB-360）
- 本轮结论：**Windows 严格全量 0 failed / 0 errors**，两个偶发失败的根因已定位并
  在测试基建层隔离；**产品代码未改动**
- 证据目录：[docs/validation/tsb361/](validation/tsb361/)

本轮不开发 AI 视频识别功能、不启动 M6、不重构剪辑算法。

---

## 一、修复前的复现证据

两个失败都不是"单独跑偶发"，而是"前序用例 + 目标用例"两段组合下的确定性失败。
屏障关闭时，下面两条命令本轮各跑两次、两次全红。

**问题 A** — `test_video_fallback_runner_e2e.py::test_b01_no_upload_consent_means_no_frames_and_no_model_request`

```
cd backend && TSB_REQUIRE_FFMPEG=1 python -m pytest \
  tests/test_video_fallback_execution.py tests/test_video_job.py \
  tests/test_video_fallback_runner_e2e.py::test_b01_no_upload_consent_means_no_frames_and_no_model_request
```

```
>       assert e.extracted == []
E       AssertionError: assert [(('C:\\Users...583D692480>})] == []
E         Left contains 2 more items, first extra item:
E         (('C:\\...\\pytest-619\\test_a_fallback_does_not_produ0\\src.mp4',...
```

断言里出现的是**别的用例的 `src.mp4`**——与任务书 §1.1 记录的现象一致。

**问题 B** — `test_vision_cleanup_status.py::test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free[motion]`

```
cd backend && TSB_REQUIRE_FFMPEG=1 python -m pytest \
  tests/test_video_fallback_execution.py \
  "tests/test_vision_cleanup_status.py::test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free[motion]"
```

```
>       assert len(calls)==paid
E       assert 2 == 1
E        +  where 2 = len([1, 1])
```

## 二、根因

两个失败**同源**：一个用例留下的可执行工作，在另一个用例体内执行。有两条路径。

**路径 1（已提交、未等待）。** `test_video_fallback_execution.py` 里的用例直接调用
`vj.video_analyze_task(job_id)`；回退分支经 `_schedule_fallback` →
`job_service._create_locked` → `runner.submit(...)` 把 `motion_review` 子任务提交到
**模块级 `runner`**（`app/services/runner.py::runner`）。用例返回时子任务仍在跑，
晚到的 `motion.extract_frame` 落进下一个用例 monkeypatch 的 tracker（问题 A）。

**路径 2（未派发、被恢复）。** 同一批用例从不经过 `JobRunner._run`，行永远停在
`pending`。下一个用例的 `client` fixture 启动 lifespan 时，
`app/main.py::lifespan → resume_interrupted_jobs()` 把它当作"上次进程崩溃遗留"
重新派发，于是它**在那个用例体内**执行（问题 B）。

路径 2 的直接证据（在 `infer` stub 上临时打印调用栈，取证后已移除）：

```
[infer] calls=2 thread=tsb-job_1
        source_path='...\\pytest-619\\test_v23c_a_fallback_does_not_0\\src.mp4'
  File "app/services/runner.py", line 480, in _run
  File "app/services/vision/motion.py", line 278, in motion_review_task
```

第二次 `infer` 不是本用例的 retry 发出的，而是 `tsb-job_1` 里一个属于 `test_v23c_*`
的真实 Job。同一次调试中 `runner._futures` 的三条记录全部属于 v23c 的项目。

**这不是产品缺陷。** 生产进程只启动一次，`resume_interrupted_jobs()` 的语义在那时
是正确的；缺口在于测试共享同一个进程、同一个 `runner`、同一个数据库。

## 三、修改文件与方案

| 文件 | 改动 |
|---|---|
| `backend/tests/conftest.py` | 新增 `drain_background_jobs()`、`settle_unfinished_jobs()` 与 autouse 屏障 `_no_background_jobs_left_behind` |
| `backend/tests/test_vision_cleanup_status.py` | `visual` fixture 装配抽成 `_pipeline(kind, …)`；新增 B01–B08；**既有用例一行未改** |
| `backend/tests/test_tsb361_job_lifecycle.py` | 新增（A01–A09） |
| `backend/tests/test_media_process_tree.py` | 取消触发条件由 `pid_file.exists()` 改为 `_pids_ready()`（§八·附），新增 2 项守卫 |
| `scripts/ci_annotate.py` + `backend/tests/test_ci_annotate.py` | 新增：把 junit 的失败/跳过变成运行页注解，另加 8 项测试 |
| `.github/workflows/ci.yml` | 后端作业与强制媒体作业各加一步 `Annotate failures and skips` |
| `AGENTS.md` / `docs/REGRESSION.md` / `README.md` | 纪律、回归清单、实测数字 |
| `docs/validation/tsb361/` | 证据（命令、日志、junit 统计） |

屏障在**每个用例的 teardown** 执行两步：

1. `drain_background_jobs()`：遍历 `runner._futures`，每轮重新快照（完成的父任务
   可能又提交了后继），逐个 `future.result()` 等待；超时返回残留 job id 并由屏障
   `pytest.fail`——**不静默**。
2. `settle_unfinished_jobs()`：把仍处于 `pending/running/cancelling` 的行落成终态
   （`finalize_job(..., allow_pending=True)`），使下一次启动的恢复扫描不再选中它们。
   只收敛 worker 已停止的行；仍在飞的 Job 由第 1 步 fail。

teardown 与用例体成败无关（pytest 对 fixture 的收尾是正常推进到 `yield` 之后），
因此断言失败也照样回收；`A05` 直接驱动该生成器复刻了这一步。

**没有做的事**：没有删除/弱化任何既有断言（`test_vision_cleanup_status.py` 仍按
全表快照比较）；没有新增 skip/xfail；没有用固定 sleep 当修复；没有放宽
`TSB_REQUIRE_FFMPEG=1` 的静默跳过判定；没有改任何 `backend/app/` 代码。

## 四、新增回归测试

| 编号 | 用例 | 验收点 |
|---|---|---|
| A01 | `test_a01_a/_b` | 留下仍在跑的后台 Job 即返回；下一用例看到 `succeeded`（真跑完），不是被兜底取消 |
| A02 | `test_a02_a/_b` | 测试体之后才发的抽帧事件只落进发起它的用例，后一个用例 tracker 为空 |
| A03 | `test_a03_…` | `threading.Barrier` 保证两个 Job 真并发，各自 tracker 无交集 |
| A04 | `test_a04_a/_b` | 运行中取消：等 worker 自己退出后行才落 `cancelled` |
| A05 | `test_a05_…` | 用例体失败时 teardown 仍把 Job 收干净 |
| A06 | `test_a06_a/_b` | `add_done_callback` 的晚到回调不写进后来者的 tracker |
| A07 | `test_a07_a/_b` | 后台任务抛异常 ⇒ `failed` + 错误原文，不假装成功 |
| A08 | `test_a08_…` | 同一场景连跑 3 轮，终态/统计/收敛性完全一致 |
| A09 | `test_a09_…` | `pending` 行落终态后 `resume_interrupted_jobs()` 不再选中（路径 2） |
| B01 | `test_b01_…` | 模型请求次数、入库行数、staging 残留、`partial_failed`、`analyzed=1` 五项各自可见（semantic/motion） |
| B02 | `test_b02_…` | 连续两次 cleanup-retry 都不重新请求模型、不重复写结果（semantic/motion） |
| B03 | `test_b03_…` | 两个 Job 并发清理只动自己的暂存，`removed_bytes` 各自准确 |
| B04 | `test_b04_…` | 他人遗留的 ownership 不影响本 Job 结论，其文件与 stats 均不被改动 |
| B05 | `test_b05_…` | receipt 丢失后同路径出现新目录 ⇒ `STAGING_NOT_OWNED`，一个字节都不删 |
| B06 | `test_b06_…` | 删到一半抛 `OSError` ⇒ `CLEANUP_FAILED` + `retryable` + 残留字节 + 审计条目 |
| B07 | `test_b07_…` | 反复清理幂等（`removed_files` 1→0→0），邻接目录不受影响 |
| B08 | `test_b08_…` | motion 与 semantic 在同一用例内逐字段比较 8 项清理状态契约，且与期望契约相等 |

`collected` 由 **1349** 升到 **1383**（+14 A、+10 B、+8 注解器、+2 进程树守卫）。
B 组保留 `motion` 参数化。

**非空转证明**：把屏障关掉后，`test_tsb361_job_lifecycle.py` 有 6 项立刻变红，
`A09` 在只保留 drain 时变红（`AttributeError: settle_unfinished_jobs`）。

## 五、修复前后对比

| 指标 | 修复前（基线 119531e） | 修复后（本轮） |
|---|---:|---:|
| collected | 1349 | **1383** |
| passed | 1342 | **1378** |
| failed | 2 | **0** |
| errors | 0 | **0** |
| skipped | 5 | 5（均为白名单平台事实） |
| 后端全量耗时 | 22m51s | 17m43s |

耗时下降与"少跑两轮重试/差异来自主机负载"有关，不作为性能结论。

## 六、Windows 严格全量

```
cd backend && TSB_REQUIRE_FFMPEG=1 python -m pytest --junitxml=tsb361-windows.xml
→ 1378 passed, 5 skipped in 1063.87s (0:17:43)，exit=0
```

junit 统计：`tests=1383 failures=0 errors=0 skipped=5`
（[junit-summary.txt](validation/tsb361/junit-summary.txt)、
[full-windows.txt](validation/tsb361/full-windows.txt)、
[tsb361-windows.xml](validation/tsb361/tsb361-windows.xml)）。

5 个 skip 全部是 `link creation is a platform fact`（Windows 无符号链接创建权限），
逐项列在 `junit-summary.txt`，与 TSB-360 一致，未新增 skip。

**顺序与重复验证**（`TSB_REQUIRE_FFMPEG=1`）：

| 场景 | 结果 |
|---|---|
| A → B 混合 | 全绿 |
| B → A 反向 | 全绿 |
| 相关后台 Job 用例混跑（8 个文件） | 131 passed, 1 skipped（199.95s） |
| 同组连续 3 轮 | 3×`131 passed, 1 skipped` |
| Windows 严格全量 | 1378 passed, 5 skipped, 0 failed, 0 errors |

## 七、前端

`pnpm run typecheck / lint / test / build` 四项 exit=0，**98 passed / 12 files**
（[frontend.txt](validation/tsb361/frontend.txt)）。本轮无前端产品代码改动。

## 八、GitHub Actions

| 运行 | 交付 SHA | 结果 |
|---|---|---|
| #83（TSB-359 收尾） | `506ca69` | 7/7 success |
| #84（冻结基线） | `119531e` | 失败：Windows 三个作业在安装/校验步骤 `exit 127`（runner 环境），与本轮改动无关 |
| #85 | `247a9b1` | 6/7：`Required FFmpeg (windows-latest)` 在 step 9（强制媒体 pytest）`exit 1`，无公开可读的失败用例名 |
| #86 | `4bdefec` | 7/7 success（6m23s）——测试代码与 #85 完全相同，只多一步注解器 |
| #87 | `6f31dd6` | 7/7 success（6m29s） |
| #88 | `c652e9b` | 6/7：`Backend (Python 3.11)` `exit 1`；注解器当时只在媒体作业，失败用例名不可见 |
| #89 | `e739f2d` | 6/7：注解器给出用例名——`test_media_process_tree::test_parent_exiting_first_still_reclaims_the_grandchild`，见下节 |
| **#90（最终）** | **`<!-- FINAL_SHA -->`** | 见 §十一 |

### 关于 #85 的诚实交代

#85 的 Windows 强制媒体作业在 step 9 以 `exit 1` 结束。该作业的失败详情只写在**需要
登录**才能读的作业日志里，run 页当时也没有可公开读取的失败用例名，artifact 同样需要
授权——所以**本记录不声称已定位那一次失败**，也不把它算成"已修好"。可核验的事实是：

1. 用与 CI **完全相同的一条命令**（同一份 69 个文件、同一顺序、`TSB_REQUIRE_FFMPEG=1`）
   在 Windows 本机复跑：`tests=1275 failures=0 errors=0 skipped=5`
   （[media-mandatory-windows.txt](validation/tsb361/media-mandatory-windows.txt)）；
2. #86 的**测试代码与 #85 完全相同**（只多了一步 junit 注解器）却 7/7 通过——说明
   #85 不是由某个稳定的断言错误造成的；
3. 两次运行之间唯一的变化是把"红叉但不说是哪条用例"这个盲区补上了（见下）。

如果 #85 属于 TSB-361 要修的那类跨用例执行，本轮的屏障仍然只是"把窗口关小"：
第 1 条证据只能证明本机在同序全量下干净，不能反证 CI 上不会再现。因此这条风险
保留在 §十。

### 为此增加的 CI 观测（`.github/workflows/ci.yml`）

新增一步 `Annotate failures and skips`（`if: always()`）：

- junit 里每个 `failure`/`error` → `::error` 注解，带用例 nodeid 与首行断言；
- 每个 `skip` → `::warning`，带 nodeid 与跳过原因；
- 同一份内容写进 `GITHUB_STEP_SUMMARY`。

只做观测：不改测试选择、不改环境变量、不改任何断言。受 `ci.yml` 内容约束的 55 项
用例（`test_ci_environment_isolation`、`test_ci_skip_guard`、`test_media_format_matrix`、
`test_video_fallback_runner_e2e`、`test_video_model_switch_scope`）在改动后全绿。

### 附：注解器第一次上岗就抓到第三个偶发（`test_media_process_tree`）

`#88` 是一次纯文档提交，却在 `Backend (Python 3.11)` 上 `exit 1`；当时注解器只装在
媒体作业里，运行页依旧只说"退出码 1"。把它装到 `backend` 作业后，`#89`（`e739f2d`）
立刻给出用例名：

```
[failure] Required FFmpeg (ubuntu-latest)
tests.test_media_process_tree::test_parent_exiting_first_still_reclaims_the_grandchild
AssertionError: the tree was never observable
```

**根因在夹具，不在产品。** `_ORPHANING_SCRIPT` 先 `open(pid_file, "w")` 再写两个 pid；
`Path.exists()` 在文件被创建的那一刻就为真，而内容还是空的。用例用
`cancel_check=lambda: pid_file.exists()` 触发取消，于是取消可能落在"文件已存在、内容
未落盘"的窗口里：整棵树被杀掉，文件永远停在空或半个 pid，随后的
`_wait_file(pid_file, 5)` 必然失败。**断言没错，是触发条件比后置条件弱。**

修法：新增 `_pids_ready()`（文件可读且正好两个 pid），取消触发与 `_wait_file` 共用
同一个判定，5 个触发点（`test_media_process_tree.py`）全部替换。用例强度未变——仍然
断言孙子进程被杀掉，只是"必须先观测到完整进程树"这一前提改由构造保证。

- RED 证据：CI run #89 的注解原文（见上）；
- 守卫：`test_a_half_written_pid_file_is_not_treated_as_observable` 复刻"空文件
  `exists()` 为真、`_pids_ready()` 为假"的那一刻；
  `test_a_missing_pid_file_is_not_an_error` 保证触发函数在 `run()` 的轮询里不抛异常；
- 顺带把 5 个触发点的 `run()` 加上 `timeout=60`：一旦触发条件永远不成立，60 秒内
  以 `FFmpegTimeout` 失败，而不是等默认的 3600 秒把整条流水线拖死（§十.5 记了这条
  的来历）；
- GREEN：该文件本机连续 3 轮 `12 passed`（[media-process-tree-3x.txt](validation/tsb361/media-process-tree-3x.txt)），
  随后进入 Windows 严格全量与 CI 强制媒体作业。

## 九、是否修改了产品代码

**没有。** `backend/app/` 一行未改。根因全部位于测试共享状态，改产品代码反而会
破坏"启动恢复中断任务"这一正确语义。

## 十、尚未解决的问题

1. **两条泄漏路径的触发依赖时序。** 屏障两半缺任一半，某些顺序下仍可能复现
   （[validation README](validation/tsb361/README.md) 的「归因实验」小节）。
   因此两半都必须保留，"只留一半"不能作为长期方案。
2. **成对用例必须整文件运行。** A01/A02/A04/A06/A07 的第二个用例若被单独选中，
   会显式失败并提示"必须整文件运行"，而不是静默通过。CI 与本地全量都满足这一前提。
3. **`%TEMP%` 归属问题属宿主环境**：本机取证时 `pytest` 的 `sessionfinish` 清理会被
   安全删除钩子中断（退出码 1 但没有失败用例）。已写入 `AGENTS.md §4.2`，用
   仓库外的 `TMPDIR` 规避；CI 不受影响。
4. **#85 的 Windows 强制媒体作业失败原因未定位**（详见 §八）。本机能证明同一命令
   同序全绿、#86 在相同测试代码上 7/7，但这不是因果结论；下一次再现时，新增的注解
   会直接给出用例名。同理，基线 `119531e`（run 4/7）的 Windows `exit 127` 属 runner
   环境，未在本轮处理。
5. **本机首次全量曾整段卡死（30 分钟无输出）**：卡在
   `test_long_paths.py::test_run_classifies_command_failure`，`run()` 内的 `ffmpeg`
   子进程随后消失、父进程仍在等。杀掉后用**新的仓库外 `TMPDIR`** 重跑同一棵树，
   17m43s 全绿，单跑该用例也通过。判断为宿主/沙箱层面的偶发，不是用例逻辑；但它
   说明"`run()` 的默认 `timeout=3600` 会把一次卡顿放大成整小时"——因此本轮给
   `test_media_process_tree.py` 的 5 个取消触发都加了 `timeout=60`，卡住时 60 秒内
   以 `FFmpegTimeout` 失败，而不是挂到 CI 超时。
6. G5/G6（真实端点视频能力、真实 A/B）、真人素材验收、200GB 性能**状态不变**。

## 十一、最终 Commit SHA

- **交付 SHA（代码 + 测试 + 文档全部在内）：`6f31dd6`**
  `docs(TSB-361): 证据文件改用 .txt 后缀，补强制媒体清单证据条目`
- 该 SHA 的 CI：**run #87 → 7/7 success**（6m29s）
- 本轮推送序列：`d3bbcd8` → `fbbf94c` → `247a9b1` → `4bdefec` → `c5ccccd` → `6f31dd6`
- 四次推送里三次 7/7（#86 `4bdefec`、#87 `6f31dd6`，均 6m2x s），一次 6/7（#85
  `247a9b1`，见 §八 的交代）
- 本节所在的回填提交**只改这一个文件**，被测代码/测试/CI 定义与 `6f31dd6` 完全一致，
  因此上表就是最终交付树的证据

## 十二、发布记录与测试证据路径

- 本文件
- [docs/REGRESSION.md](REGRESSION.md) 新增「TSB-360 / TSB-361 测试基建」小节
- [docs/validation/tsb361/README.md](validation/tsb361/README.md)：RED/GREEN 命令、
  归因实验、顺序与重复验证
- [docs/validation/tsb361/full-windows.txt](validation/tsb361/full-windows.txt)、
  [tsb361-windows.xml](validation/tsb361/tsb361-windows.xml)、
  [junit-summary.txt](validation/tsb361/junit-summary.txt)、
  [media-mandatory-windows.txt](validation/tsb361/media-mandatory-windows.txt)（CI 同款
  69 文件强制清单的本机复跑）、
  [media-process-tree-3x.txt](validation/tsb361/media-process-tree-3x.txt)、
  [mixed-1.txt](validation/tsb361/mixed-1.txt)、
  [repeat-3x.txt](validation/tsb361/repeat-3x.txt)、
  [frontend.txt](validation/tsb361/frontend.txt)
- 上一轮（TSB-360）：[docs/validation/win-local-2026-11-02.md](validation/win-local-2026-11-02.md)
