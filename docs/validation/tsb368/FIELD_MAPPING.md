# TSB-368 字段映射表：源表 → 导演候选 DTO

基线 `bddadd8`。本表**逐项对照真实代码**核对，不是依据任务书示例 JSON 猜测。
标记含义：**已验证** = 已从模型定义/写入点确认；**推导** = 由多个已存在字段计算；
**不可用** = 数据库中没有该信息，不能编造。

---

## 1. 身份与时间窗

| DTO 字段 | 源 | 状态 | 说明 |
|---|---|---|---|
| `segment_id` | `Segment.id` | 已验证 | 服务端生成 UUID |
| `asset_id` | `Segment.asset_id` | 已验证 | 与 `Asset.id` 关联 |
| `source_window.start_sec/end_sec` | `Segment.start_sec` / `end_sec` | 已验证 | **当前可编辑区间**（C2） |
| `detected_window.start_sec/end_sec` | `Segment.detected_start_sec` / `detected_end_sec` | 已验证 | **独立检测边界**，人工裁剪不改它 |
| `asset.display_name` | `Asset.display_name` | 已验证 | 不含路径 |
| `asset.duration_sec` | `Asset.duration_sec` | 已验证 | |
| `timeline_key` | `(asset_id, start_sec, segment_id)` | 推导 | 稳定 tie-break |

**约束**：`start_sec < end_sec` 且均为有限数；不满足者不得进入候选，按拒绝原因输出。

---

## 2. 人工判断（最高优先级）

| DTO 字段 | 源 | 状态 | 说明 |
|---|---|---|---|
| `human.rating` | `Segment.user_rating` | 已验证 | 取值集见下 |
| `human.note` | `UserAnnotation.note` | 已验证 | 无记录则空串 |
| `human.rating`（注解覆盖） | `UserAnnotation.rating` | 已验证 | 存在注解行时以注解为准 |
| `human.story_role` | `Segment.story_role` | 已验证 | 原样保留 |

**真实评分枚举**（`backend/app/api/segments.py:L25`，已验证）：
```python
RATING_VALUES = {"like", "favorite", "exclude", "pending"}
```
默认值 `pending`（`Segment.user_rating` column default）。

**优先级规则**（确定性、可观测）：
1. `UserAnnotation` 存在且 `rating` 非空非 `pending` → 用注解值；
2. 否则用 `Segment.user_rating`；
3. 两者都缺失 → `pending`。
人工值**永不**被 AI 结果覆盖。

---

## 3. 技术指标

| DTO 字段 | 源 | 状态 | 说明 |
|---|---|---|---|
| `technical.quality_metrics` | `Segment.quality_metrics` | 已验证 | 白名单键，见下 |
| `technical.duration_sec` | `end_sec - start_sec` | 推导 | |
| `technical.is_trimmed` | `Segment.is_trimmed` | 已验证 | |

**`quality_metrics` 真实键**（`analyzer.py:L380`，已验证）：
`blur_avg` · `blur_min` · `brightness_avg` · `overexposed_ratio` · `underexposed_ratio` ·
`frame_count` · `near_duplicate_segments`

**投影规则**：只输出上述白名单键；缺失键**不出现**（不是 0），避免编造"质量 0 分"。

**`content_tags`**：自由 `dict`，**不做内容推断**，原样透传但标注为人工/规则来源。

---

## 4. 三渠道观测

### 4.1 静帧语义 `observations.static`

| DTO 字段 | 源 | 状态 |
|---|---|---|
| `result_id` | `SegmentSemanticResult.id` | 已验证 |
| `state` | `is_current` + 签名核验 | 推导 |
| `status` | `SegmentSemanticResult.status` | 已验证 |
| `visual` | `analysis_json` 白名单投影 | 已验证 |
| `evidence_state` | `evidence_json.state` | 已验证 |
| `model_provider` / `model_id` / `prompt_version` | 同行 | 已验证 |
| `source_signature` / `frame_manifest_hash` | 同行 | 已验证 |

**新鲜度**（§2.3 要求）：
- `source_signature` 依赖 `verdicts.source_identity(asset)`，后者需**有界采样**读取源文件
  （`indexer.quick_fingerprint`：3 个采样点 × 64KB = 192KB，**非全盘扫描**）；
- 本次采用**分级校验**（用户已确认）：
  - 源文件可读 → 重算 `source_signature` 并精确比对 → `current` / `stale`；
  - 源文件不可读（离线/移动）→ `freshness_unverified`，**不猜**。

### 4.2 多帧运动 `observations.multiframe`

| DTO 字段 | 源 | 状态 |
|---|---|---|
| `result_id` | `SegmentMotionReview.id` | 已验证 |
| `motion_type` | 同行 | 已验证 |
| `stability_risk` | 同行 | 已验证 |
| `action_summary` | 同行 | 已验证 |
| `input_signature` | 同行 | 已验证 |
| `human_verdict` | 同行 / `SegmentMotionVerdict` | 已验证 |
| `start_sec` / `end_sec` | 同行 | 已验证 |

**新鲜度**：用 `input_signature` + 区间与当前 `Segment` 比对。
区间不一致 ⇒ `stale`。**不等于**"完整视频已看"。

### 4.3 原生视频 `observations.video_native`

| DTO 字段 | 源 | 状态 |
|---|---|---|
| `result_id` | `SegmentVideoObservation.id` | 已验证 |
| `summary` | `analysis_json` 投影 | 已验证 |
| `events` | `events_json` | 已验证 |
| `source_signature` / `version_key` | 同行 | 已验证 |
| `clip_start_sec` / `clip_end_sec` | 同行 | 已验证 |
| `segment_start_sec` / `segment_end_sec` | 同行 | 已验证 |
| `audio_analyzed` / `color_unverified` / `observed_temporal_quality` | 同行 | 已验证 |

**新鲜度（零成本精确核验）**：
- `segment_start_sec/end_sec` ≠ 当前 `Segment.start_sec/end_sec` ⇒ **`stale`**（区间证据）；
- 区间一致但 `source_signature` 无法在无上传前提下重算 ⇒ `freshness_unverified`；
- `status != 'succeeded'` ⇒ 不作为有效证据。

**events 时间窗校验**：每个事件的绝对时间必须落在当前 Segment 区间内，越界者丢弃并计入限制。

---

## 5. 覆盖与资格

| DTO 字段 | 源 | 状态 |
|---|---|---|
| `eligibility` | 由 `Segment.status` + `user_rating` + 区间合法性推导 | 推导 |
| `exclusion_reasons` | 同上 | 推导 |
| `library_segments` | `COUNT(Segment WHERE project_id=?)` | 已验证 |
| `static_valid` / `multiframe_valid` / `video_native_valid` | 各渠道**当前有效**记录数 | 推导 |
| `no_valid_model_reading` | 三渠道均无有效记录 | 推导 |

**资格合同与现有业务对齐**（`review_validation.INVALID_SEGMENT_STATUSES`，已验证）：
```python
INVALID_SEGMENT_STATUSES = ('needs_review', 'archived', 'invalid')
```
- `status in INVALID_SEGMENT_STATUSES` → `needs_review`（**只读展示，不自动推荐**）；
- `user_rating == 'exclude'` → `excluded`；
- 区间非法 → `excluded`；
- 其余 → `eligible`。

**无 AI 分析 ≠ excluded**：合法镜头即使 `not_analyzed` 也是 `eligible`。

---

## 6. 不可用字段（禁止编造）

| 任务书示例中出现 | 真实情况 |
|---|---|
| `editorial_hints.priority` 的数值分数 | **数据库无此字段**，只输出离散 `unrated/noted/preferred` + 理由列表 |
| 校准后的置信概率 | `analysis_json` 里的 confidence 是**模型自报**，必须标 `uncalibrated` |
| 地点名 / 人物身份 | 模型 schema 明确禁止断言，DTO 不提供该字段 |
| `observations.*.state = "observed"` 的统一布尔 | 三渠道状态分开表达，不合并 |

---

## 7. 安全边界

- 所有关联强制 `Asset.project_id == Segment.project_id == path project_id`；
- 模型结果行还需 `result.project_id == segment.project_id` 且 `result.asset_id == segment.asset_id`；
- 任何一项不一致 ⇒ **整条候选不出现**（不是打标后仍输出）；
- DTO 为**固定字段白名单**，不序列化 ORM `__dict__`；
- 不含 `source_path`、令牌、`request_usage` 原文、EXIF/GPS。
