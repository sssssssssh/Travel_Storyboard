# Travel Storyboard v2.0.6 — 后端测试记录

- 主机：Windows（`LongPathsEnabled=0`，即任务书验收要求的场景）
- Python 3.13.7 / FFmpeg 9.0.2 essentials build（`bin/ffmpeg/ffmpeg-9.0.2-essentials_build/bin`）
- 基线：`main` @ `74b372d3545ac3308827b1113088d8901240d66a`
- 本轮：TSB-261 / TSB-262 / TSB-263 / TSB-264

## 汇总

```
tests=456  failures=0  errors=0  skipped=0  time=~165s
```

**注意 `skipped=0`**：`TSB_REQUIRE_FFMPEG=1` 下媒体用例**实际执行**而非跳过；
深路径与 Windows 专属用例带 `requires_windows` 标记，在本机同样实际执行。
若依赖缺失或有用例被静默跳过，`conftest.py` 会让整个 run 失败。

## 用例数构成

| 来源 | 用例数 | 说明 |
| --- | --- | --- |
| v2.0.5 基线 | 398 | 行为未改动 |
| `test_cancel_lifecycle.py` | 14 | TSB-261 新增 |
| `test_cancel_cleanup.py` | 21 | TSB-262 新增 |
| `test_media_process_tree.py` | 10 | TSB-263 新增 |
| `test_cancel_recovery.py` | 10 | TSB-264 新增 |
| `test_ci_skip_guard.py` | 3 | TSB-266 新增（CI skip 守卫自覆盖） |
| **合计** | **456** | |

## CI 结果（跨平台）

主线 `7e2fb7d` 的 GitHub Actions **7/7 成功**：
Backend（3.11/3.13 × Ubuntu/Windows）、Required FFmpeg（Ubuntu/Windows）、Frontend。
运行链接：<https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38540406730>

**Ubuntu 媒体作业曾失败并被修复**：把深路径套件加入该作业后，其 Linux 合法 skip
（原因 `"MAX_PATH only constrains Windows"`）未被 `_EXPECTED_SKIP_REASONS` 识别，
在 `TSB_REQUIRE_FFMPEG=1` 下被当成意外 skip 而失败。修复与守卫见发布文档 §5.2。

## 复现

```powershell
cd backend
$env:PATH = "F:\work\project\Travel_Storyboard\bin\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;$env:PATH"
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest -q --junitxml=tsb.xml
# tests=456 failures=0 errors=0 skipped=0
```

按组运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cancel_lifecycle.py -q     # 14
.\.venv\Scripts\python.exe -m pytest tests/test_cancel_cleanup.py -q       # 21
.\.venv\Scripts\python.exe -m pytest tests/test_media_process_tree.py -q   # 10
.\.venv\Scripts\python.exe -m pytest tests/test_cancel_recovery.py -q       # 10
.\.venv\Scripts\python.exe -m pytest tests/test_ci_skip_guard.py -q         # 3
```

按 `ci.yml` 的强制媒体作业清单实跑（本机 **386 passed / 0 skipped**）：

```powershell
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_ffmpeg_integration.py tests/test_cache_recovery.py `
  tests/test_stability_jobs.py tests/test_checkpoints.py tests/test_story_direct.py tests/test_output_dir.py `
  tests/test_concurrent_annotations.py tests/test_hash_pipeline.py tests/test_review_validation.py `
  tests/test_review_proxies.py tests/test_review_package_privacy.py tests/test_database_backup.py `
  tests/test_long_paths.py tests/test_job_mutex.py tests/test_proxy_duration.py `
  tests/test_job_cancel_kills_process.py tests/test_deep_media_http.py tests/test_deep_path_cleanup.py `
  tests/test_deep_scan_api.py tests/test_cancel_lifecycle.py tests/test_cancel_cleanup.py `
  tests/test_media_process_tree.py tests/test_cancel_recovery.py tests/test_ci_skip_guard.py
```

独立观察手段（不依赖 pytest）：

```powershell
.\.venv\Scripts\python.exe tools\probe_cancel_tree.py       # 取消是否回收整棵树
.\.venv\Scripts\python.exe tools\probe_process_tree.py      # 进程链实测（含 pid 归属观察）
.\.venv\Scripts\python.exe tools\probe_skip_guard.py        # 平台 skip 是否会被 CI 误判
.\.venv\Scripts\python.exe tools\check_free_names.py app\services\analyzer.py _analyze_asset_inner
```

## 修复前状态（说明测试确实守护了这些缺陷）

- **TSB-261**：cancel 直接写终态，互斥只认 `pending/running` → 旧 worker 仍持锁时
  新互斥任务可被接受。
- **TSB-262**：`roughcut` 的 `rmtree` 在成功路径末尾，取消/超时/失败均提前返回 → 暂存泄漏。
- **TSB-263**：只对直接子进程 `terminate()` → 孙进程存活（已用探针实测观察到孤儿孙进程）。

## 反向验证（证明用例不是空断言）

| 撤去的保护 | 观察到的失败 |
|---|---|
| `cancel_job` 改为写终态 | `cancel must not jump straight to a terminal state, got cancelled`；`new_exclusive_job_is_rejected_while_cancelling` 同时失败 |
| 清理跟随 reparse point | `cleanup followed a link out of the project and deleted outside data` |
| 禁用 Job Object + 进程组终止 | 探针**卡住**，进程表可见存活的孤儿孙进程 |

三项均在恢复后重新转绿。

## 本轮由测试发现并修复的真实缺陷

1. **链接逃逸（严重）**：清理会跟随 junction 删除项目外数据 —— 违反「外部数据不可删除」。
2. **深路径静默空目录**：`FileNotFoundError` 被先当作「空目录」，导致深路径 stage
   清理报告成功却什么都没删。
3. **计数不诚实**：`ok=True, removed_files=0` 出现在目录已被完全删除时。
4. **函数拆分遗留自由变量**：`_analyze_asset` 拆分后遗留
   `run_token` / `source` / `context` / `verify_*`，运行时才逐条暴露；
   已加 `tools/check_free_names.py` 静态守卫。

## 未覆盖

- **TSB-265 真实素材实机验收未执行**，见
  [`WINDOWS_REAL_FOOTAGE.md`](WINDOWS_REAL_FOOTAGE.md)。
- 本文件仅记录自动化与合成素材结果，**不构成实机发布验收证据**。
- 具体地，以下只有真实素材/受限环境才能确认：
  - 真实 10 分钟 / 4K / HEVC / VFR 素材上取消的**实际**时延；
  - 真实长时间编码中取消后，项目锁释放与后续任务提交的用户体感（手动项 T20/T21/T22）；
  - Windows Job Object 在**嵌套 Job 受限**主机上降级路径的行为。
