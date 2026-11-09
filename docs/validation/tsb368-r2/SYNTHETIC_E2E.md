# TSB-368-R2 合成端到端报告

本报告记录「模型输出 → 生产保存 → DB → 原生 API → 候选 API → context」全链路验证。

**全部使用 Mock Provider 与合成素材，无任何外部请求。**

---

## 1. 测试链路

```
Mock 模型响应（clip-relative 事件）
  ↓ video_schema.validate_temporal_events()   真实校验器，补全 start_abs_sec
  ↓ TemporalEvent.to_dict()                    真实序列化器
  ↓ video_analysis.store_observation()         真实持久化（含 job 状态与来源校验）
  ↓ SegmentVideoObservation.events_json        真实 SQLite 落库
  ↓ GET /api/segments/{id}/video-observation
  ↓ GET /api/projects/{id}/director/candidates
  ↓ GET /api/projects/{id}/director/context
```

**为什么必须走这条路**：R1 的教训是「测试数据编码了读取方的想象」。
本轮的 `events_json` **没有一行是手写的**——全部来自生产序列化器。

---

## 2. 合成素材

| 项 | 值 |
|---|---|
| 源文件 | 项目自带 FFmpeg 现场生成 `testsrc` 6 秒 320×180 H.264 |
| 为什么需要真实文件 | `validate_publication` 会重算 `source_identity(asset)`，源不存在则拒绝发布 |
| Provider | `mock`（`store_observation` 的 `provider_name="mock"`、`model_id="mock-video-1"`） |
| 网络 | **零外部请求** |

---

## 3. 关键断言（逐跳）

| 跳 | 断言 | 文件 |
|---|---|---|
| 序列化 | `events_json[0]["start_abs_sec"] == 1.0`、`event_type == "subject_action"`、`confidence_is_calibrated is False` | `test_store_observation_persists_production_event_shape` |
| 原生 API | `body["id"] == row_id`；`events[0]["event_type"] == "subject_action"`；`start_abs_sec ≈ 1.0` | `test_native_video_api_returns_the_stored_events` |
| 候选 API | `status_detail.events` 长度 2；类型保留；`label` 含原描述 | `test_director_candidates_surface_the_same_events` |
| context | `readings[video_native].result_id == row_id`；回读端点得同一 `id` 与同一描述 | `test_context_references_the_same_result_and_content` |
| 隐私 | 响应不含 `total_tokens` / `request_usage` / 真实路径 / `source_path` | `test_no_raw_model_payload_or_path_leaks` |

**同一个事件的 `event_type` / `description` / 起止时间从模型输出到最终候选内容，
逐跳比对未丢失、未串号、未漂移。**

---

## 4. 非零起点（关键）

合成素材的镜头窗口为 `0–6s`，事件相对时间为 `1.0–2.5` 与 `3.0–4.5`，
因此 `start_abs_sec = start_rel_sec + clip_start(0)`——**两者恰好相等，无法区分**。

**专门用 `10.0–15.0s` 的镜头做对比测试**（`test_relative_times_are_never_used_as_absolute`）：

| 字段 | 值 |
|---|---|
| `start_rel_sec` | 0.5 |
| `start_abs_sec` | **10.5** |
| 断言 | 读取端拿到的必须是 **10.5**，且 **≠ 0.5** |

该测试在修复前失败——修复前所有事件被丢弃，`events` 为空。

---

## 5. 旧格式与混存

| 场景 | 测试 | 结果 |
|---|---|---|
| 旧 `{start_sec,end_sec,label}` | `test_legacy_event_shape_is_still_accepted` | ✅ |
| 旧 `{start,end,type}` | 同上（第二条事件） | ✅ |
| 两种格式时间冲突 | 保留生产值 + `event_time_shape_conflict` | ✅ |

---

## 6. 非法数据

| 输入 | 结果 |
|---|---|
| NaN / Inf | 丢弃，`event_time_non_finite` |
| `True` / `"10.5"` | 丢弃，`event_time_not_numeric` |
| 缺时间字段 | 丢弃，`event_missing_time` |
| `start >= end` | 丢弃，`event_reversed_window` |
| 越出镜头窗口 | 丢弃，`event_outside_shot_window` |
| 非 dict 条目 | 计入 `skipped_non_object` |

**全部不抛异常、不编造数据，且计数与原因可见。**

---

## 7. 再分析与当前性

| 场景 | 测试 | 结果 |
|---|---|---|
| 第二次运行 | `test_a_second_run_supersedes_the_first...` | 旧行 `is_current=False`，恰好一行 current |
| 候选读取 | `test_the_candidate_reads_the_current_row_after_a_rerun` | 指向新行 |
| 来源变化 | `test_a_source_change_blocks_publication_visibly` | `ModelError`（`SOURCE_CHANGED`/`JOB_INVALID`） |
| Job 非 running | `test_publication_is_refused_when_the_job_is_not_running` | `ModelError("JOB_INVALID")` |
| 窗口变化 | `test_a_window_mismatch_still_yields_stale` | `stale` |
| 无记录 | `test_no_video_row_means_not_analyzed...` | `not_analyzed`，payload 为空 |

---

## 8. digest 一致性

| 场景 | 结果 |
|---|---|
| 视频事件描述改变（result_id 与 version 均不变） | digest **变** |
| `important_human_labels` 中的备注改变（该镜头不在候选摘要内） | digest **变** |
| 同输入重复调用 / 逆序读取 | digest **恒等** |
| 无关项目变动 | digest **不变** |
| 仅有已过期读数变动 | digest **不变** |

---

## 9. 统计

| 项 | 数量 |
|---|---|
| 视频事件合同测试 | 18 |
| digest 合同测试 | 10 |
| 生产往返测试 | 9 |
| 合计新增 | **37** |

返回码：全部 PASS，无 skip、无 xfail。
