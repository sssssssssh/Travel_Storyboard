# TSB-368 覆盖口径与排序规则

基线 `bddadd8`。本文说明**为什么**覆盖率、资格与排序是这样算的——这些口径直接决定
用户看到的是"素材库真实状态"还是"一个好看的假象"。

---

## 1. 候选全域：**整个项目的 Segment**

候选集合来自 `SELECT Segment WHERE project_id = ?`，**不是**以下任何子集：

| 错误来源 | 为什么不采用 |
|---|---|
| `GET /vision-candidates` | 那是**运动复核队列**，语义完全不同 |
| `semantic.select_candidates` | 那是**送模型的队列**，要求必须有 Frame |
| `video_job.select_candidates` | 那是**上传预算筛选**，为发送视频服务 |

**关键后果**：一个没有代表帧、没有模型跑过、用户也没评分的镜头，**仍然是一个候选**，
仍出现在 coverage 的分母里。它只是被标记为 `not_analyzed`。

> 这是任务书 §2.1 与 C6 的硬性要求：让缺口可见，而不是让不完美的东西消失。

---

## 2. 资格（eligibility）三分

| 值 | 条件 | 自动策划可用？ |
|---|---|---|
| `eligible` | 默认 | 是 |
| `needs_review` | `Segment.status ∈ INVALID_SEGMENT_STATUSES` | **否**（只读展示） |
| `excluded` | 人工 `exclude` 或区间非法 | **否** |

**与现有业务合同对齐**（`review_validation.INVALID_SEGMENT_STATUSES`，已验证）：

```python
INVALID_SEGMENT_STATUSES = ('needs_review', 'archived', 'invalid')
```

代码**没有**自定义一套冲突语义，直接复用这一元组。

**`needs_review` 与 `excluded` 的区别**：
- `needs_review`：来源需人工确认，但**不是**用户拒绝 → 仍计入 `needs_review_segments`
- `excluded`：硬性拒绝（人工排除 / 区间非法）→ 计入 `excluded_segments`

**人工排除优先于一切**：即使该镜头有完美的 AI 读数，`exclude` 仍然生效。

**无 AI 分析 ≠ 排除**：合法但未分析的镜头是 `eligible`，不是 `excluded`。

---

## 3. 三渠道状态（互不替代）

每个镜头同时保留三个独立状态，**不合并为一个 "analyzed" 布尔**：

| 状态 | 含义 |
|---|---|
| `current` | 有读数，且签名核验通过（仅静帧在源文件可读时可达到） |
| `freshness_unverified` | 有读数，但无法在**无上传/无重分析**前提下重算签名 |
| `stale` | 有读数，但该读数是关于**另一个版本**的镜头 |
| `not_analyzed` | 从未跑过 |
| `failed` | 跑过，但失败了 |

### 各渠道的新鲜度判定

| 渠道 | 判定方式 | 成本 |
|---|---|---|
| `static` | 重算 `source_signature`（有界采样 3×64KB）并比对 | 读源文件 192KB |
| `multiframe` | 比对存储的 `start_sec/end_sec` 与当前 Segment 窗口 | **零成本**（DB 内比较） |
| `video_native` | 比对 `segment_start_sec/segment_end_sec` 与当前窗口 | **零成本** |

**为什么 `video_native` 匹配窗口也不是 `current`**：`source_signature`/`version_key`
包含 `clip_sha256`，重算需要重新剪辑——那是"工作"，不能在查询里做。所以标
`freshness_unverified` 并说明原因，**不猜**。

**为什么源文件不可读时是 `unverified` 而不是 `current`**：无法核验 ≠ 没有变化。

### 关键区分（任务书 §6 要求必须写明的四条）

1. 静帧语义结果 **≠** 看过视频
2. 多帧运动分析 **≠** 原生视频理解
3. 原生视频观察 **≠** 已看完整素材库
4. 模型"精彩/动作/情绪"线索 **≠** 人工确认事实

页面上这三个徽标永远分开显示，颜色不同，文字不同。

---

## 4. 覆盖率口径

```
library_segments           = 项目全部 Segment 数（分母，恒定）
eligible_segments          = eligibility == eligible
needs_review_segments      = eligibility == needs_review
excluded_segments          = eligibility == excluded

static_valid               = 有 static 读数且 state ∈ {current, unverified}
multiframe_valid           = 同上
video_native_valid         = 同上

segments_with_any_valid_reading  = 三渠道中**任意一个**有效（去重）
no_valid_model_reading     = library_segments - segments_with_any_valid_reading
coverage_complete          = (任何有效读数数 == library_segments)
```

**去重是关键**：一个镜头被三个渠道覆盖时，在三个 `*_valid` 里各计一次，
但在 `segments_with_any_valid_reading` 里**只计一次**。把三者相加会重复计数。

**有过期记录 ≠ 已覆盖**：`stale` / `failed` 的记录**不**计入 `*_valid`。
（反向验证专门验证了这一条。）

**coverage 不随筛选/分页变化**：分母永远是整个项目。

---

## 5. 排序

### 5.1 `timeline`
按 `(asset_id, start_sec, segment_id)`。完全确定，分页稳定。

### 5.2 `editorial_priority`
离散桶，从高到低：

| 桶 | 条件 |
|---|---|
| `preferred` | 人工 `favorite` / `like` |
| `noted` | 人工设了 `story_role`；或有 `current` 读数 |
| `unrated` | 没有任何证据 |
| `excluded` | 人工 `exclude` |

**没有综合分数**。原因是：数据库里没有任何东西能校准"观众喜欢程度"，
一个编造的 0.83 分与真实测量在消费端无法区分。只输出离散桶 + `reasons` 列表。

### 5.3 跨素材多样性

当 `diversify=true`（默认）且按 `editorial_priority` 排序时：

```
在每个位置：
  1. 取当前优先级最高的、仍有剩余的桶
  2. 桶内选「已出现次数最少的 asset」的镜头
```

**两个必须同时成立的约束**：
- 人工收藏**永远**排在同桶或低桶镜头之前（不会被多样性降级）
- 单个长视频**不能**霸占首页（跨 asset 轮转）

> 实现过程中这里出过一个真实 bug：最初只用"前 offset+limit 个"窗口做轮转，
> 导致 3 个 asset 的库只显示出 2 个。已由测试 `test_diversity_spreads_across_assets` 捕获并修复。

---

## 6. 人工优先的具体规则

| 场景 | 结果 |
|---|---|
| 人工 `favorite` + 技术质量极差 | **仍为 `preferred`**，不被质量分一票否决 |
| 人工 `exclude` + AI 完美读数 | **仍为 `excluded`** |
| 人工设了 `story_role` | AI 结果不得覆盖，原样保留 |
| `UserAnnotation.rating` 与 `Segment.user_rating` 冲突 | **注解优先**（更新、更具体），`rating_source` 字段公开说明来源 |
| AI 读到 `uncertain` / `not_visible` | 原样保留在 `evidence_state`，不改写成肯定内容 |

---

## 7. 查询成本

**实测 SQL 语句数**（`test_tsb368_query_cost.py` 通过 SQLAlchemy 事件监听计数）：

| Segment 数 | 候选数 | SQL 语句数 |
|---|---|---|
| 4 | 4 | **7** |
| 40 | 40 | **7** |
| 500 | 500 | **7** |
| 1000 | 1000 | **7** |

固定 7 条：segments / assets / annotations / static / motion / verdicts / video。

> 初版实现是每 Segment 查 3 次（1000 镜头 = **3003 条**），被 N+1 测试当场捕获后
> 改为批量加载。

---

## 8. 只读保证

| 保证 | 验证方式 |
|---|---|
| 不创建 Job | `test_browsing_creates_no_jobs_versions_or_edits` 比较前后计数 |
| 不建 Storyboard 版本 | 同上 |
| 不改人工数据 | `test_reading_does_not_mutate_human_data` |
| 不发外部请求 | 无任何网络客户端调用；测试仅用 `TestClient` |
| 默认不碰文件系统 | `verify_freshness=false` 为默认；真浏览器验收实测无写请求 |
| 不改原片 | 只读 `safe_stat` / `safe_open`，无写路径 |
