# TSB-369-D 真实项目只读预验收与模型门禁

**性质：只读。** 全程未写数据库、未上传素材、未调用任何外部模型。
真实库 `M5-real-acceptance`，2026-11-10 复核。

---

## D-01 真实库只读预验收

### 1. 素材库覆盖（实时读取，非硬编码）

| 项 | 值 |
|---|---|
| Asset | 6 |
| Segment | **99**（全部 `candidate` / `pending`） |
| 有任意可靠读数 | **10** |
| 无有效模型读数 | **89** |
| 静帧有效 | 8 |
| 多帧有效 | 4 |
| **原生视频有效** | **0** |
| 人工标注 | **0** |
| 锁定镜头（base 版本内） | **0** |

**约 10/99 的有限证据**，与任务书 §1.3 的历史快照一致。

### 2. 首页第一屏必提示的三件事

| 要求 | 实现 | 实测 |
|---|---|---|
| 部分证据 | `coverage-line` 显示「有可靠读数 10 个」 | ✅ |
| 未分析镜头数 | 同显示「无有效读数 89 个」 | ✅ |
| 原生视频是否为零 | 同显示「原生视频有效 0 个」 | ✅ |

**未在任何位置声称已了解整个旅行。**

### 3. 逐项证据核对（3–5 个有真实读数的候选）

取前 5 个 `has_usable_reading == True` 的候选逐一核对证据出处、时间窗与理由。

| # | segment | 时间窗 | 静帧 result | 静帧证据状态 | 信道状态 | 音频 | 内容标签 |
|---|---|---|---|---|---|---|---|
| 1 | `4b482acf` | 0.0–1.0s | `e7cd9614` | `observed` | `freshness_unverified` | 未分析 | transport / street / urban / sky |
| 2 | `f05b21df` | 0.0–4.0s | `db8651a3` | `observed` | `freshness_unverified` | 未分析 | nature / water / sky / transport |
| 3 | `6f1ace17` | 4.0–11.0s | `bc3735fd` | `observed` | `freshness_unverified`，**多帧 `pan`** | 未分析 | nature / water / monument |
| 4 | `b9a4242b` | 0.0–1.0s | `3ad6e89e` | `observed` | `freshness_unverified` | 未分析 | street / urban / architecture |
| 5 | `6b46d94d` | 0.0–2.0s | `c85e4b98` | `observed` | `freshness_unverified` | 未分析 | indoor / religious |

核对结论：

| 核对项 | 结果 |
|---|---|
| 每条读数都有 `result_id`，可回读原生端点复核 | ✅ |
| **无一条被冒称为 `current`**，全部如实标 `freshness_unverified` | ✅ |
| 多帧仅 #3 有结论（`pan`），其余 `not_analyzed` | ✅ |
| 原生视频全部 `not_analyzed`（events=0） | ✅ |
| 音频全部未分析（`audio=False`） | ✅ |
| 限制项逐条列出（`multiframe_not_analyzed` / `video_native_not_analyzed`） | ✅ |
| **模型自报 confidence 未被当作事实准确率** | ✅ 从未进入草案 |

**逐帧人工观看不可执行** → `MANUAL_REVIEW_PENDING`。

### 4. 只读前后一致性（计数 + 内容摘要）

| 表 | 开工 | 验收后 |
|---|---|---|
| Storyboards | 1 | 1 |
| StoryboardVersions | 1 | 1 |
| EditPlanShots | 0 | 0 |
| PlanOperations | 0 | 0 |
| Jobs | 23 | 23 |
| UserAnnotations | 0 | 0 |

**全部一致 → 只读通过。**

浏览器 UAT 另外独立验证了同一点（见 [REAL_LIBRARY_UAT.md](REAL_LIBRARY_UAT.md) §3）。

### 5. 未执行项

| 项 | 原因 |
|---|---|
| `verify_freshness=true` 的源文件采样 | 任务书 D-01 要求默认不执行 |
| 逐帧人工观看 | 不可执行 → `MANUAL_REVIEW_PENDING` |
| 访问缺失原片 | 5/6 可访问，缺失 1 个，未触碰 |

---

## D-02 未授权真实模型的门禁

### 1. 当前状态

```
MODEL_UAT = BLOCKED (awaiting_explicit_approval_and_price_confirmation)
```

**不因离线草案与 Mock 测试成功而自行解锁。**

### 2. 门禁的代码实现

不是文档承诺，是**运行时拒绝**：

| 层级 | 实现 | 验证 |
|---|---|---|
| HTTP 层 | `planning_mode="model"` → **403**，在读取任何数据前拒绝 | `test_model_mode_is_403_with_no_network` |
| Provider 层 | `ExternalPlannerProvider.propose()` 抛 `ModelCallNotAuthorized` | `test_external_provider_makes_no_call_and_refuses` |
| 选择层 | `provider_for("mock")` 抛 `ValueError` | `test_mock_mode_is_not_selectable` |
| Schema 层 | `PlanningMode` Literal 只含 `offline_rules` / `model` | `test_invalid_payloads_are_422` |

**无任何 import 外部 SDK**：不装、不导、不初始化任何模型客户端。

### 3. 本轮确认未发送的数据

| 类型 | 是否发送 |
|---|---|
| 视频 | ❌ |
| 代表帧 | ❌ |
| 模型识别内容（标签/主体/摘要） | ❌ |
| 用户备注 | ❌ |
| 绝对路径 | ❌ |
| Token / API Key | ❌ |

### 4. 未来申请需单列的事项（尚未申请）

若要开启真实模型策划，需要你逐项确认：

| 项 | 需确认内容 |
|---|---|
| 服务端点/供应商 | 具体 URL 与归属 |
| 发送内容 | 仅文字摘要 / 是否含影像 |
| 段数与长度 | 单次请求覆盖多少镜头 |
| 地区 | 数据出境范围 |
| 请求次数 | 总量上限 |
| **最坏费用上限** | 单价 × 次数（**当前仓库无单价**） |
| 失败策略 | 超时/限流/能力不支持如何处理 |
| 敏感信息风险 | 用户备注可能含地点、人物 |

---

## 结论

| 项 | 状态 |
|---|---|
| D-01 真实库只读预验收 | ✅ **PASS** |
| D-02 未授权模型门禁 | ✅ **PASS**（持续 BLOCKED，门禁有效） |
| 真实模型效果 | 🔒 **BLOCKED** |

**不得因本轮通过而宣称 AI 创意能力已验收。**
