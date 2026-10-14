# Travel Storyboard v2.0.5 — 深路径收尾与取消语义

> 基线：`main` @ `77533bd`（v2.0.4 + TSB-251/252/254）
> 范围：缺陷修复与回归验证（不含 v2.1 的全素材 AI 导演设计）

本轮对应 TSB-251 ~ TSB-254。其中 TSB-251 / TSB-252 / TSB-254 已在基线提交，
**TSB-253 的代码在上一轮遗留为未提交状态且没有任何回归测试**，本轮补齐测试、
修正两处漏接线并提交。**TSB-246（Windows 实机真实素材端到端验收）仍未执行**，
详见文末。

---

## TSB-251 深路径 HTTP 读取（P1，已在基线提交）

`api/media.py` 用普通 `Path.is_file()` 判断产物是否存在，在 Windows 上对
大于 `MAX_PATH` 的文件返回 `False`：文件与数据库引用都在，路由却回 404。

排查发现问题的**第二半**：Starlette 的 `FileResponse` 自身也会对长路径抛
`RuntimeError("File ... does not exist")`，所以只修存在性判断会把 404 变成 500。
两半都在 `test_deep_media_http.py` 中守护：

- 长路径产物可正常返回；
- 越界、白名单外的子目录、扩展名不允许、`..` 穿越仍然全部拒绝；
- 扩展长度前缀**绝不**泄漏给客户端。

## TSB-252 安全删除（P1，已在基线提交）

清理逻辑删除产物时同样受长路径影响，统一走安全封装。`test_deep_path_cleanup.py`
覆盖深路径下"删除确实生效"且**不触碰原片**。

## TSB-253 取消必须终止在跑的媒体进程（P1 — 本轮完成）

**问题**：取消此前是**纯协作式**的 —— `is_cancelled()` 只在**条目之间**被检查。
取消一个长编码（10 分钟代理、粗剪 concat）时，任务行立刻变成 `cancelled`，
但 FFmpeg 子进程会继续跑完：项目锁一直被占、CPU 持续占用，有时长达数分钟。
用户看到"已取消"，实际却要等到整个片段编码结束才能提交新任务。

**修复**：把 `cancel_check` 回调从任务一路传进 `ffmpeg.run()`。

| 层 | 处理 |
| --- | --- |
| `ffmpeg.run` | 新增可选 `cancel_check` / `poll_interval`。有回调时改用 `Popen` + 轮询循环；回调为真则终止进程组并抛 `FFmpegCancelled`（`code="CANCELLED"`） |
| 快路径 | **不传回调时行为与原先完全一致** —— 仍走 `subprocess.run`，无额外线程与轮询 |
| 进程组 | Windows `CREATE_NEW_PROCESS_GROUP`；POSIX `start_new_session`。先 `terminate()`，`CANCEL_GRACE_SEC`(5s) 后 `kill()` |
| 超时 | 轮询循环内同样判定超时，抛 `FFmpegTimeout`（`code="TIMEOUT"`） |
| 管道 | `communicate(timeout=)` 在后台线程排空两路管道，避免输出写满导致死锁 |
| `runner` | 新增 `cancel_check_for(job_id)`；无任务时返回 `None`，一次性检测保持原路径 |
| 终态 | `code == "CANCELLED"` 的异常记为**取消**而非失败；`cancelled` 不再被 `failed` 覆盖 |

**关键设计**：终止**只作用于自己启动的进程树**。按项目或按进程名杀会波及
其他项目的并发任务，这是任务书明令禁止的，因此 `_terminate_tree` 只持有
自己的 `Popen` 句柄。`test_cancelling_one_run_does_not_kill_a_concurrent_run`
用一个"旁观者"子进程钉死这条边界。

**本轮补的两处漏接线**（原实现只改了 5 个文件，但漏了生产调用点）：

1. `story_planner.proxies_task` —— 代理是全应用**最长**的单个编码，
   却没传 `cancel_check`，取消仍要等每个片段编码完。已接线，
   并补 `except FFmpegCancelled`：否则被杀掉的编码会被记成**一条失败请求**
   并继续编码下一个。
2. `analyzer.detect_task` / `_analyze_asset` —— 抽样与抽帧已传回调，但
   `detect_task` 的 `except Exception` 会把 `FFmpegCancelled` 记成**单个素材失败**
   后继续下一个素材。已改为向上抛 `JobCancelled`。

`roughcut_task` 在基线中已有同样的 `except FFmpegCancelled` 处理，本轮为其
补上守卫用例，防止回归。

**时延才是关键指标**：旧实现同样会到达 `cancelled`，所以只断言终态是**测不出**
这个缺陷的。新增用例因此同时断言"停得够快"与"子进程在操作系统层面真的死了"。

实测（`tools/probe_cancel_latency.py`，同一 8 秒媒体命令，0.6 秒后请求取消）：

```
child would run for      : 8.0s
cancel requested after   : 0.6s
OLD (no cancel_check)    : 8.44s  <- waited the encode out
NEW (cancel_check)       : 0.73s  <- stopped at the cancel
time reclaimed           : 7.71s
```

在真实的 10 分钟素材上，这对应的是**分钟级**的无效占用。

## TSB-254 通过正式 API 扫描深层源目录（P2，已在基线提交）

`test_deep_scan_api.py` 覆盖"源目录本身也在超长路径下"时，扫描走正式
HTTP API 仍能发现素材并正确报失败。

---

## 验证结果

| 项目 | 结果 |
| --- | --- |
| 后端测试 | **398 passed / 0 failed / 0 skipped**（基线 376 + TSB-253 新增 22） |
| 新增测试文件 | `test_job_cancel_kills_process.py`(22) |
| 本机环境 | Windows / Python 3.13.7 / FFmpeg 9.0.2 / `LongPathsEnabled=0` |
| 前端 | `pnpm run typecheck` 与 `pnpm run lint` 均通过 |
| 回归 | 无既有用例被破坏（376 → 398，新增 22 全部通过） |

新增测试全部**先复现问题**再随修复转绿：

- 在基线上，`FFmpegCancelled` / `cancel_check_for` 尚不存在，
  新用例模块在**收集阶段即 ImportError** —— 证明这些符号是本次引入的契约；
- 时延探针在旧路径 8.44s、新路径 0.73s；
- 接线用例经**反向验证**：临时移除 `proxies_task` 的 `cancel_check` 后，
  `test_proxy_task_passes_a_cancel_check_to_its_encodes` 立即失败并给出
  "proxies_task no longer builds a cancel callback" —— 证明它不是空断言。

---

## 尚未完成：TSB-246（发布门禁）

**本轮不包含，也不能由 CI 代替：**

- Windows 实机 + **8～10 段真实旅行素材**端到端验收（RT-18）
- 真实长片 / 横竖屏 / 音轨混合素材验收（RT-19）
- 60+ 视频 / 200GB 全量性能验收

[`docs/validation/v2.0.4/WINDOWS_REAL_FOOTAGE.md`](../validation/v2.0.4/WINDOWS_REAL_FOOTAGE.md)
中的 RT-18 / RT-19 仍为「未执行」。在这些记录补齐之前，
**v2.0.5 不应宣称已满足真实素材发布门禁**。

CI 全绿只证明自动化与合成素材路径通过，不构成实机验收证据。

---

## 范围声明

本轮**未**改动：镜头检测算法、模型 Prompt、故事线规划、粗剪叙事逻辑。

**未**迁移或改写任何既有产物路径与数据库引用；**未**删除或修改用户原片、
历史分析包、人工评分/备注/裁剪、ReviewRequest 历史、EditPlanShot 与已发布代理。

取消语义的改动**只**影响"谁在何时被终止"：产物发布、时长验收与历史保留逻辑
一律未动，`cancelled` 任务同样不发布半成品（`raise_if_cancelled` 在发布前仍会再查一次）。
