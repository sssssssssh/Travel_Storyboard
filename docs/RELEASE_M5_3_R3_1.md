# M5.3-R3.1 模型预算与回退闭环补丁发布记录（TSB-351 / 352 / 353 / 354 / 355）

**审阅基线 commit：** `main@f7ed5d349a0b1366f941eb40e84415cdf9fa67fd`（与任务书冻结基线逐字符一致）
**本批终态 commit：** `main@9401297`（代码终态 `01e522c`，其后仅追加本记录）
**机器（本机测量）：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**CI：** **7/7 success**，两个 SHA 各跑一次：[39038660081](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39038660081) @ 代码终态 `01e522c`、[39039301986](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39039301986) @ 文档终态 `9401297`
**批次：** P1-A（TSB-351、352）→ P1-B（TSB-353）→ P1-C（TSB-354）→ P1-D（TSB-355）

---

## 〇、结论

**「R3.1 代码级整改通过，发布门禁未完成。」**

| 门禁 | 内容 | 状态 |
| --- | --- | --- |
| **G1** | 静态检查 + 单元测试（0 failure） | **PASS** — 后端 1306/0/0/44；前端四项全绿 |
| **G2** | 同一 SHA 的 CI 7/7 | **PASS** — `01e522c`，[run 39038660081](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39038660081) |
| **G3** | Mock E2E（API → Mock → Observation → Cleanup → 回退执行） | **PASS** |
| **G4** | 数据安全（原片只读、历史与人工判定不覆盖） | **PASS** |
| **G5** | 真实端点视频能力 | **`blocked_by_endpoint`** — 见第六节 |
| **G6** | 真实 A/B（原生视频 vs 抽帧） | **`not_evaluated`**（依赖 G5） |

在 G5、G6 完成前**不得**声称「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。

---

## 一、提交清单

| # | Commit | 任务 | 摘要 |
| --- | --- | --- | --- |
| 1 | `a5f812d` | TSB-351、TSB-352 | 单次 HTTP 尝试只扣一次，按真实 Body 字节计费 |
| 2 | `be913ab` | TSB-353 | 任务级重试与超时真正生效，且任务之间不串扰 |
| 3 | `01e522c` | TSB-354 | 回退父子任务追踪、终态与 UI 不再误报已完成 |

CI 作业明细（[run 39038660081](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39038660081)）：
Backend 3.11/3.13 × ubuntu-latest、Backend 3.11/3.13 × windows-latest、
Required FFmpeg × ubuntu/windows、Frontend —— **7/7 success**。

起点 `f7ed5d3` 与任务书冻结基线逐字符一致，工作树干净。

---

## 二、逐任务：问题 → 修复 → 验收

### TSB-351（P0）单次 HTTP 尝试只能扣减一次

**问题（RED）—— R3 自己引入的缺陷**

R3 的 TSB-347 把计费从 Job 层移到 Provider 的 `attempt_guard`，却**没有删掉原有的
`charge_attempt(0)` 预扣**。于是：

* `_analyze_one` 先执行有副作用的 `charge_attempt(0)`；
* Provider 每次 `httpx.post` 前再调用 `attempt_guard`；
* ⇒ **一次真实 HTTP 尝试被计费两次**。

`max_calls=1` 时预扣就把唯一额度用光，**第一条请求在真正发出前被自己的预扣拒绝**。
实测与任务书预测的形态完全吻合：

```
pre-charge charge_attempt(0): estimate=1618 -> ledger.calls=1
raised: MODEL_BUDGET_EXCEEDED
ACTUAL POSTs at Mock server : 0   <- 期望 1
ledger.calls                : 1
```

**修复**

* 删除有副作用的预扣。**唯一计费点**位于 Provider 内部、紧挨每次真实
  `httpx.post` 之前，每次重试重新调用。
* 新增**纯检查** `VideoBudget.can_attempt()`：返回 `(bool, reason)`，
  **不改变** calls/bytes —— 预检不得成为耗尽额度的原因。

**验收**

| 用例 | 修复前 | 修复后 |
| --- | --- | --- |
| B01 配额 1、模型 200 | POST=0 ❌ | **POST=1、calls=1** ✅ |
| B01b 回调调用次数 | 2 ❌ | **1** ✅ |
| B02 配额 1、503 | ❌ | **POST=1**，重试被预算拒绝 ✅ |
| B03 配额 2、503→200 | ❌ | **POST=2、calls=2** ✅ |
| B04/B05 授权拒绝/非法部件 | — | **0 POST、0 计费** ✅ |

### TSB-352（P1）以真实序列化 Body 计费

**问题** `_request_body_bytes()` 用 Base64 长度 + prompt 长度 + 固定 `+512`
**估算**，而 Provider 实际发送 `json.dumps(payload).encode("utf-8")`。
两者不保证相等，台账与真实 HTTP Body 脱节。

**修复** `attempt_guard` 契约改为 `(attempt, exact_body_bytes)`，由 Provider 传入
自己的 `len(body_bytes)` —— 即真正作为 `content=` 发出的字节。计费路径不再引用
本地估算函数，**消灭两套独立算法**。

**验收**

| 用例 | 断言 |
| --- | --- |
| B07/B11 | 预留字节 **== Mock 实收 body 长度**（实测 1295 == 1295） |
| B08 | 精确 Body 为 B、配额 B-1 ⇒ **0 POST** 且稳定预算错误 |
| B08b | 配额恰为 B ⇒ 允许 1 次（**不过度拒绝**） |
| B09 | 中文 prompt 按 UTF-8 精确计量 |
| B09b | 计费路径不得再出现独立估算函数 |

### TSB-353（P1）冻结并执行 Job 级重试次数、超时

**问题（RED）** `NativeVideoVisionProvider.__init__` 从 `Settings` 读
`model_max_retries` / `model_timeout_sec`，而 Job 快照里的值只进了 `VideoBudget`，
**从未传给 Provider**。实测：

```
provider.max_retries = 2     <- 来自 settings，不是任务快照
provider.timeout_sec = 120.0 <- 来自 settings，不是任务快照
job snapshot max_retries = 0   （被忽略）
job snapshot timeout_sec = 7.5 （被忽略）
```

后果：快照记录"0 次重试"的任务实际仍按 settings 重试；快照记录"7.5 秒超时"的
任务实际用 120 秒 —— **快照与真实执行不一致**。

**修复**

* 新增不可变 `CallOptions(max_retries, timeout_sec)`，**按调用传入**，
  绝不写入共享 Provider 的 `self` —— 否则并发任务会互相覆盖。
* `CallOptions.resolve()` 用 **min** 钳制在服务端上限内：任务只能要得**更少**。
* 快照新增 `max_calls` / `max_retries` / `timeout_sec`（Pydantic 边界校验）。
* `VideoBudget` 语义修正：快照**缺键**表示"继承上限"，而非"用本地默认值"。

**验收**

| 用例 | 断言 |
| --- | --- |
| B13 | 快照 `max_retries=0`、持续 503、Provider 自身值 99 ⇒ **只发 1 个 POST** |
| B14 | 快照 `max_retries=2`、Provider 自身值 0 ⇒ **发 3 个 POST** |
| B15 | A(3s)/B(11s) 的 `httpx.post(timeout=)` 分别为 3.0 / 11.0 |
| B15b | **次序互换**结果仍为各自的值 —— 无共享可变状态污染 |
| B15c | 请求 99 次重试 / 9999 秒 ⇒ 被钳制为上限 |
| B15d | 不得在调用中改写共享实例属性 |
| B15e | **接线**：调用点必须传 `CallOptions`，取自 `plan.budget` |

### TSB-354（P1）回退父子 Job 追踪、终态与真正执行 E2E

**问题（RED，三处）**

1. `_schedule_fallback` 只返回 `(scheduled, error)`，**没有后继 Job ID**。
2. 父 Job 通过 `_record_failure` 记录回退 ⇒ 显示 `partial_failed`，
   **把预期降级说成故障**。
3. `fallback_state="scheduled"` 只在排队那一刻写入、之后永不更新；UI 仅展示
   "已回退"，把"仅已排队"读成"已分析完成"。

**修复**

* 新增 `FallbackSchedule(scheduled, job_id, job_type, error, state)` 携带**真实后继 ID**；
  `__iter__` 保持旧两元组解包可用。
* worker 把后继身份写入父 Job stats。
* **只有排不出回退才算失败**：降级原因仍通过 `phase`/`fallback_state`/
  `deferred_reason` 完整可见（C6：不算失败 ≠ 隐藏）。
* 新增 `resolve_fallback_state()`：**每次读取**时解析后继当前状态，
  `analysed` 仅在后继真正 `succeeded` 时为真。
* `JobOut` 新增 `FallbackOut`，详情与列表接口均附带。
* 前端新增 `FallbackTracking`，渲染后继 ID 与**实时**状态。

**验收**

| 用例 | 断言 |
| --- | --- |
| B16/B16b | 返回的 ID 必须指向**唯一那条**真实后继行 |
| B16c/B16d | 排不出来 ⇒ state=failed 无 ID；旧解包仍可用 |
| B17 | **断言 worker 源码**确实写入后继身份 |
| B18 | 六种后继状态逐一钉住：**只有 succeeded 的 analysed 为真** |
| B19/B20 | 无后继 / 后继行消失 ⇒ 永不报成已分析 |
| B21/B22 | 成功回退不算失败；降级原因仍可见 |
| B23/B23b | **真实 Runner 入口**执行后继并进入终态 |
| B24–B24d | API 与列表均带 fallback 块；无回退不伪造 |
| 前端 8 项 | 排队/运行/失败/取消均**不得**显示"已入库" |

### TSB-355（P0）回归、CI、文档、发布门禁

本记录即为交付物。门禁表见第〇节。

---

## 三、验证结果

| 项目 | 计数 |
| --- | --- |
| 后端全量（本机，含 `.env`） | **1306 collected / 0 failed / 0 error / 44 skipped** |
| 后端全量（**移走 `.env`**，CI 真实条件） | **1306 / 0 / 0 / 44** |
| CI 媒体作业（67 个文件，`TSB_REQUIRE_FFMPEG=1`） | **1222 / 0 / 0 / 44** |
| 前端 typecheck / lint / build | 通过 |
| 前端测试 | **79 passed**（R3 为 71） |

**本批新增用例**

| 套件 | 计数 | 覆盖 |
| --- | --- | --- |
| `test_video_budget_http_contract.py` | **18**（新建） | B01–B15e |
| `test_fallback_job_tracking.py` | **22**（新建） | B16–B24d |
| `VideoUnderstandingPanel.test.tsx` | +8（共 25） | 回退 UI 追踪 |

**反向验证 6 次，全部确认非空断言**

| 任务 | 反向操作 | 结果 |
| --- | --- | --- |
| TSB-351/352 | 恢复有副作用预扣 | B01 等 **10 RED** |
| TSB-353 | Provider 忽略按调用选项 | B13/B15/B15b **3 RED** |
| TSB-354 | 删掉 worker 的 `fallback_job_id` | B17 **RED** |
| TSB-354 | 恢复无条件 `_record_failure` | B21 **RED** |
| TSB-354 | 移除前端 `<FallbackTracking>` | **7 项前端 RED** |
| TSB-354 | 前端类型/组件语义 | 见上 |

> 计数口径：`collected` 含 skip，`passed` 不含。全部数字来自 junitxml 实测。

---

## 四、范围声明

**本轮改动**

* `backend/app/services/vision/providers/native_video.py`（TSB-351/352/353）
* `backend/app/services/vision/video_job.py`（TSB-351/352/353/354）
* `backend/app/schemas.py`（TSB-353 请求字段、TSB-354 `FallbackOut`）
* `backend/app/api/vision.py`（TSB-353 快照）、`backend/app/api/jobs.py`（TSB-354）
* `backend/tests/`：上述两个新套件
* `frontend/src/lib/types.ts`、`components/VideoUnderstandingPanel.tsx` 及其测试
* 本记录与 `docs/validation/m5.3-r3.1/`

**本轮明确未做**

* **未**修改 `TRAVELSB_MODEL_MODE` 生产默认（仍为 `off`）。
* **未**在 CI 注入真实 Key，**未**导出全局模型开关。
* **未**删除或改写任何原片、历史 `SegmentSemanticResult`、
  `SegmentMotionReview`、人工评分、历史 `EditPlanShot`。
* **未**重新设计 M5 结构化视觉 Schema。
* **未**调整 PAI-EAS/vLLM 部署、密钥、代理或公网权限。
* **未**为了通过测试删除断言、添加 `xfail`/`skip`、放宽 skip 白名单、
  修改断言以迎合缺陷，或只跑部分套件。
* **未**将请求 Body / Base64 / Key 写入日志。
* **未**跳过 Linux/Windows 原有强制媒体测试。
* **未**开始 M6 AI 导演。

**尚未完成（诚实列出）**

1. **G5 真实端点联调**：端点此前全线 HTTP 503 `no healthy upstream`
   （文本/图片/视频一律），属外部部署状态。
2. **G6 真实 A/B（20–30 镜头）**：依赖 G5。
3. **Windows 实机手工确认**：CI Windows 作业通过，但未做真机人工走查。
4. **`test_vision_commit_races.py` Ubuntu 间歇竞争**：未复现，**未** xfail/skip，
   保留为观察项。

---

## 五、估算与实测的对照（任务书 §5 要求）

| 项目 | R3 估算 | R3.1 实测 |
| --- | --- | --- |
| 单次尝试预留字节 | `_request_body_bytes()` 推算 | **Mock 实收 1295 B == 台账 1295 B** |
| `max_calls=1` 的实际 POST 数 | — | **1**（修复前 **0**） |
| 503 后重试的计费次数 | 只记 1 次 | **每次重试各记 1 次** |
| 任务快照重试/超时是否生效 | 否（被忽略） | **是**（B13/B14/B15） |
| 回退后继可追踪 | 无 ID | **有真实 ID**（B16） |

---

## 六、G5 真实端点状态（`blocked_by_endpoint`）

端点 `GLM-5.3-Flash`（PAI-EAS）观察：`/v1/models` 曾返回 HTTP 200；
此后所有 `/chat/completions`（**文本、图片、视频一律**）返回 HTTP 503
`no healthy upstream`。

任务书明确：**文本/视频同为 503 不等于视频能力通过或不通过**。探测工具在同样
条件下行为正确：报 `inconclusive`，保持 `unverified`/`sendable: False`，
**未写入能力记录**。

因此 G5 = `blocked_by_endpoint`（外部部署状态，**不构成**视频能力的任何证据）；
G6 依赖 G5 ⇒ `not_evaluated`。

> 凭据安全：全仓库 `git grep` 该 token **0 次命中**；`backend/.env` 由
> `.gitignore:23` 忽略，CI 无该文件。本批新增用例全部使用回环 Mock 与
> 明显的占位 Key，且**不将 Body/Base64/Key 写入日志**。
>
> 明文 HTTP 提示：该端点为 `http://` 且非回环。任务书禁止生产中明文上传原片。

---

## 七、证据文件

| 文件 | 内容 |
| --- | --- |
| `docs/validation/m5.3-r3.1/backend-full-with-env.xml` | 本机全量（含 `.env`） |
| `docs/validation/m5.3-r3.1/backend-full-no-env.xml` | 无 `.env`（CI 真实条件） |
| `docs/validation/m5.3-r3.1/media-job-67-files.xml` | CI 媒体作业（67 文件） |
| `docs/validation/m5.3-r3.1/ci-run-39038660081.json` | CI 7/7 作业明细（`01e522c`） |
| `docs/validation/m5.3-r3.1/ci-run-39039301986.json` | CI 7/7 作业明细（`9401297`，终态） |

详见 [docs/validation/m5.3-r3.1/README.md](validation/m5.3-r3.1/README.md)。
