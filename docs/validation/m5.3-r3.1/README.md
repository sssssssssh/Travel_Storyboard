# M5.3-R3.1 验证证据

**终态 commit：** `main@01e522c`
**审阅基线：** `main@f7ed5d349a0b1366f941eb40e84415cdf9fa67fd`（与任务书冻结基线一致）
**机器（本机测量）：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**CI：** [run 39038660081](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39038660081) — **7/7 success**

本目录保存 R3.1 的**原始 junitxml 与 CI 运行记录**，不是转录数字。
发布记录中的每个计数都可在此复核。

---

## 一、文件清单

| 文件 | 内容 | 计数 |
| --- | --- | --- |
| `backend-full-with-env.xml` | 后端全量（本机，含 `backend/.env`） | tests=1306 failures=0 errors=0 skipped=44 |
| `backend-full-no-env.xml` | 后端全量，**移走 `backend/.env`**（CI 真实条件） | tests=1306 failures=0 errors=0 skipped=44 |
| `media-job-67-files.xml` | CI 媒体作业清单（**67 个文件**，`TSB_REQUIRE_FFMPEG=1`） | tests=1222 failures=0 errors=0 skipped=44 |
| `ci-run-39038660081.json` | GitHub Actions 运行记录与 7 个作业明细 | 7 jobs / 7 success |

复核命令（`cd backend`）：

```bash
../.venv/bin/python -m pytest -q --junitxml=out.xml
../.venv/bin/python - <<'PY'
import xml.etree.ElementTree as ET
r = ET.parse('out.xml').getroot()
s = r.find('testsuite') if r.find('testsuite') is not None else r
print('tests=%s failures=%s errors=%s skipped=%s' % (
    s.get('tests'), s.get('failures'), s.get('errors'), s.get('skipped')))
PY
```

> 计数口径：`tests` 含 skip，`passed` 不含。**不要**把 `tests` 报成 "N passed"。

**CI 条件等价性**：含 `.env` 与移走 `.env` 两次运行计数**完全一致**
（1306/0/0/44），证明本批新增用例不依赖开发机配置。CI 无 `.env`。

---

## 二、TSB-351 缺陷的确定性复现（修复前）

任务书预测"当前代码预计 POST=0、ledger.calls=1"。实测完全吻合：

```
pre-charge charge_attempt(0): estimate=1618 -> ledger.calls=1
raised: MODEL_BUDGET_EXCEEDED
ACTUAL POSTs at Mock server : 0   <- 期望 1
ledger.calls                : 1
```

修复后同一路径：

```
ACTUAL POSTs at Mock server : 1
ACTUAL body bytes           : [1295]
ledger.calls                : 1
ledger.bytes                : 1295
POST==1: True | calls==1: True | bytes==real body: True
```

**根因**：R3 的 TSB-347 把计费移进 `attempt_guard` 后**未删除**原有的
`charge_attempt(0)`，该预扣本身有副作用（`note_attempt` 是写操作），
于是同一次 HTTP 尝试被扣两次；`max_calls=1` 时预扣即耗尽额度。

---

## 三、反向验证记录（6 次）

"先复现、再转绿"的每一步都做了反向验证：**临时把修复去掉，确认对应用例转红，
再恢复**，以排除"空断言式的绿"。

| 任务 | 反向操作 | 期望转红 | 实测 |
| --- | --- | --- | --- |
| TSB-351/352 | 恢复有副作用的预扣 | B01 等 | ✅ **10 RED** |
| TSB-353 | Provider 忽略按调用选项 | B13/B15/B15b | ✅ **3 RED** |
| TSB-354 | 删掉 worker 的 `fallback_job_id` | B17 | ✅ **1 RED** |
| TSB-354 | 恢复无条件 `_record_failure` | B21 | ✅ **1 RED** |
| TSB-354 | 移除前端 `<FallbackTracking>` | 前端 7 项 | ✅ **7 RED** |
| TSB-354 | 前端回退语义 | 见上 | ✅ |

全部恢复后套件全绿。

---

## 四、预算—实测对照表

| 项目 | 修复前 | 修复后 |
| --- | --- | --- |
| 配额 1 次的实际 POST 数 | **0** | **1** |
| 一次 HTTP 尝试的计费次数 | **2** | **1** |
| 预留字节 vs 真实 Body | 估算，不保证相等 | **1295 == 1295** |
| 503 后每次重试是否计费 | 只记 1 次 | **每次各记 1 次** |
| 任务快照重试/超时是否生效 | **否** | **是** |
| 回退后继是否有可追踪 ID | **否** | **是** |

---

## 五、约束与安全

* 新增用例全部使用**回环 Mock HTTP 服务**与明显的占位 Key。
* **不将请求 Body / Base64 / Key 写入日志**。
* CI 未注入真实 Key；`ci.yml` 不导出全局 `TRAVELSB_MODEL_MODE`。
* 生产默认仍为 `TRAVELSB_MODEL_MODE=off`；swich 只在调用测试内开启。
* 全仓库 `git grep` 该 token ⇒ **0 次命中**；`backend/.env` 由 `.gitignore:23` 忽略。
* 未删除断言、未新增 `xfail`/`skip`；skip 仍为登记在册的 44 项平台事实。

---

## 六、未完成事项（与发布记录一致）

1. **G5 真实端点**：`blocked_by_endpoint` —— 端点全线 HTTP 503
   `no healthy upstream`（文本/图片/视频一律），属外部部署状态，
   **不构成**视频能力通过或不通过的证据。
2. **G6 真实 A/B（20–30 镜头）**：`not_evaluated`，依赖 G5。
3. **Windows 实机手工确认**：CI Windows 作业通过，但未做真机人工走查。
4. **`test_vision_commit_races.py` Ubuntu 间歇竞争**：本批 CI 全绿、未复现，
   **未** xfail/skip；保留为观察项。

**在 G5、G6 完成前不得声称**「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。本批结论措辞为
**「R3.1 代码级整改通过，发布门禁未完成」**。
