# TSB-369 Context Consumption Contract

**状态**：已定型，待 TSB-369 实施时遵循
**基线**：TSB-368-R1
**范围**：只定义下游如何取得镜头内容；**本轮不生成故事线、不调用模型**

---

## 0. 这份文档解决的问题

TSB-368 的 `/director/context` 返回值里，`candidate_summaries` 主要包含
**ID、时间窗、人工备注、优先级和三渠道 `result_id/state`**。

它**不直接包含**：
- 静帧的 `scene_tags` / `subjects` / `visible_actions`
- 多帧的 `action_summary`
- 原生视频的 `summary` / `events`

**因此 TSB-369 面临一个必须现在就回答的问题**：如果只拿到 ID，却声称"AI 导演理解了镜头内容"，那就是在编造。

---

## 1. 核心原则（不可协商）

| # | 原则 |
|---|---|
| C-1 | **没有证据就不描述**。`not_analyzed` 的镜头只能以「未知」出现，不能由 prompt 补全。 |
| C-2 | **渠道不可混淆**。静帧 ≠ 视频，多帧 ≠ 原生视频。描述必须携带来源渠道。 |
| C-3 | **置信度不是准确率**。模型自报 confidence 一律标 `uncalibrated`，不得参与任何加减分。 |
| C-4 | **新鲜度必须传递**。`freshness_unverified` / `stale` 的读数，下游要知道它可能过时。 |
| C-5 | **人工判断优先**。`exclude` 的镜头不得被任何下游逻辑重新纳入。 |
| C-6 | **digest 与内容一致**。同一 `input_digest` 必须对应同一份内容；内容变了 digest 必须变。 |

---

## 2. TSB-369 获取内容的两种方式（**必须二选一并记录**）

### 方案 A：按 `evidence_refs` 二次拉取（**本轮推荐**）

TSB-369 拿 `/director/context` 的 `candidate_summaries[].readings`，
其中有 `{channel, state, result_id}`，再按渠道调用已有只读端点取内容：

| 渠道 | 端点 | 取到的字段 |
|---|---|---|
| `static` | `GET /api/segments/{segment_id}/vision-result` | `visual.scene_tags/subjects/visible_actions/lighting/composition/mood`、`evidence.state` |
| `multiframe` | `GET /api/segments/{segment_id}/motion-review` | `motion_type`、`action_summary`、`stability_risk`、`human_verdict` |
| `video_native` | `GET /api/segments/{segment_id}/video-observation` | **`summary`（顶层）**、`events[]`、`audio_analyzed`、`color_unverified`、`observed_temporal_quality` |

**优点**：context 保持小巧；TSB-369 可只取需要的候选；隐私边界与现有端点一致。
**代价**：多轮请求；TSB-369 必须自己处理"取不到"。

**必须实现的行为**：

1. 只对 `state ∈ {current, freshness_unverified}` 的读数取内容；
   `stale` / `failed` / `not_analyzed` **不取**，直接记为「无可靠内容」。
2. `not_visible` / `uncertain` 的 `evidence.state` **原样传递**，不得改写成肯定描述。
3. 取不到时（404 / 端点异常）记为 `content_unavailable`，**不得跳过或补默认值**。

### 方案 B：在 context 内直接带内容（**未来可选**）

若 TSB-369 需要单次请求完成，可在 `/director/context` 的
`candidate_summaries[]` 中增加**有界、已校验**的内容块：

```json
"content": {
  "static": {"scene_tags": [...], "subjects": [...], "visible_actions": [...],
             "evidence_state": "observed"},
  "multiframe": {"motion_type": "pan", "action_summary": "...(<=300字)"},
  "video_native": {"summary": "...(<=300字)", "events": [...]}
}
```

**若采用方案 B，必须同时满足**：
- 每个字符串字段硬截断（`subjects`/`actions` ≤ 3 条、每条 ≤ 80 字；`summary` ≤ 300 字）；
- 只包含上述白名单键，**不得**放入原始模型响应、`request_usage`、路径；
- `input_digest` 必须覆盖这些内容（TSB-368-R1 已实现 `_content_fingerprint`）。

**方案 B 会让 context 变大**。在候选数 > 40 时须实测体积。

---

## 3. 输入字段清单（TSB-369 可依赖的稳定合同）

```jsonc
{
  "schema_version": "1.0",
  "project_id": "<uuid>",
  "input_digest": "<64 位 hex>",          // 变化检测器，非"素材未变"的证明
  "brief": {
    "theme": "<client 输入，不是视频事实>",
    "target_duration_sec": 120,
    "note": "client-supplied constraint text; not verified as video content"
  },
  "coverage": { /* 全项目口径，不随筛选变化 */ },
  "candidate_segment_ids": ["<uuid>", ...],   // 只含 eligibility == eligible
  "candidate_summaries": [
    {
      "segment_id": "<uuid>",
      "asset_id": "<uuid>",
      "source_window": {"start_sec": 0.0, "end_sec": 3.0},
      "eligibility": "eligible",
      "human": {"rating": "favorite", "story_role": "开场", "note": "..."},
      "editorial_priority": "preferred",
      "priority_reasons": ["human_rating_favorite"],
      "readings": [{"channel": "static", "state": "freshness_unverified",
                    "result_id": "<uuid>"}],
      "limitations": ["no_valid_model_reading"]
    }
  ],
  "important_human_labels": [...],
  "limitations": ["no_story_generated_this_round", "partial_analysis_coverage"],
  "needs_more_analysis": true,
  "uncovered_segment_count": 89,
  "withheld": {"needs_review_segments": 0, "excluded_segments": 0}
}
```

### 字段语义（易误解处）

| 字段 | 含义 | 常见误解 |
|---|---|---|
| `input_digest` | 输入**变化**的标识 | ❌ 不等于"源文件字节未变"。源不可读时读数仍是 `freshness_unverified` |
| `coverage` | **全项目**口径 | ❌ 不是当前页/当前筛选的口径 |
| `uncovered_segment_count` | 无任何有效读数的镜头数 | 这是**必须如实告知**的数字 |
| `withheld.*` | 被**故意排除**出候选的数量 | 让下游知道"没给"≠"没有" |
| `candidate_segment_ids` | 只含 `eligible` | `needs_review` 需人工确认后才能进 |
| `brief.theme` | 用户输入 | ❌ 不代表视频里真的出现了该内容 |

---

## 4. 缺失 / 过期 / 失败的处理

| 情形 | TSB-369 必须怎么做 |
|---|---|
| 三渠道全 `not_analyzed` | 只允许引用时间窗与人工标注；**禁止**生成画面描述 |
| `freshness_unverified` | 可以使用内容，但必须标注"该读数未经验证是否对应当前源文件" |
| `stale` | **不得**使用该内容作为当前镜头的事实 |
| `failed` | 视同未分析，且在报告中说明"曾尝试但失败" |
| `evidence_state = "uncertain"` | 保留 `uncertain`，不得转述为断言 |
| `evidence_state = "not_visible"` | 表示"看不到"，**不是**"没有" |
| 事件时间越界 | 已被 TSB-368 丢弃；不得从别处补回 |

---

## 5. 输入大小上限

| 项 | 上限 | 现状 |
|---|---|---|
| `candidate_summaries` 条数 | 由 `max_candidates` 控制（默认 40，最大 200） | 已实现 |
| 每条 `human.note` | 200 字（context 内截断） | 已实现 |
| 每条 `limitations` | 8 条 | 已实现 |
| `important_human_labels` | 20 条 | 已实现 |
| 方案 B 的内容块 | `subjects`/`actions` ≤ 3 条、`summary` ≤ 300 字 | 待 TSB-369 实施 |

---

## 6. 隐私边界

**context 与任何内容端点都不得包含**：

- 源文件绝对路径（`source_path`）
- API 令牌、模型 Key
- `request_usage`（token 计数）原文
- 原始模型响应全文（只投影白名单字段）
- EXIF / GPS
- 可识别到具体个人身份的信息（模型 schema 已禁止断言人名/地名）

TSB-368-R1 实测：真实库响应中 `source_path` / `token` / `request_usage` / Windows 路径
命中数均为 **0**。

---

## 7. 本轮已完成的支撑工作

| 项 | 状态 |
|---|---|
| `input_digest` 覆盖人工备注 | ✅ 已实现并测试 |
| `input_digest` 覆盖模型内容（含结果 ID 不变但内容变化） | ✅ 已实现 `_content_fingerprint` 并测试 |
| 扁平 `analysis_json` 可读（真实数据格式） | ✅ 已修复 |
| `needs_review` 不进自动候选 | ✅ 已实现并测试 |
| `withheld` 字段告知被排除数量 | ✅ 已实现 |
| 重复请求 digest 稳定 | ✅ 实测 |

---

## 8. 给 TSB-369 的直接建议

1. **先做方案 A**（按 `evidence_refs` 二次拉取）。它的信息损真最小，且 context 体积可控。
2. **不要**用 `brief.theme` 反向推断内容。用户输入"海边日落"，不代表画面里有海。
3. **必须**读 `limitations` 与 `uncovered_segment_count`，并在输出里体现
   "本方案基于 99 个镜头中的 10 个有观测证据的镜头"。
4. 若 TSB-369 的输出会让用户以为"AI 看过全部素材"，那是**验收失败**，不是文案问题。


---

## 9. 字段名更正（TSB-368-R2）

上一版本文档写作 `analysis.summary`，与实际响应不符。**按真实端点更正如下**
（以 `video_analysis.observation_payload()` 的返回为准，未改动 API）：

| 端点 | 真实字段 |
|---|---|
| `GET /segments/{id}/video-observation` | `id`（不是 `result_id`）、`segment_id`、`asset_id`、`summary`（**顶层**）、`events[]`、`is_current`、`audio_analyzed`、`color_unverified`、`observed_temporal_quality`、`clip{start_sec,end_sec,duration_sec,size_bytes,sha256}`、`segment_window{start_sec,end_sec}` |
| 导演 DTO 中对应的引用名 | `observations.video_native.result_id` ← 对应端点的 `id` |

`events[]` 的元素是生产序列化的原样结构：`event_type` / `start_abs_sec` /
`end_abs_sec` / `start_rel_sec` / `end_rel_sec` / `description` / `confidence` /
`confidence_is_calibrated` / `evidence` / `limitations`。

**注意**：`start_rel_sec` 是相对 clip 起点的时间，**不是**源视频绝对时间。
除 `start_abs_sec` / `end_abs_sec` 外不得用其他字段当作绝对时间。
