# Travel Storyboard v2.0.5 — 后端测试记录

- 主机：Windows（`LongPathsEnabled=0`，即任务书验收要求的场景）
- Python 3.13.7 / FFmpeg 9.0.2 essentials build（`bin/ffmpeg/ffmpeg-9.0.2-essentials_build/bin`）
- 基线：`main` @ `77533bd`
- 本轮：TSB-253 补齐测试与漏接线修复（TSB-251/252/254 已在基线提交）

## 汇总

```
tests=398  failures=0  errors=0  skipped=0  time=95.9s
```

**注意 `skipped=0`**：`TSB_REQUIRE_FFMPEG=1` 下媒体用例**实际执行**而非跳过，
深路径用例带 `@requires_windows` 标记，在本机（Windows）同样实际执行。
若依赖缺失或有用例被静默跳过，`conftest.py` 会让整个 run 失败。

## 用例数构成

| 来源 | 用例数 | 说明 |
| --- | --- | --- |
| v2.0.4 基线 | 329 | 行为未改动 |
| TSB-251 / 252 / 254 | 47 | 深路径 HTTP / 安全删除 / 深目录扫描 API |
| **本轮小计（基线）** | **376** | |
| `test_job_cancel_kills_process.py` | 22 | TSB-253 新增 |
| **合计** | **398** | |

## 复现

```powershell
cd backend
$env:PATH = "F:\work\project\Travel_Storyboard\bin\ffmpeg\ffmpeg-9.0.2-essentials_build\bin;$env:PATH"
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest -q --junitxml=tsb.xml
# tests=398 failures=0 errors=0 skipped=0
```

单独运行本轮新增：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_job_cancel_kills_process.py -q   # 22 passed
```

时延探针（说明缺陷确实存在、且已被消除）：

```powershell
.\.venv\Scripts\python.exe tools\probe_cancel_latency.py
```

## 修复前状态（说明测试确实守护了这些缺陷）

- **TSB-253**：取消只检查条目之间，正在跑的 FFmpeg **不会**被终止。
  终态确实是 `cancelled` —— 所以只断言终态**测不出**这个问题；
  必须断言"停止时延"与"子进程是否真的消失"。

原实现的 5 个文件改动**漏了两处生产调用点**：
`story_planner.proxies_task`（最长编码，完全没传回调）与
`analyzer.detect_task`（会把 `FFmpegCancelled` 记成单个素材失败后继续）。

## 反向验证（证明用例不是空断言）

### 1. 基线上新用例模块无法导入

在 `77533bd` 上运行 `tests/test_job_cancel_kills_process.py`：

```
ImportError: cannot import name 'ERR_CANCELLED' from 'app.ffmpeg'
ERROR tests/test_job_cancel_kills_process.py
```

说明 `ERR_CANCELLED` / `FFmpegCancelled` / `cancel_check_for` 都是本轮引入的契约。

### 2. 时延对比

```
child would run for      : 8.0s
cancel requested after   : 0.6s
OLD (no cancel_check)    : 8.44s  <- waited the encode out
NEW (cancel_check)       : 0.73s  <- stopped at the cancel
time reclaimed           : 7.71s
VERDICT: PASS - cancel now stops the process
```

### 3. 接线用例经反向验证

临时移除 `proxies_task` 中的 `cancel_check=cancel_check_for(job_id)` 后：

```
FAILED tests/test_job_cancel_kills_process.py::test_proxy_task_passes_a_cancel_check_to_its_encodes
AssertionError: proxies_task no longer builds a cancel callback
  — cancelling proxies would wait out each encode
```

恢复接线后重新转绿。

## 未覆盖

真实旅行素材（8～10 段）与 200GB 全量性能**未执行**，
详见 [`../v2.0.4/WINDOWS_REAL_FOOTAGE.md`](../v2.0.4/WINDOWS_REAL_FOOTAGE.md)。
本文件仅记录自动化与合成素材结果，**不构成实机发布验收证据**。

具体地，以下两项只有真实素材才能确认：

- 真实 10 分钟 / 4K / HEVC / VFR 素材上取消的**实际**时延（合成素材为 8 秒命令）；
- 真实长时间编码中取消后，项目锁释放与后续任务提交的用户体感（手动项 T20/T21）。
