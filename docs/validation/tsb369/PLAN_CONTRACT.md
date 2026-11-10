# TSB-369 方案 Schema 与证据合同

基线 `a6a79076a26a46e698abf182fec8444d2b17fff4`。

本轮把"AI 导演"拆成三层可分别验证的东西：**证据输入 → 编排建议 → 严格校验**。
本文说明每层保证什么、拒绝什么。

---

## 1. 为什么要先定合同再谈"生成"

任务书 §0 的五项决策归结为一句话：**证据先于叙事**。

一个能生成漂亮文案的系统很容易写出"清晨的薄雾笼罩着山谷"——
但如果模型从没看过那段视频，这句话就是编造。所以本轮的设计顺序是：

1. 先定义**什么算证据**（三渠道 + 状态 + 引用）；
2. 再定义**草案能声称什么**（claim kind + 必须带证据引用）；
3. 最后才是**谁来编排**（离线规则 / 未来模型）。

---

## 2. 输入层：`director_plan_input`

### 2.1 唯一来源

输入**只能**来自 `/director/context` 已经筛过的候选（`eligibility == eligible`）。
禁入的镜头：

| 情形 | 结果 |
|---|---|
| 人工 `exclude` | 不进入输入 |
| `segment.status ∈ {needs_review, archived, invalid}` | 不进入输入 |
| `exclude` + 只有 `pending` 注解 | **不进入**（R1 的 P0-01 陷阱） |
| 其他项目的镜头 | 不进入 |

### 2.2 三渠道结构

```python
StaticEvidence(state, result_id, evidence_state, visual, model, limitations)
MotionEvidence(state, result_id, motion_type, action_summary, stability_risk, ...)
VideoEvidence(state, result_id, summary, events[VideoEvent], audio_analyzed, ...)
```

- `state ∈ {current, freshness_unverified, stale, failed, not_analyzed}`
- **`not_analyzed` 是合法答案**，不是异常
- `stale` / `failed` 的读数**不可被引用**作为证据

### 2.3 时间单位

`VideoEvent.start_sec` / `end_sec` 一律是**源视频绝对秒**。

生产端 `TemporalEvent` 同时有 `start_rel_sec`（相对 clip 起点）与 `start_abs_sec`。
R2 已确立：**只用绝对时间**。输入层复用同一个归一化器，不写第三套解析。

### 2.4 不确定性原样传递

`evidence_state = uncertain` / `not_visible` 到达规划层时保持原值。
规划器**不被告知"这里可以放心说"**，而是被告知真相。

### 2.5 零副作用

`build_planning_input()` 不调用 `get_current_version()`（那会自动创建空 v1）。
改用 `_storyboard_state()` 纯读探测，返回 `(version_id | None, absent)`。

### 2.6 成本

批量加载：segments / assets / annotations / 三渠道 / verdicts，各一次查询。
1000 镜头实测语句数恒定（测试 `test_one_thousand_segments_stay_bounded`）。

---

## 3. 草案层：`director_plan_schema`

### 3.1 封闭结构

所有 Pydantic 模型 `extra="forbid"`。规划器**不能塞入额外字段**。

### 3.2 `Claim` 是核心

```python
class Claim:
    text: str                    # ≤300 字
    kind: ClaimKind              # observed_signal | human_supplied |
                                 # editorial_suggestion | unknown
    evidence_refs: list[EvidenceRef]
```

| kind | 含义 | 是否需要证据 |
|---|---|---|
| `observed_signal` | 关于画面的观察 | **必须**带引用 |
| `human_supplied` | 来自用户备注/角色 | 需要镜头确实有人工输入 |
| `editorial_suggestion` | 剪辑手法建议 | 不需要（不声称画面内容） |
| `unknown` | 明确表示不知道 | 不需要 |

**关键约束**：`observed_signal` 若无 `evidence_refs` → 校验失败 `UNSUPPORTED_VISUAL_CLAIM`。

### 3.3 `EvidenceRef`

```python
channel: static | multiframe | video_native
result_id: str
segment_id: str
event_start_sec / event_end_sec   # 仅原生视频事件，源片绝对秒
```

---

## 4. 校验层：`director_plan_validation`

**这是最重要的安全边界。**离线规则也是启发式；未来模型更不可控。

### 4.1 五组规则

| 组 | 规则 | 错误码 |
|---|---|---|
| **身份** | 引用的镜头必须在候选集内、属于本项目 | `UNKNOWN_SEGMENT` |
| | asset 必须拥有该 segment | `ASSET_MISMATCH` |
| **窗口** | `in < out`，有限值 | `INVALID_SHOT_WINDOW` |
| | 必须落在当前可编辑区间内 | `INVALID_SHOT_WINDOW` |
| | `planned_duration == out - in` | `INVALID_SHOT_DURATION` |
| **时间轴** | 首帧从 0 开始，逐段连续 | `INVALID_TIMELINE` |
| | `total == Σduration`，`gap == target - total` | `INVALID_TIMELINE` |
| **断言** | `observed_signal` 必须带可用证据 | `UNSUPPORTED_VISUAL_CLAIM` |
| | **不得把 `uncertain`/`not_visible` 说成观察** | `SEMANTIC_UPGRADE` |
| | 事件时间必须在镜头窗口内 | `INVALID_EVENT_TIME` |
| **形状** | section 必须已声明 | `ORPHAN_SECTION` |
| | 同一 segment 不得出现两次 | `DUPLICATE_SEGMENT` |
| | 三个方案不能只差标题 | `VARIANTS_NOT_DISTINCT` |
| **一致性** | 草案 digest 必须等于当前输入 digest | `DIRECTOR_INPUT_CHANGED` |
| | 项目必须一致 | `PROJECT_MISMATCH` |

### 4.2 为什么 `SEMANTIC_UPGRADE` 单独成码

静帧模型可以说"证据不足，画面中看不清"。如果规划器拿到这个读数后写成
"镜头里有湖"，那是**把不确定性升级成事实**——不是格式错误，是语义造假。
单独立码是为了让它可被统计、可被单独拒绝。

### 4.3 全量返回

校验器返回**所有**问题，不是第一个。用户看到被拒的草案时，
应该知道所有问题，而不是改一个再撞下一个。

---

## 5. 编排层：`director_plan_rules`

### 5.1 三种策略，真实不同

| 策略 | 排序依据 |
|---|---|
| `chronological` | `(asset_id, start_sec, segment_id)` |
| `highlights_first` | 人工收藏 → 人工角色 → 有读数 → 时间线 |
| `atmospheric` | 有读数 → 有 mood 标签 → 无动作 → 时间线 |

若素材太小导致两种策略产出**相同序列**，该方案被**丢弃**而不是只改标题。

### 5.2 时长分配

按策略顺序取镜头，每段取自身可用区间，直到填满目标时长。
**不重复使用**同一镜头来凑时长。填不满就如实报 `duration_gap_sec`。

### 5.3 角色分配

首段 `opening`、末段 `closing`、中间按数量分布 `development/turning_point/climax`。
这是**结构建议**，不是"这就是故事高潮"。

### 5.4 状态诚实

| `generation_status` | 条件 |
|---|---|
| `insufficient_evidence` | 无合格镜头 → **返回空方案，不编造** |
| `provisional` | 有读数镜头 < 3 |
| `complete` | 有读数镜头 ≥ 3 |

注意：`complete` 只表示**结构完整**，不表示模型看过全部素材。

---

## 6. 生成器接口：`director_plan_providers`

```python
class PlannerProvider(Protocol):
    name: str
    def propose(self, input, brief) -> list[VariantDraft]: ...
```

| 实现 | 用途 | 可达性 |
|---|---|---|
| `OfflineRulesPlanner` | **生产默认** | HTTP `offline_rules` |
| `MockPlannerProvider` | 测试注入恶意/错误草案 | **HTTP 不可达** |
| `ExternalPlannerProvider` | 门禁桩 | 抛 `MODEL_CALL_NOT_AUTHORIZED`，**零网络** |

`provider_for("mock")` 直接抛 `ValueError`；HTTP 传 `planning_mode: "mock"` 返回 422。

---

## 7. API：`POST /director/plans/preview`

| 状态码 | 含义 |
|---|---|
| 200 | 草案（可能是 `insufficient_evidence`） |
| 404 | 项目不存在 |
| 409 | `DIRECTOR_INPUT_CHANGED` —— 附当前 digest |
| 422 | `PLAN_REJECTED` —— 附全部问题；或请求参数非法 |
| 403 | `MODEL_CALL_NOT_AUTHORIZED` |

**该端点不写任何表**：不建 Storyboard、不建版本、不建 Job、不建 PlanOperation、不改人工数据。

---

## 8. 页面

`/project/:id/director/plan`

- 生成前即显示真实覆盖（分子 + 分母 + 未分析数）
- 每个镜头的每条断言带 **[观察]/[人工]/[建议]** 标签与证据渠道
- 无读数镜头显式显示「没有任何模型读数」+ 「待复核」
- 顺序调整与裁剪**仅在内存**，时间轴本地重算
- 导出 JSON / Markdown，内容自述「只读草案，未写入任何正式剪辑计划」
- **没有**应用/保存/提交按钮

---

## 9. 本轮**没有**做的事

| 项 | 原因 |
|---|---|
| 真实模型策划 | 未授权；代码中为零网络拒绝 |
| 把草案保存为正式 Storyboard | 任务书禁止；留待独立审批 |
| 音乐推荐 / 调色执行 / 自动渲染 | 任务书明确排除 |
| 向量检索 / 新综合评分 | 任务书明确排除 |
| 全库重新分析 | 任务书明确排除 |
