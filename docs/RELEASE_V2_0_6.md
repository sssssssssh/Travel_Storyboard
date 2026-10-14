# Travel Storyboard v2.0.6 — 取消状态与资源清理闭环整改

> 基线：`main` @ `74b372d3545ac3308827b1113088d8901240d66a`（固定审查基线）
> 范围：缺陷整改与回归验证（不含 v2.1 的全素材 AI 导演设计）

本轮对应《Travel_Storyboard v2.0.6 取消状态与资源清理闭环整改开发任务书》，
关闭 TSB-261 ~ TSB-264、TSB-266。**TSB-265（Windows 真实旅行素材验收）未执行**，
见第七节：按任务书要求，交付结论只能是**「代码级整改通过，发布门禁未完成」**。

**范围限定（已遵守）**：未调整镜头算法、AI Prompt、故事线规划、时间线规则；
未迁移原片；未重建 SQLite；未更改人工评分、备注、裁剪与剪辑计划；
未新建分支，全部提交在 `main` 并推送。

---

## 一、TSB-261 取消中状态与项目互斥闭环（P1）

**问题**：`cancel_job` 直接把 `pending/running` 写成终态 `cancelled`，而互斥只认
`ACTIVE_STATUSES = {pending, running}`。于是用户一点取消，项目立刻显得空闲 ——
尽管 worker 还在编码、仍持有 `project_lock`、暂存尚未回收。新的互斥任务可被提前
接受，排到那个尚未停止的任务后面；任务行显示「已取消」而 CPU 仍在烧。

**修复**：引入**非终态** `cancelling`，并把状态集合拆开（三者不可混用）：

| 集合 | 成员 | 用途 |
|---|---|---|
| `ACTIVE_STATUSES` | `pending`, `running` | 可迁移到 `running` 的状态 |
| `CANCEL_REQUESTED_STATES` | `cancelling`, `cancelled` | `is_cancelled()` 等：取消一落地媒体层就应停 |
| `EXCLUSIVE_STATUSES` | `pending`, `running`, `cancelling` | 互斥判定：正在停止的任务仍占用项目 |

- `POST /jobs/{id}/cancel` 原子 CAS 写 `cancelling`，**不写 `finished_at`**；
  重复取消 `cancelling` 幂等返回 200；真终态仍 409 且绝不改写。
- Runner 在 worker 真正退出（任务体返回、暂存清理完成）后才
  `cancelling → cancelled` 并写 `finished_at`，用 CAS 保证终态唯一、不逆向覆盖。
- 排队任务的取消由 `converge_pending_cancellations()` 收敛，不会永远停在 `cancelling`；
  启动时先收敛再 resume。恢复逻辑不会把 `cancelling` 改回 `running`，也不重跑已取消的编码。
- 互斥改为**两层**：先非阻塞获取 worker 持有的同一 `project_lock`（拿不到即 409，
  并带出阻塞任务类型与 ID），再在 `BEGIN IMMEDIATE` 内检查并插入。锁序固定为
  「先项目锁、后 DB 写事务」。**不能只让新 Job 排队** —— 任务书明确要求 409。

**前端**：`cancelling` 显示「正在取消 / 资源清理中」，与「已取消」区分；
取消中与等待中一样保持重活按钮禁用（Project / Export），AIDirector 轮询覆盖
`cancelling`。`JobOut` 字段向后兼容。

**状态模型**已固化到 [`DESIGN.md` §8.1](../DESIGN.md)。

---

## 二、TSB-262 取消/异常时暂存资源必达清理（P1）

**问题**：`roughcut_task` 只在 **success 路径**执行 `shutil.rmtree(stage)`，而该行位于
concat 与发布之后 —— 取消、超时、「没有产出任何片段」、编码抛错都会提前返回，把整
目录已编码片段留在盘上。`_analyze_asset` 是同一形状（抽帧中止即绕开清理）。
`proxies_task` 用的裸 `ignore_errors=True` 无法区分「已清理」与「清理失败」，
不能作为验收证据。

**修复**：新增 [`services/staging_cleanup.py`](../backend/app/services/staging_cleanup.py)。

- `cleanup_owned_staging(project_id, stage, owner_job_id)`：**正向白名单**。
  仅接受同时满足「真实目录 + 严格位于本项目工作区内（工作区根拒绝）+ 形状为
  `staging/<asset>/<token>` / `proxies/staging/<token>` / `roughcut/staging-*`
  + 解析后仍在本项目内」的路径；其余一律 `STAGING_NOT_OWNED`。
- 明确拒绝 `frames/`、`proxies/`、`roughcut/`、`packages/` 等正式产物目录、
  数据目录、用户素材目录与项目外路径。
- 返回 `CleanupResult`（`removed_files` / `removed_bytes` / `residual_paths` /
  `retryable`），失败记 `CLEANUP_FAILED` 并写入 `job.stats['cleanup']` 可追踪；
  **不做无证据的成功宣称**，不使用 `ignore_errors=True` 作为证据。
- 接线：`roughcut` 整个任务体进 `try/finally`；`analyzer` 拆出
  `_analyze_asset_inner`，stage 创建后即进 `try/finally`；`proxies` 的 `finally`
  改用受限封装。清理发生在任务退出、**互斥释放之前**。

**修复过程中由测试发现的三个真实缺陷**（均已修复并留守卫）：

1. **链接逃逸（严重）**：初版 `_iter_tree` + `rmtree` 会跟随 junction，把项目外的
   目录**整个删掉** —— 直接违反「原片/外部数据不可删除」。现已不进入任何 reparse
   point，链接本身单独 unlink。
2. **深路径静默空目录**：`_listdir` 先捕获 `FileNotFoundError` 并返回 `[]`，
   而 Windows 对 >MAX_PATH 目录抛的正是 WinError 3 —— 于是深路径 stage 看起来是空的，
   清理「成功」却什么都没删。已改为先尝试扩展长度形式再判定为空。
3. **计数不诚实**：按调用是否抛错累计 `removed`，会出现「文件 unlink 失败、随后被目录
   删除顺手带走」，结果报告 `ok=True, removed_files=0`。现改为清理后实测残留，
   按真实消失量计数。

**配套**：`runner.merge_job_stats()` 合并清理结果而不覆盖并发进度写入。
新增 [`tools/check_free_names.py`](../backend/tools/check_free_names.py) 静态查出
「函数拆分后遗留的自由变量」——本次拆分实际遗留了 `run_token`/`source`/`context`/
`verify_*` 四个，运行时才逐条暴露；该工具一次列全。

---

## 三、TSB-263 媒体进程树定向终止与回收（P2）

**问题**：`_popen_kwargs` 给了子进程独立进程组，但 `_terminate_tree` 只对**直接子进程**
调用 `terminate()/kill()`。FFmpeg 会派生辅助进程，真实编码树可能有多层，所以父进程
退出并不能证明树已回收 —— 孙进程继续存活，占着文件句柄与 CPU，而任务早已报告 cancelled。

**已实测复现**：禁用新逻辑后运行 [`tools/probe_cancel_tree.py`](../backend/tools/probe_cancel_tree.py)，
观察到父进程消失、**孤儿孙进程仍 ALIVE**（进程表可见）。

**修复**：

- **Windows**：为每个媒体进程创建独立 Job Object 并设
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。后代自动成为成员，关闭句柄即回收全树 ——
  包括父进程已先退出的情况（这正是单点 `terminate()` 处理不了的那类）。
  环境不允许嵌套 Job 时降级为进程组信号，不影响主流程。
- **POSIX**：对本次启动的独立进程组发 `SIGTERM`（子进程已 `setsid`），宽限期后升级
  `SIGKILL`；组号在 spawn 时记录，避免子进程被回收后读到被复用的组号。
- 两条路径最后都 `_reap()` 直接子进程，不留僵尸，并确保管道句柄回收。
  `finally` 中关闭 job —— 关闭动作本身就是回收动作，句柄不泄漏。

**范围限定**：作用域仅限本次启动的进程组 / Job。**无** `/IM`、`pkill`、`killall`；
该规则由 `test_no_global_name_based_kill_is_used` 静态守卫，
并由 `test_cancelling_one_run_leaves_an_unrelated_run_alone` 用旁观者真实进程树证明
取消 A 不影响并发运行的 B。

---

## 四、TSB-264 回归矩阵及故障注入（P1）

新增 5 个测试文件、58 个用例；`test_job_cancel_kills_process.py`（TSB-253）等既有文件保留并复跑。

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `test_cancel_lifecycle.py` | 14 | 状态集合不变量、TC-01~10 |
| `test_cancel_cleanup.py` | 21 | 清理白名单边界、TC-11~20 |
| `test_media_process_tree.py` | 10 | 真实父→孙进程树、TC-21~23 |
| `test_cancel_recovery.py` | 10 | HTTP 一致性、用户状态保全、多项目、TC-24~26/29 |
| `test_ci_skip_guard.py` | 3 | CI skip 守卫自覆盖（见 §5.2） |

**测试纪律（已遵守）**：
- 先写复现失败的用例，再修复至通过；对关键保护做**反向验证**（撤去接线应失败）。
- 并发用 `threading.Event` / `Barrier` 等可观察条件同步，**不以 `sleep(0.1)` 作为
  可靠竞争验证**。
- 不把 Windows 专属测试的合理 skip 误判为 FFmpeg 缺失：`requires_windows` 标记的
  skip 与「工具缺失」在 `conftest.py` 中被区分，`TSB_REQUIRE_FFMPEG=1` 只把后者判失败。

**反向验证记录**：

| 撤去的保护 | 结果 |
|---|---|
| `cancel_job` 写 `cancelling` 改为写终态 | `test_cancel_while_running_enters_cancelling`、`test_new_exclusive_job_is_rejected_while_cancelling` 立即失败，报「cancel must not jump straight to a terminal state」 |
| 清理不进入 reparse point 的检查 | `test_cleanup_refuses_a_symlink_pointing_outside` 失败，报「deleted outside data」 |
| Job Object + 进程组终止 | 探针**卡住**并观察到存活孤儿孙进程 |

---

## 五、TSB-266 CI、发布记录与单分支交付（P1）

- 全部提交在 `main`，未新建分支，未 `push --force`。每批次独立 Commit。
- 状态图与恢复策略已更新至 [`DESIGN.md` §8.1–8.3](../DESIGN.md)。
- 新用例映射已加入 [`REGRESSION.md`](REGRESSION.md)。

### 5.1 CI 结果（已验证）

主线 `${HEAD}` 对应的 GitHub Actions **7/7 全部成功**：

| 作业 | 结果 |
| --- | --- |
| Backend (Python 3.11) ×2 | ✅ Ubuntu / Windows |
| Backend (Python 3.13) ×2 | ✅ Ubuntu / Windows |
| Required FFmpeg (ubuntu-latest) | ✅ |
| Required FFmpeg (windows-latest) | ✅ |
| Frontend | ✅ |

> 提交 `7e2fb7d` 的运行：<https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38540406730>

### 5.2 CI 发现并修复的缺陷：平台 skip 被误判为失败

把 8 个套件加入强制媒体作业后，CI 在 **Ubuntu** 上失败（Windows 通过），失败作业
正是 `Required FFmpeg (ubuntu-latest)`。

**根因**：`conftest._EXPECTED_SKIP_REASONS` 只列了两条原因，而
`test_deep_media_http.py` 与 `test_deep_scan_api.py` 在 Linux 上用的 skip 原因是
`"MAX_PATH only constrains Windows"`，未被识别。`TSB_REQUIRE_FFMPEG=1` 下
`pytest_sessionfinish` 把这类**合法平台 skip** 当成「意外 skip」并置非零退出码。
这两个文件此前不在媒体作业里，所以问题直到扩表才暴露。

**修复**：白名单补入该原因；同时收紧两处测试，使其不再依赖环境是否碰巧可用：

- `test_cancel_cleanup` 的链接用例原先 `mklink` 失败即 skip —— 而它守护的是本轮
  **最严重**的缺陷（跟随 junction 删除项目外数据）。现改为 junction 失败后回退
  symlink，两者都不行才 skip，且原因写明为平台事实。
- `test_media_process_tree` 的「父进程先退出」用例去掉竞态 skip：取消只在 pid 文件
  可读后才请求，观测必然成功，改为断言。

**新增自覆盖守卫** `tests/test_ci_skip_guard.py`（3 用例）：扫描全部测试源码的
`skipif` 原因，每个原因要么被认定为平台事实，要么显式列为应失败。反向验证：移除
白名单该条后立即失败并点名 `"MAX_PATH only constrains Windows" ... will fail the
mandatory-media job on the platform where it skips` —— 精确复现本次 Ubuntu 故障。
同时断言**工具缺失类 skip 必须仍然失败**，防止为了让 CI 变绿而放宽守卫到失效。

### 5.3 强制媒体作业的清单维护

媒体作业用**硬编码**文件清单（普通 backend 作业跑全量但不设 `TSB_REQUIRE_FFMPEG=1`，
被静默 skip 的用例在那里会通过）。本轮把 TSB-251/252/253/254/261/262/263/264 的
8 个套件补入，并在 workflow 内注明该清单用途，避免后续再漏。


---

## 六、验证结果

| 项目 | 结果 |
| --- | --- |
| 后端测试 | **456 passed / 0 failed / 0 skipped**（基线 398 + 新增 58） |
| 本机环境 | Windows / Python 3.13.7 / FFmpeg 9.0.2 / `LongPathsEnabled=0` |
| 前端 | `typecheck` · `lint` · `test`(14) · `build` 四项通过 |
| 主线 CI | **7/7 成功**（含 Ubuntu 与 Windows 的强制媒体作业） |
| 回归 | 无既有用例被破坏 |

测试记录见 [`validation/v2.0.6/backend-windows-py3.13.md`](validation/v2.0.6/backend-windows-py3.13.md)。

---

## 七、尚未完成：TSB-265（发布门禁）

**未执行，不能作为通过证据。**

任务书要求：Windows 实机 + **8～10 段真实土耳其旅行视频**（横/竖屏、短/长片、
4K HEVC、有/无音轨，如有 HDR 与 VFR 一并纳入）跑完整流程，并在代理/粗剪处理中
执行取消，核对「cancelling 期间 409、结束后 202」、子进程退出、stage 无残留，
最后复验原片 SHA-256、大小、mtime 一致。

本轮**未获得**这些真实素材，也未在其实机执行该流程，因此：

- `docs/validation/v2.0.6/WINDOWS_REAL_FOOTAGE.md` 记为**未执行**；
- 60+ 视频 / 200GB 全量性能同样**未执行**，不以外推宣称通过；
- 交付结论：**代码级整改通过（TC-01~28 可执行且符合预期，主线 CI 通过），
  发布门禁未完成（缺 TC-29/30 的真实素材实机证据）**。

自动化与合成素材通过**不构成**实机验收证据。

---

## 八、范围声明

本轮**未**改动：镜头检测算法、模型 Prompt、故事线规划、粗剪叙事逻辑。

**未**迁移或改写任何既有产物路径与数据库引用；**未**删除或修改用户原片、
历史分析包、人工评分/备注/裁剪、ReviewRequest 历史、EditPlanShot 与已发布代理。

`cancelling` 是**业务状态扩展，不是新的数据库列**：`jobs.status` 本就是
`String(32)`，无需迁移。

---

## 九、风险与回滚

**风险 / 待验证**（未在本轮测试中稳定复现或覆盖的系统边界，不写「已修复」）：

- Windows Job Object 在**嵌套 Job 受限**的主机（部分 CI runner、某些企业策略）
  上会降级为进程组信号；降级路径本身的正确性只有 POSIX 侧有直接测试。
- 真实 4K/HEVC/VFR 素材上取消的**实际时延**未测（合成素材为秒级命令）。
- 旧版本不认识 `cancelling` 状态：**不能在仍有 `cancelling` 任务时直接降级启动**。

**回滚办法**：按批次 `git revert`（不要 `push --force`），然后重跑 CI。
回滚前请停止服务、确认所有 worker 已退出，并备份 SQLite（含 WAL）与工作区。
本轮未新增列、未改写数据，因此回滚不需要数据迁移。

**不要**为了让取消「看起来立刻完成」而把状态提前写成终态；
**不要**为了清盘而按文件名模糊删除历史产物；**不要**全局结束 `ffmpeg.exe`。
