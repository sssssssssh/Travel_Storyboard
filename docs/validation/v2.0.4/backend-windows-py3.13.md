# Travel Storyboard v2.0.4 — 后端测试记录

- 主机：Windows（`LongPathsEnabled=0`，即任务书验收要求的场景）
- Python 3.13 / FFmpeg 9.0.2 essentials build
- 基线：`main` @ `ff89e6f69cf8dd8f07e79b60d81e8fb1564965e0`
- 本轮：TSB-241 / TSB-242 / TSB-243 / TSB-244

## 汇总

```
tests=323  failures=0  errors=0  skipped=0
```

**注意 `skipped=0`**：`test_long_paths.py` 中的深路径用例带
`@requires_windows` 标记，在本机（Windows）**实际执行**而非跳过。

## 用例数构成

| 来源 | 用例数 | 说明 |
| --- | --- | --- |
| 基线既有 | 258 | 行为未改动 |
| `test_long_paths.py` | 23 | TSB-241 新增 |
| `test_job_mutex.py` | 20 | TSB-242 新增 |
| `test_proxy_duration.py` | 22 | TSB-243 / TSB-244 新增 |
| **合计** | **323** | |

## 复现

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest --junitxml=tsb.xml -q --no-header --tb=no
# tests=323 failures=0 errors=0 skipped=0
```

按组运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_long_paths.py -q       # 23 passed
.\.venv\Scripts\python.exe -m pytest tests/test_job_mutex.py -q        # 20 passed
.\.venv\Scripts\python.exe -m pytest tests/test_proxy_duration.py -q   # 22 passed
```

## 修复前状态（说明测试确实守护了这些缺陷）

- **TSB-241**：深路径（≥270 字符）下抽帧 / 检测 / 粗剪全链路失败；
  且抽样帧**全部**不可读时会静默返回单个 `single` 镜头 —— 即任务书
  明令禁止的「静默跳过全部图片后当作单镜头成功」。
- **TSB-242**：`/review-requests/proxies` 连续调用会建立多个排队 Job。
- **TSB-243**：错误时长的代理仍被标记 `proxied` 并发布。
- **TSB-244**：`version` 列宽 32 与实际生成的 69 字符不一致。

新增用例均先复现上述问题，再随修复转绿。

## 一处分类判断的回归

修复过程中曾把「编码产物不可读」也归为 `ReviewValidationError`，
使既有 `test_review_proxies.py::test_publish_faults_preserve_old_file_and_database[probe]`
由 `failed` 变为 `needs_review`。已修正为：**产物不可读属系统故障**（`failed`），
**仅时长不符属请求问题**（`needs_review`）。该既有用例现作为此分类的守卫。

## 未覆盖

真实旅行素材（8～10 段）与 200GB 全量性能**未执行**，
详见 [`WINDOWS_REAL_FOOTAGE.md`](WINDOWS_REAL_FOOTAGE.md)。
本文件仅记录自动化与合成素材结果，**不构成实机发布验收证据**。
