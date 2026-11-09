# TSB-368-R2 生产者 → 消费者字段映射

基线 `15f4cefaf24884ca68592c79c8a2f83bcb9772fd`。
本表**逐项对照真实代码**核对，不是依据任务书示例猜测。

---

## 1. 原生视频时序事件

| 来源 | 真正生成字段 | DB 字段 | 原生 API 字段 | 导演 DTO 字段 | 兼容策略 |
|---|---|---|---|---|---|
| video event | `start_abs_sec` / `end_abs_sec` | `events_json[].start_abs_sec` | `events[].start_abs_sec` | `status_detail.events[].start_sec` / `end_sec` | **生产优先**；缺失时回退旧格式 |
| video event | `event_type` | `events_json[].event_type` | `events[].event_type` | `status_detail.events[].event_type` | 保留原值 |
| video event | `description` | `events_json[].description` | `events[].description` | `status_detail.events[].label` | 优先 `description`，回退 `label`/`type` |
| video event | `start_rel_sec` / `end_rel_sec` | 同行存储 | 同名字段 | **不投影**（会误导） | 仅作兼容判定，绝不当绝对时间 |
| video event | `confidence` / `confidence_is_calibrated=false` | 同行存储 | 同名 | `confidence` + `confidence_calibration="uncalibrated"` | 原样传递并标注 |
| video event | `evidence[]` / `limitations[]` | 同行存储 | 同名 | `status_detail.events[].evidence/limitations` | 有界截断（≤6 项，≤64 字） |
| video summary | `ValidatedVideoObservation.summary` | `analysis_json.summary` | **`summary`（顶层）** | `status_detail.summary` | 按真实端点对齐（≤600 字） |
| video honesty | `audio_analyzed` / `color_unverified` / `observed_temporal_quality` | 同名列 | 同名字段 | `status_detail.honesty.*` | 原样传递 |

### 1.1 生产写入路径（已验证）

```
model response
  → video_schema.validate_temporal_events()      # 校验并补全 start_abs_sec
  → TemporalEvent.to_dict()                       # 生产序列化器
  → video_analysis.store_observation()            # 生产持久化（含 job/来源校验）
  → SegmentVideoObservation.events_json
```

`TemporalEvent.to_dict()` 的实际输出（`video_schema.py:L103`）：

```python
{
    "event_type": ..., "start_rel_sec": ..., "end_rel_sec": ...,
    "start_abs_sec": ..., "end_abs_sec": ...,
    "description": ..., "confidence": ...,
    "confidence_is_calibrated": False,
    "evidence": [...], "limitations": [...],
}
```

### 1.2 读取端（修复前后）

| 项 | 修复前 | 修复后 |
|---|---|---|
| 时间字段 | `start_sec` / `start` | `start_abs_sec` 优先，回退 `start_sec`/`start` |
| 文案字段 | `label` / `type` | `description` 优先，回退 `label`/`type` |
| 失败计数 | 仅一个 `dropped` 整数 | `event_counters{accepted,dropped,skipped_non_object}` + `drop_reasons[]` |

**修复前后果**：生产事件因找不到 `start_sec` 全部被丢弃，`events` 恒为空，
但 `summary` 仍在——表现为「模型有摘要但没有任何事件」。

---

## 2. 静帧语义（R1 已修，本轮回归确认）

| 来源 | 真正生成字段 | DB 字段 | 原生 API 字段 | 导演 DTO 字段 | 兼容策略 |
|---|---|---|---|---|---|
| static | `validated.visual`（**扁平**） | `analysis_json` 顶层 | `visual` | `status_detail.visual` | 扁平优先，兼容嵌套 `{"visual":{...}}` |

生产写入点：`semantic._store_result` → `analysis_json=validated.visual`。

全库实际分布（R1 实测）：嵌套 **772** 条、扁平 **14** 条。

---

## 3. 多帧运动

| 来源 | 真正生成字段 | DB 字段 | 原生 API 字段 | 导演 DTO 字段 |
|---|---|---|---|---|
| multiframe | `motion_type` | 列 | 同名 | `status_detail.motion_type` |
| multiframe | `action_summary` | 列 | 同名 | `status_detail.action_summary` |
| multiframe | `stability_risk` | 列 | 同名 | `status_detail.stability_risk` |
| multiframe | `input_signature` | 列 | 同名 | `source_version` |
| multiframe | 人工裁决 | `human_verdict` / `SegmentMotionVerdict` | 同名 | `status_detail.human_verdict` |

---

## 4. 三种「当前性」判定

| 渠道 | 判定方式 | 能证明什么 |
|---|---|---|
| static | 重算 `source_signature`（有界采样 192KB）比对 | `current` / `stale` / `freshness_unverified` |
| multiframe | 比对存储窗口 vs 当前 Segment 窗口（零成本） | 窗口不符 → `stale` |
| video_native | 比对 `segment_start_sec`/`segment_end_sec`（零成本） | 窗口不符 → `stale` |

**`is_current=True` 不等于「对当前素材有效」**：三种渠道都额外做了内容核验。

视频的 `source_signature` 包含 `clip_sha256`，重算需重新剪辑——因此窗口一致时
仍标 `freshness_unverified`，不升级为 `current`。

---

## 5. 事件计数与可见性

| 字段 | 含义 |
|---|---|
| `event_count_total` | 存储中原始事件条数（含非法） |
| `event_counters.accepted` | 通过校验并投影的条数 |
| `event_counters.dropped` | 被拒绝的条数 |
| `event_counters.skipped_non_object` | 非 dict 条目数 |
| `drop_reasons[]` | 去重后的拒绝原因 |

**拒绝原因取值**：

| 原因 | 含义 |
|---|---|
| `event_missing_time` | 完全没有时间字段 |
| `event_time_not_numeric` | 时间字段是 bool / 字符串 |
| `event_time_non_finite` | NaN / Inf |
| `event_reversed_window` | `start >= end` |
| `event_outside_shot_window` | 越出当前 Segment 窗口 |
| `event_time_shape_conflict` | 两种格式同时存在且不一致（保留生产值并记录） |

**这些原因同时写入 `limitations`**，使只读 `limitations` 的消费者也能知道
「事件存在但被过滤」，而不是误以为模型没报事件（C6）。

---

## 6. 端点字段名对照（R2 修正）

R1 文档写作 `analysis.summary`，与真实响应不符。真实字段：

| 端点 | 真实响应字段 |
|---|---|
| `GET /segments/{id}/video-observation` | **`id`**（不是 `result_id`）、`segment_id`、`asset_id`、`status`、`method`、`is_current`、`model_id`、`prompt_version`、`schema_version`、`protocol`、**`summary`（顶层）**、`events[]`、`limitations[]`、`dropped[]`、`clip{start_sec,end_sec,duration_sec,size_bytes,sha256}`、`segment_window{start_sec,end_sec}`、`audio_analyzed`、`color_unverified`、`observed_temporal_quality` |
| `GET /segments/{id}/vision-result` | `visual{...}`、`evidence{state,frame_ids}`、`id` |
| `GET /segments/{id}/motion-review` | `motion_type`、`action_summary`、`stability_risk`、`id` |
| 导演 DTO 引用名 | `observations.<channel>.result_id` ← 对应上述端点的 `id` |

**未改动任何既有 API**：只修正文档以匹配真实响应。
