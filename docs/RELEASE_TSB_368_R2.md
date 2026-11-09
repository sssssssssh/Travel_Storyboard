# TSB-368-R2 发布记录：原生视频证据链端到端验收与策划输入合同收口

| 字段 | 内容 |
|---|---|
| 基线 | `15f4cefaf24884ca68592c79c8a2f83bcb9772fd` |
| 上一轮代码 | `9b7c8740125ca0aea52413e309c50da07880b2da`（TSB-368-R1） |
| **本轮代码** | `97cb5020520029acaa7e7d6495fda4509984c70e` |
| **CI** | run #124，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42597456345 |
| 证据目录 | [docs/validation/tsb368-r2/](validation/tsb368-r2/) |

---

## 一、状态汇总（六项分开陈述，禁止合并）

| 项 | 状态 |
|---|---|
| **P0-01 代码合同修复** | ✅ **PASS**（RED→GREEN→反向验证） |
| **P1-02 代码合同修复** | ✅ **PASS**（RED→GREEN→反向验证） |
| **生产写入路径合成端到端** | ✅ **PASS**（9 项，真实 `store_observation`） |
| **后端 / 前端 / CI 回归** | ✅ 见 §七 |
| **真实素材只读预检** | ✅ **完成**（未调用模型） |
| **真实模型（原生视频）验收** | 🔒 **BLOCKED**（未获授权，未上传任何素材） |

> 本轮**未上传任何视频或图片**，**未发起任何有费用的模型调用**。
> 任务书 §7.2 的审批尚未获得，因此真实模型验收不能写作 PASS。

---

## 二、P0-01：原生视频时序事件全部丢失

### 2.1 缺陷

生产序列化器 `TemporalEvent.to_dict()` 写出：

```python
{"event_type": …, "start_abs_sec": …, "end_abs_sec": …,
 "start_rel_sec": …, "end_rel_sec": …,
 "description": …, "confidence": …, "confidence_is_calibrated": False, …}
```

读取端 `_video_channel()` 查找的是 `start_sec`/`start`、`end_sec`/`end`、
`type`、`label`。

**每个真实事件都在第一个 `isinstance` 检查处被丢弃**，表现为
「视频有摘要、但事件表为空」。

### 2.2 为什么测试没发现（与 R1 同类的错误）

`test_tsb368_candidates.py::_video()` 手工构造的事件，恰好是**读取端想象的样子**：

```python
{"start_sec": …, "end_sec": …, "label": …, "confidence": …}
```

测试数据编码了读取方的假设，于是「能读自己造的格式」被误当成「读取正确」。
这与 R1 的扁平/嵌套 `analysis_json` 是同一类错误，只是换了字段。

### 2.3 修复（最小）

新增 `_normalize_video_events()`：

| 规则 | 实现 |
|---|---|
| 时间优先生产格式 | `start_abs_sec` / `end_abs_sec` |
| 旧格式兼容 | `start_sec`/`start`、`end_sec`/`end` |
| **拒绝相对时间当绝对时间** | 绝不使用 `start_rel_sec`/`end_rel_sec` |
| 文案 | `description` 优先，回退 `label`/`type` |
| 类型 | `event_type` 优先，回退 `type` |
| 两种格式冲突 | 保留生产值 + 记录 `event_time_shape_conflict` |

**为什么不拿 `start_rel_sec` 兜底**：它是相对 clip 起点的时间。clip 从 0 开始时
看不出差别，从 10 秒开始时每个事件错 10 秒——这正是最难发现的那类错误。

### 2.4 非法数据的可见性

| 输入 | 结果 |
|---|---|
| NaN / Inf | 丢弃，`event_time_non_finite` |
| bool / 字符串 | 丢弃，`event_time_not_numeric` |
| 缺时间 | 丢弃，`event_missing_time` |
| `start >= end` | 丢弃，`event_reversed_window` |
| 越界 | 丢弃，`event_outside_shot_window` |

返回结构：

```json
"event_count_total": 4,
"event_counters": {"accepted": 1, "dropped": 3, "skipped_non_object": 0},
"drop_reasons": ["event_outside_shot_window", "event_reversed_window"]
```

**原因同时写入 `limitations`**，使只读 `limitations` 的消费者也能知道
「事件存在但被过滤」，而不是以为模型没报事件（C6）。

### 2.5 RED → GREEN → 反向验证

| 阶段 | 结果 |
|---|---|
| RED | **13 项失败** |
| GREEN | 18/18 通过 |
| 反向验证（恢复旧解析器） | **13 项失败，集合一致** |

RED 失败集合：

```
test_production_event_shape_keeps_times_type_and_description
test_two_valid_production_events_are_both_accepted
test_relative_times_are_never_used_as_absolute
test_malformed_events_are_dropped_with_a_reason[bad0-non_finite]
test_malformed_events_are_dropped_with_a_reason[bad1-non_finite]
test_malformed_events_are_dropped_with_a_reason[bad2-not_numeric]
test_malformed_events_are_dropped_with_a_reason[bad3-not_numeric]
test_malformed_events_are_dropped_with_a_reason[bad4-missing_time]
test_malformed_events_are_dropped_with_a_reason[bad5-reversed]
test_malformed_events_are_dropped_with_a_reason[bad6-outside_shot]
test_malformed_events_are_dropped_with_a_reason[bad7-outside_shot]
test_a_dropped_event_is_reported_as_filtered_not_as_no_events
test_changing_a_video_event_description_changes_the_digest
```

---

## 三、P1-02：`input_digest` 漏掉 `important_human_labels`

### 3.1 缺陷

```python
chosen = usable[:max_candidates]                   # 只到 max_candidates
"important_human_labels": [...]                    # 来自 usable 全集
"input_digest": input_digest(pid, chosen, brief)   # 只算 chosen
```

`max_candidates` 小于库规模时，一个**不在候选摘要内**的镜头的人工备注改变，
返回内容变了但 digest 不变——下游缓存会复用建立在不同输入上的计划。

### 3.2 修复

**不再枚举「应该进 digest 的字段」**，改为对**构造完成的整个返回文档**做规范化
SHA-256（排除 `input_digest` 自身，附加 `_digest_version = "2"`）。

理由：手工枚举正是漏掉 `important_human_labels` 的原因。改为「digest 返回的文档」
后，将来给 context 加字段会**自动**进入 digest 范围，不需要有人记得同步修改。

### 3.3 RED → GREEN → 反向验证

| 阶段 | 结果 |
|---|---|
| RED | **2 项失败**（标签变化 digest 不变、字段覆盖不全） |
| GREEN | 10/10 通过 |
| 反向验证 | **2 项失败，集合一致** |

---

## 四、生产写入路径合成端到端

**关键约束**：`events_json` 没有一行是手写的，全部来自生产序列化器。

```
Mock 模型响应
  → validate_temporal_events()      真实校验器（补全 start_abs_sec）
  → TemporalEvent.to_dict()         真实序列化器
  → store_observation()             真实持久化（含 job 状态与来源校验）
  → SegmentVideoObservation        真实 SQLite
  → GET /video-observation → /director/candidates → /director/context
```

源文件用项目自带 FFmpeg 现场生成，因为 `validate_publication` 会重算
`source_identity(asset)`，源不存在则拒绝发布。

| 断言 | 结果 |
|---|---|
| DB 中存的是生产键名（`start_abs_sec`/`event_type`/`description`） | ✅ |
| 原生 API 返回同一 `id` 与事件 | ✅ |
| 候选 DTO 内容一致 | ✅ |
| context 引用可回读同一行同一内容 | ✅ |
| 无 usage / 路径泄漏 | ✅ |
| 再分析后恰好一行 current | ✅ |
| 来源变化拒绝发布（`SOURCE_CHANGED`） | ✅ |
| Job 非 running 返回 `JOB_INVALID` | ✅ |

详见 [SYNTHETIC_E2E.md](validation/tsb368-r2/SYNTHETIC_E2E.md)。

---

## 五、字段映射

完整表格见 [PRODUCER_CONSUMER_MAPPING.md](validation/tsb368-r2/PRODUCER_CONSUMER_MAPPING.md)。

核心差异：

| 项 | 生产端 | 读取端（修复后） |
|---|---|---|
| 起始时间 | `start_abs_sec` | 同名字段 |
| 结束时间 | `end_abs_sec` | 同名字段 |
| 类型 | `event_type` | 同名字段 |
| 描述 | `description` | 同名字段 |
| 相对时间 | `start_rel_sec` | **不投影** |

---

## 六、文档修正（未改 API）

| 文件 | 修正 |
|---|---|
| `CONTEXT_CONSUMPTION_CONTRACT.md` | `analysis.summary` → **顶层 `summary`** |
| 同上 | 补充真实端点字段：原生端点行 id 是 `id`，导演 DTO 叫 `result_id` |

---

## 七、测试与 CI

| 项 | 结果 |
|---|---|
| 新增测试 | **37 项**（18 + 10 + 9） |
| 后端全量 | **1869 collected / 0 failed / 0 errors / 6 skipped** |
| 前端四项 | typecheck / lint / **148 passed** / build 全部通过 |

6 项 skip 均为 `link creation is a platform fact`，已在白名单登记。

**对既有测试的改动**：`test_tsb368_candidates.py::test_events_outside_the_shot_window_are_dropped_and_reported`
新增 3 条断言（`drop_reasons`、`event_counters`、`event_count_total`），
**未删除或降低任何原有断言**。

---

## 八、真实素材与真实模型

### 8.1 只读预检（已完成）

| 项 | 值 |
|---|---|
| 项目 | `M5-real-acceptance` |
| Segment | 99（全部 candidate / pending） |
| 源文件 | 5/6 可访问 |
| 原生视频读数 | **0**（历史也是 0） |
| 人工标注 | **0** |

候选镜头已选出 6 个（脱敏），详见 [REAL_VIDEO_UAT.md](validation/tsb368-r2/REAL_VIDEO_UAT.md)。

### 8.2 真实模型验收：**BLOCKED**

`REAL_VIDEO_UAT = BLOCKED (waiting_for_external_upload_approval)`

未获授权的原因：
1. 需向您确认是否允许把派生 clip 发送至外部服务；
2. **费用无法在本轮确认**（私有部署端点，仓库配置中无单价）；
3. 素材可能包含可识别人物，需您判断。

审批单已备好，见 `REAL_VIDEO_UAT.md` §2。

### 8.3 隔离副本人工标注验证（已完成）

| 项 | 结果 |
|---|---|
| `favorite` 人工优先 | ✅ |
| `exclude` 不进 context | ✅ |
| `segment.exclude + annotation.pending` 不泄漏 | ✅ |
| 备注变化触发 digest | ✅ |
| 未污染真实库 | ✅（全部用独立项目行） |

---

## 九、范围声明

**修改**：
- `backend/app/services/director_candidates.py`（事件映射）
- `backend/app/services/director_query.py`（digest）
- `backend/tests/test_tsb368_candidates.py`（+3 断言，无删减）
- `docs/validation/tsb368-r1/CONTEXT_CONSUMPTION_CONTRACT.md`（字段名更正）

**新增**：
- `backend/tests/test_tsb368r2_video_event_contract.py`（18）
- `backend/tests/test_tsb368r2_director_digest_contract.py`（10）
- `backend/tests/test_tsb368r2_video_production_roundtrip.py`（9）
- 本轮文档

**未改动**：原片、人工标注、Storyboard、Alembic、既有 API 契约、
TSB-366/367 长路径与代理修复、上传许可与预算机制。

**未引入**：新表、向量检索、新评分算法、新依赖。

---

## 十、未完成 / 风险

1. **真实模型验收 BLOCKED**：原生视频渠道在真实数据上仍是 0 条。
   代码层面的映射已修，但「真实模型产出能被完整传递」尚未在真实数据上验证。
2. **真实库人工标注仍为 0**：`favorite`/`exclude` 的真实行为只在隔离副本验证过。
3. **一个源文件缺失**：该素材的读数永远是 `freshness_unverified`。
4. **`digest_version` 已从 1 升到 2**：若下游已有基于旧 digest 的缓存，需要失效。
   本仓库内无此类缓存，但外部消费者需注意。
5. **`event_time_shape_conflict` 场景未在真实数据中出现过**：混合格式目前只有合成测试覆盖。

---

## 十一、是否允许进入 TSB-369

**代码层面：可以。** 内容证据链在合成数据上端到端验证完整。

**建议：先完成真实模型验收。** 理由：

- TSB-369 依赖「真实模型产出能被完整、安全、可追溯地传递」；
- 该链条目前只在 Mock 数据上验证过；
- R1 和 R2 的两次教训都是「没在真实数据上跑过的路径，就不知道它是不是坏的」。
  原生视频是最后一条没被真实数据检验的路径。

若您决定跳过真实模型验收直接进入 TSB-369，请知悉：届时 TSB-369 的内容输入
将主要来自静帧（8 条有效）与多帧（4 条有效），原生视频渠道贡献为 0。


---

## 十二、CI 执行结果

| 项 | 值 |
|---|---|
| 代码 SHA | `97cb5020520029acaa7e7d6495fda4509984c70e` |
| run | **#124** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42597456345 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ |
| `Required FFmpeg (windows-latest)` | ✅ |
| `Required FFmpeg (ubuntu-latest)` | ✅ |
| `Backend (Python 3.11)` ×2 | ✅ |
| `Backend (Python 3.13)` ×2 | ✅ |

---

## 十三、关闭标准对照（任务书 §9）

### 代码合同 DoD

| 标准 | 状态 |
|---|---|
| HEAD / 工作区 / 远端一致性核对 | ✅ `15f4cef` → `97cb502` |
| 真实 `TemporalEvent.to_dict()` 用于 RED | ✅ |
| 生产格式 `_video_channel` 正确映射绝对时间与描述 | ✅ |
| 历史旧结构兼容，非法数据拒绝且限制可见 | ✅ |
| 生产写入→DB→API→候选→context 合成端到端绿 | ✅ 9/9 |
| `summary` 字段合同使用真实 API 顶层字段 | ✅ |
| `result_id`/`segment_id`/版本一致性经测试 | ✅ |
| `important_human_labels` 变化使 digest 变化 | ✅ |
| 实际视频 `events/description` 变化使 digest 变化 | ✅ |
| 既有排除、待复核、未知状态、不上传、只读合同无回归 | ✅ |
| N+1、长路径、代理及生命周期回归未破坏 | ✅ 1869 项全绿 |
| 后端/前端全量回归、主 CI 确认 | ✅ run #124 |
| 发布记录、证据、README 更新并推送、工作区干净 | ✅ |

**代码合同 DoD：可以关闭。**

### 真实素材 / 模型 DoD

| 标准 | 状态 |
|---|---|
| 真实素材预检完成，候选视频确实可访问 | ✅ |
| **外部上传、Provider、费用上限已由用户明确批准** | ❌ **未批准** |
| 批准范围内 2～3 段原生视频调用成功 | ⛔ BLOCKED |
| DB、API、浏览器同一镜头摘要与时间事件一致 | ⛔ BLOCKED |
| 人工观看对照至少 1 段 | ⛔ BLOCKED |
| 原片不变、费用与次数在审批上限内、隐私安全无泄漏 | ✅（未调用，故无费用与泄漏） |
| 真实结果失败时如实记录，不将 synthetic PASS 写成真实 PASS | ✅ |

**真实素材 / 模型 DoD：BLOCKED，不能关闭。**

> **不得把「代码合同通过」等同于「发布可以上线」。**
> G5/G6 的真实端点、性能与模型能力门禁仍是独立事项。
