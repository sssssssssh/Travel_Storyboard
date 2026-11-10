# TSB-369 发布记录：AI 导演故事线生成与镜头编排（证据受限、草案优先）

| 字段 | 内容 |
|---|---|
| 基线 | `a6a79076a26a46e698abf182fec8444d2b17fff4`（TSB-368-R2 文档终态） |
| 证据目录 | [docs/validation/tsb369/](validation/tsb369/) |
| **代码提交** | `f1ecc2ce083cb9b49b8f8915087f28ba870fede7` |
| **CI（首轮）** | run #128，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42737573260 |
| **审计轮提交** | `47867c6d1038fbdc13debc654830363b1ba57b12` |
| **CI（审计轮）** | run #130，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42751575930 |
| 合同说明 | [PLAN_CONTRACT.md](validation/tsb369/PLAN_CONTRACT.md) |

---

## 一、最终可宣称的状态

> **TSB-369 合同 + 离线可解释故事线策划 + 只读浏览器预览：PASS；
> 真实模型创作能力和发布门禁：BLOCKED。**

本轮**未**发起任何真实外部模型请求，**未**上传任何素材。

---

## 二、交付内容

### 2.1 三个新模块

| 模块 | 职责 |
|---|---|
| `app/services/director_plan_input.py` | 证据输入聚合（三渠道 + 零副作用 + 批量加载） |
| `app/services/director_plan_schema.py` | 草案 schema（`extra="forbid"`，`Claim` 必须带证据） |
| `app/services/director_plan_validation.py` | **严格校验器**（22 类错误码） |
| `app/services/director_plan_rules.py` | 离线可解释编排器（三种策略） |
| `app/services/director_plan_providers.py` | 生成器接口 + Mock + **未授权门禁** |
| `app/api/director.py` | `POST /director/plans/preview`（只读） |
| `frontend/src/pages/DirectorPlans.tsx` | 只读草案页 |

### 2.2 五项关键决策的落实

| 决策 | 落实方式 | 守护测试 |
|---|---|---|
| **证据先于叙事** | `Claim.kind` 四分类；`observed_signal` 无证据即拒绝；主题不入草案 | `test_the_theme_never_becomes_a_footage_claim` |
| **不重造视频理解** | 复用 `dc._visual_payload` / `dc._normalize_video_events`，零新解析器 | `test_flat_static_and_nested_static_both_reach_the_planner` |
| **草案与正式分离** | 不调 `get_current_version()`；纯读探测；无 apply 按钮 | `test_preview_does_not_create_a_storyboard_for_a_bare_project` |
| **离线 ≠ AI** | UI/日志标注 `offline_rules`；`model` 模式 403 零网络 | `test_model_mode_is_403_with_no_network` |
| **结构 ≠ 故事** | `insufficient_evidence` 返回空方案；`provisional` 如实标注 | `test_insufficient_evidence_yields_no_variants_rather_than_fabrication` |

---

## 三、最关键的防线：校验器

离线规则也是启发式；未来模型更不可控。校验器是唯一放行口。

| 组 | 错误码 | 拒绝什么 |
|---|---|---|
| 身份 | `UNKNOWN_SEGMENT` | 引用不在候选集的镜头（含其他项目的） |
| | `ASSET_MISMATCH` | asset 与 segment 不匹配 |
| 窗口 | `INVALID_SHOT_WINDOW` | 越界、反向、NaN、Inf、非数值 |
| | `INVALID_SHOT_DURATION` | `planned_duration ≠ out - in` |
| 时间轴 | `INVALID_TIMELINE` | 不连续、总长不等于求和、gap 不符 |
| 断言 | `UNSUPPORTED_VISUAL_CLAIM` | 声称观察却不带证据，或引用不存在的读数 |
| | **`SEMANTIC_UPGRADE`** | **把 `uncertain`/`not_visible` 说成已观察** |
| | `INVALID_EVENT_TIME` | 事件时间越出镜头窗口 |
| | `UNSUPPORTED_HUMAN_CLAIM` | 声称人工输入但镜头没有 |
| 形状 | `ORPHAN_SECTION` / `DUPLICATE_SEGMENT` / `DUPLICATE_SHOT_ID` | 结构错误 |
| | `VARIANTS_NOT_DISTINCT` | 三个方案只差标题 |
| 一致性 | `DIRECTOR_INPUT_CHANGED` / `PROJECT_MISMATCH` | 草案与输入不符 |

**`SEMANTIC_UPGRADE` 单独立码**：静帧模型说"看不清"，规划器写成"镜头里有湖"——
这不是格式错误，是语义造假，必须可被单独统计与拒绝。

---

## 四、真实库实测（只读）

2026-11-10 真实库 `M5-real-acceptance`：

| 项 | 值 |
|---|---|
| 素材库 | 99 个镜头 |
| 有可靠读数 | **10** |
| 无有效读数 | **89** |
| 原生视频有效 | **0** |
| 人工标注 | 0 |

**真实 Chrome UAT：12/12 通过**，逐项：

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 生成前即显示真实覆盖率（含未分析数） | PASS |
| 2 | 页面声明只读不保存 | PASS |
| 3 | 无任何可写回系统的按钮 | PASS |
| 4 | 草案生成成功 | PASS |
| 5 | 明确标注为离线规则生成 | PASS |
| 6 | 无读数镜头显示「没有画面描述」而非编造 | PASS |
| 7 | 顺序可本地调整（时间轴重算） | PASS |
| 8 | 导出内容声明为只读草案 | PASS |
| 9 | **只发出一个预览 POST，无其他写请求** | PASS |
| 10 | **数据库前后完全一致** | PASS |
| 11 | 页面无未处理异常 | PASS |
| 12 | **用户主题未成为素材事实** | PASS |

DB 前后对比（第 10 项原始证据）：

```
before: {storyboards:1, versions:1, jobs:23, operations:0, editPlan:0, annotations:0}
after : {storyboards:1, versions:1, jobs:23, operations:0, editPlan:0, annotations:0}
```

页面实际呈现（99 镜头、10 有读数）：
- 8 个镜头中**只有 #1 有静帧观察**（transport）
- 其余 7 个全部标注**「无内容证据，仅按时间线结构建议保留」**
- 每段标注**「待复核：未分析音频；该镜头没有任何模型读数」**
- 汇总**「需人工复核 8 项」**

---

## 五、RED → GREEN → 反向验证

### 5.1 新增测试

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_tsb369_input.py` | 17 | 输入聚合、N+1、零副作用 |
| `test_tsb369_plan.py` | 32 | 编排器 + 校验器负例 + provider |
| `test_tsb369_api.py` | 24 | 只读 API、409、403、422、隔离 |
| `DirectorPlans.test.tsx` | 14 | 前端只读与不虚构 |
| **合计** | **87** | |

### 5.2 RED 记录

新功能无"修复前行为"，RED 表现为模块缺失 + 实现期真实缺陷：

| 缺陷 | RED 现象 | 修复 |
|---|---|---|
| `PlanningEntry` frozen 却后赋值 | `FrozenInstanceError` | 注解在构造前加载 |
| fixture 固定 `source_signature` | `UNIQUE constraint failed` | 每次生成唯一签名 |
| `plan_offline` 忽略 `brief.style` | 选「重点优先」仍按时间线排 | 用户策略优先，其余补位 |
| limitation 用词与断言不符 | 断言找不到 "freshness" | 对齐真实错误码 |
| 前端 coverage 生成前为 0 | 页面显示 0 个镜头 | 改用 candidates 查询的 coverage |

### 5.3 反向验证（3 项关键防线）

| # | 注入缺陷 | 失败用例 | 恢复后 |
|---|---|---|---|
| RV-1 | 去掉证据可用性检查 | `test_validator_rejects_citing_evidence_the_shot_does_not_have` | 通过 |
| RV-2 | 允许把 `uncertain`/`not_visible` 当观察 | `test_validator_rejects_upgrading_a_hedged_reading[uncertain]` `[not_visible]` | 通过 |
| RV-3 | 去掉输入 digest 一致性检查 | `test_validator_rejects_a_plan_built_on_different_input` | 通过 |

全部恢复后：73/73 通过，全仓 `REVERSE-VERIFICATION` 标记扫描为空。

---

## 六、回归

| 项 | 结果 |
|---|---|
| 后端全量 | **1942 collected / 0 failed / 0 errors / 6 skipped** |
| 前端 typecheck | PASS |
| 前端 lint | PASS（0 error） |
| 前端测试 | **162 passed**（18 文件） |
| 前端 build | PASS |

6 项 skip 均为 `link creation is a platform fact`，已登记白名单。

---

## 七、范围声明

**新增**：6 个服务模块、1 个 API 端点、1 个前端页面、4 个测试文件、本轮文档。

**未改动**：
- 原片、人工标注、Storyboard、Alembic、既有 API 契约
- `director_candidates.py` / `director_query.py`（只消费，不修改）
- TSB-366/367/368 全部修复
- 旧入口 `/story/analyze` 与 `AIDirector.tsx`

**未引入**：新表、向量检索、新综合评分、新依赖、任何外部模型调用。

**明确未做**：
- 把草案应用/保存为正式 Storyboard（留待独立审批）
- 真实模型策划（未授权，代码中零网络拒绝）
- 音乐推荐、调色执行、自动渲染
- 全库重新分析

---

## 八、未完成与风险

1. **真实模型创作能力未验证** —— `BLOCKED`。本轮只有离线规则，不是 AI 导演。
2. **真实库原生视频读数为 0** —— 视频渠道对草案贡献为零。
3. **89/99 镜头无任何读数** —— 草案中绝大多数镜头只是结构占位。
4. **离线规则的价值上限** —— 它给顺序、时长、结构与待复核项，
   不给真正的叙事洞察。**不能称为"AI 已理解素材"**。
5. **`complete` 状态含义有限** —— 只表示结构完整（≥3 个有读数镜头），
   不表示模型看过全部素材。
6. **未做 60+ 视频 / 200GB 性能验收**。

---

## 九、是否可进入下一阶段

**代码层面：可以。** 合同、校验器、只读边界都有测试与反向验证支撑。

**但需明确**：本轮交付的是"**可信的剪辑计划生成器骨架**"，
不是"真正的模型驱动 AI 导演"。任务书本轮的完成标准正是前者。

若要成为后者，需要打开真实模型授权，且届时模型的输出仍要过同一套校验器。


---

## 十、CI 执行结果

| 项 | 值 |
|---|---|
| 代码 SHA | `f1ecc2ce083cb9b49b8f8915087f28ba870fede7` |
| run | **#128** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42737573260 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ |
| `Required FFmpeg (windows-latest)` | ✅ |
| `Required FFmpeg (ubuntu-latest)` | ✅ |
| `Backend (Python 3.11)` ×2 | ✅ |
| `Backend (Python 3.13)` ×2 | ✅ |

---

## 十一、关闭标准对照（任务书 §11）

| 阶段 | 标准 | 状态 |
|---|---|---|
| **证据输入合同** | 三渠道保留且不压平、绝对时间、1000 镜头 N+1、输入变更 409、跨项目隔离、零写入、不泄漏 | ✅ PASS |
| **故事线草案合同** | schema 严格校验、校验器拒绝越界时间/跨项目/幻觉断言/语义升级、Mock 与离线共用入口、生产不暴露 mock | ✅ PASS |
| **只读 Preview API** | 无 Storyboard/Job/版本副作用、DB 不变、409 不带错误草案、无过滤/覆盖、不建空版本 | ✅ PASS |
| **浏览器库内预览** | 文案明确模型边界、不编造、排除/待复核不出现在方案、顺序调整重算、导出只读 | ✅ PASS |
| **合成全链路** | 生产写入路径、1–1000 镜头规模、确定性、覆盖率变化后不陈旧 | ✅ PASS |
| **真实库只读验收** | 实际已存证据不泄漏、不虚构、覆盖率如实 | ✅ PASS（12/12） |
| **真实模型效果** | 独立授权、费用确认、真实调用、人工评价 | 🔒 **BLOCKED** |
| **正式应用/发布** | 单独确认保存机制与发布门禁 | ⛔ **未实现 / 不得宣称可发布** |

---

## 十二、与任务书"设计结论"的对照

> "TSB-369 必须先成为'可信的剪辑计划生成器'。"

本轮交付的正是这一层：**准确使用已看到的内容、诚实承认没看到的内容、
让用户能理解并修改剪辑方案**。

真实库实测中，8 个镜头里只有 1 个有静帧观察，其余 7 个被明确标注为
「无内容证据，仅按时间线结构建议保留」——系统没有为了好看而编造。

> "才有资格进一步变成'真正的模型驱动 AI 导演'。"

这一步**尚未开始**。要成为真正的 AI 导演，需要打开真实模型授权，
届时模型输出仍要过同一套校验器。本轮已把那个入口做成零网络的功能门禁。


---

# 附录：审计轮（TSB-369 复审）

首轮交付后按任务书 §8 测试矩阵逐条复核，发现**两处真实缺陷**与若干未覆盖条目。
本附录记录补齐过程。

## A-1 真实缺陷一：无音频分析却可以声称"听到节拍"

### 现象

规划器对 `audio_analyzed=False` 的镜头写：

> 「音乐的节拍在 1.2 秒处重击，适合卡点剪辑」

校验器**返回 `[]` 放行**——草案会被展示给用户。

### 为什么这违反本轮要求

这是"把没观察到的说成已观察"在**音频通道**的同一类错误。
特别要求第 1 条不限于画面。

### RED（修复前实测）

```
audio_analyzed = False
problems = []
*** 漏洞：无音频分析却接受'听到节拍'的断言 ***
```

### 修复

新增 `_AUDIO_CLAIM_MARKERS` 词表与 `_mentions_audio()`，在 `observed_signal` 分支最先检查，
命中即报 `AUDIO_CLAIM_WITHOUT_ANALYSIS` 并 `continue`（不再继续找证据）。

同时在 schema 层加固：`EvidenceRef.channel` 是封闭 Literal（三渠道），
**音频根本不是可引用的证据类型**。

### GREEN

| 用例 | 结果 |
|---|---|
| `test_a_claim_about_sound_is_rejected_when_audio_was_never_analysed` | PASS |
| `test_a_sound_claim_is_rejected_even_when_wording_matches_a_visual_channel` | PASS |
| `test_an_audio_channel_cannot_even_be_expressed_in_the_schema` | PASS |
| `test_a_silent_shot_is_marked_audio_unanalysed_for_review` | PASS |
| `test_the_offline_planner_never_emits_an_audio_claim` | PASS |

### 反向验证

关闭 `_mentions_audio` 守卫 → 前两条**精确失败**；恢复后通过。

---

## A-2 真实缺陷二：锁定镜头未被标注

### 现象

`PlanningEntry` 没有 `locked_in_base`。若 base 版本中某镜头被用户锁定，
草案仍会给出新的使用区间，却**不告知用户该镜头是锁定的**。

本轮不写 Storyboard，所以这不是数据破坏；但它会**误导用户对"应用"后果的理解**。

### 修复

`_storyboard_state()` 增加第三返回值：从当前版本的 `shots` 快照读出 `locked` 为真的
`segment_id` 集合（**纯读，不写**）。命中者标注 `locked_in_base=True` 并追加
`locked_in_base_storyboard_version` 限制项。

### GREEN

| 用例 | 结果 |
|---|---|
| `test_a_locked_shot_is_flagged_so_the_draft_cannot_silently_retime_it` | PASS |
| `test_an_unlocked_shot_is_not_flagged` | PASS |
| `test_reading_the_input_never_writes_to_a_locked_storyboard` | PASS |

### 反向验证

关闭锁定标注 → 第一条**精确失败**；恢复后通过。

---

## A-3 补齐的覆盖缺口

| §8 矩阵条目 | 首轮状态 | 本轮补齐 |
|---|---|---|
| 性能 4/40/**500**/1000 四档 | 仅 4/60/1000 | 参数化四档 + 趋势断言 |
| 时效 stale/failed 独立负例 | 缺 | 4 条（failed/superseded/window/stale） |
| 锁定历史版本 | 缺 | 3 条 |
| 无音频声称节拍 | **缺（漏洞）** | 5 条 |
| 生命周期：连续两次预览 | 缺 | 字节一致 |
| 生命周期：切项目 | 部分 | 加切回再切 |

---

## A-4 审计轮验证结果

| 项 | 结果 |
|---|---|
| TSB-369 用例总数 | **93**（73 + 20 审计） |
| 审计轮 RED → GREEN | 2 处真实缺陷 |
| 审计轮反向验证 | 2 项，均精确命中 |
| 后端全量 | **1962 collected / 0 failed / 0 errors / 6 skipped** |
| 前端四项 | 162 passed，typecheck/lint/build 全通过 |
| 真实 Chrome UAT（重跑） | **12/12 PASS** |

### UAT 新增验证项

填入主题「音乐节拍与海浪声」后生成草案：

```
5. audio-ish theme never becomes a sound claim  PASS  {"beat":false,"waves":false}
```

页面任何位置都未出现这两个词。

### 数据库前后（8 项计数）

```json
before: {"storyboards":1,"versions":1,"jobs":23,"operations":0,"editPlan":0,"annotations":0,"segments":99,"assets":6}
after : {"storyboards":1,"versions":1,"jobs":23,"operations":0,"editPlan":0,"annotations":0,"segments":99,"assets":6}
```

---

## A-5 阶段 D 正式验收

见 [PHASE_D_ACCEPTANCE.md](validation/tsb369/PHASE_D_ACCEPTANCE.md)。

要点：

| 项 | 结论 |
|---|---|
| D-01 真实库只读预验收 | **PASS**：99 镜头 / 10 有读数 / 89 无读数 / 原生视频 0 / 锁定 0 |
| D-01 逐项证据核对 | 5 个有读数候选，result_id 可回读，**无一条冒称 `current`** |
| D-01 只读一致性 | 6 张表计数前后一致 |
| D-01 逐帧人工观看 | **MANUAL_REVIEW_PENDING** |
| D-02 模型门禁 | **PASS**：403 + Provider 拒绝 + Mock 不可选，零网络 |
| 真实模型效果 | 🔒 **BLOCKED (awaiting_explicit_approval_and_price_confirmation)** |

---

## A-6 审计轮结论

首轮交付的**架构与边界是正确的**（只读、证据强制、校验器拦截），
但 §8 矩阵暴露了两处**语义层疏漏**：音频通道的同类幻觉、锁定状态的披露缺失。

两处都已按"先 RED 再修"的方式处理，并用反向验证证明守卫有效。

**本轮之后仍不得宣称**：
- AI 已理解全部素材（89/99 无读数，原生视频 0）
- AI 导演能力已验收（真实模型 BLOCKED）


---

## A-7 CI 执行结果（审计轮）

| 项 | 值 |
|---|---|
| 审计轮 SHA | `47867c6d1038fbdc13debc654830363b1ba57b12` |
| run | **#130** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42751575930 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ |
| `Required FFmpeg (windows-latest)` | ✅ |
| `Required FFmpeg (ubuntu-latest)` | ✅ |
| `Backend (Python 3.11)` ×2 | ✅ |
| `Backend (Python 3.13)` ×2 | ✅ |

两个提交的 CI 均通过：

| 阶段 | SHA | run | 结论 |
|---|---|---|---|
| 首轮交付 | `f1ecc2c` | #128 | ✅ 7/7 |
| 审计补漏 | `47867c6` | #130 | ✅ 7/7 |

---

## A-8 最终交付清单

### 代码

| 文件 | 行数级别 | 作用 |
|---|---|---|
| `backend/app/services/director_plan_input.py` | ~560 | 证据输入聚合 |
| `backend/app/services/director_plan_schema.py` | ~180 | 草案合同 |
| `backend/app/services/director_plan_validation.py` | ~430 | 校验器（23 类错误码） |
| `backend/app/services/director_plan_rules.py` | ~430 | 离线编排器 |
| `backend/app/services/director_plan_providers.py` | ~110 | 生成器接口 + 门禁 |
| `backend/app/api/director.py` | +110 | 只读预览端点 |
| `frontend/src/pages/DirectorPlans.tsx` | ~470 | 只读草案页 |

### 测试

| 文件 | 数量 |
|---|---|
| `test_tsb369_input.py` | 17 |
| `test_tsb369_plan.py` | 32 |
| `test_tsb369_api.py` | 24 |
| `test_tsb369_audit.py` | 20 |
| `DirectorPlans.test.tsx` | 14 |
| **合计** | **107** |

### 文档

| 文件 | 内容 |
|---|---|
| `docs/RELEASE_TSB_369.md` | 发布记录（含审计轮附录） |
| `docs/validation/tsb369/PLAN_CONTRACT.md` | 合同与错误码 |
| `docs/validation/tsb369/REAL_LIBRARY_UAT.md` | 真实库 UAT |
| `docs/validation/tsb369/PHASE_D_ACCEPTANCE.md` | 阶段 D 验收 |
| `docs/validation/tsb369/README.md` | 证据索引 |
| `docs/REGRESSION.md` | 用例映射 |

---

## A-9 特别要求逐条对照

| # | 要求 | 落实 | 证据 |
|---|---|---|---|
| 1 | 不得把没分析过的镜头描述为已观察 | `Claim.kind` 四分类 + 证据强制 + `SEMANTIC_UPGRADE` + `AUDIO_CLAIM_WITHOUT_ANALYSIS` | 5 条音频用例 + 2 条语义升级用例 |
| 2 | 不得把离线/Mock 冒充真实 AI | UI 标注 `offline_rules`；Mock 外部不可达；`model` 模式 403 | `test_mock_mode_is_not_selectable` 等 4 条 |
| 3 | 不得自动保存 Storyboard 或改原片 | 不调 `get_current_version()`；6–8 表计数前后一致 | `test_preview_writes_nothing` + UAT #11 |
| 4 | 不得未授权调用外部模型 | 三层门禁，零网络，无 SDK import | `test_model_mode_is_403_with_no_network` 等 3 条 |
| 5 | 1000 镜头无 N+1 | 参数化 4/40/500/1000 + 趋势断言 | 5 条性能用例 |
| 6 | RED→GREEN→反向验证 | 首轮 3 项 + 审计轮 2 项 | 见 §5.3 与 §A-1/A-2 |
| 7 | 不降低断言/跳过/盲目重试 | 全程未删测试、未加 skip/xfail；`test_tsb368_candidates.py` 只增不改 | `git diff` 可查 |

---

## A-10 当前局限（诚实列出）

| # | 局限 | 影响 |
|---|---|---|
| 1 | **真实模型策划 BLOCKED** | 只有离线规则，不是 AI 导演 |
| 2 | **89/99 镜头无任何读数** | 草案大部分是结构占位 |
| 3 | **原生视频读数 0** | 视频渠道对草案贡献为零 |
| 4 | **逐帧人工观看未执行** | `MANUAL_REVIEW_PENDING`；无法验证模型描述与画面是否一致 |
| 5 | **音频从未分析** | 所有节奏建议都不基于声音；已由守卫强制不得声称 |
| 6 | **离线规则价值上限** | 给结构，不给叙事洞察 |
| 7 | **未做 200GB 性能验收** | 1000 镜头是合成规模 |
| 8 | **未实现"应用为正式版本"** | 任务书明确留待独立审批 |

---

## A-11 下一阶段建议

### 建议 1（最高价值）：开启真实模型策划

这是把"可信生成器"变成"AI 导演"的**唯一阻断项**。

需你确认：端点/供应商、发送内容（仅文字 or 含影像）、段数与长度、地区、
请求次数、**费用上限**、失败策略、敏感信息风险。

届时模型输出仍要过同一套校验器——`SEMANTIC_UPGRADE` 与
`AUDIO_CLAIM_WITHOUT_ANALYSIS` 会原样拦截。

### 建议 2：提高真实库覆盖率

89/99 无读数意味着草案信息量极低。补跑静帧/多帧分析（**不涉及外部模型**）
能显著提升草案可用性。

### 建议 3：人工逐帧观看验收

取 3–5 段已读数的镜头，人工确认模型描述与画面是否一致。
这是检验"静帧模型到底准不准"的唯一方法，与真实模型策划是**两件事**。

### 建议 4（需单独审批）：草案应用到正式 Storyboard

本轮明确未做。若要做，必须重新设计保存机制、冲突处理与回滚，
并单独走发布门禁。

### 不建议现在做

- 自动渲染
- 音乐推荐
- 调色执行

这些在证据覆盖率不足时做，只会放大错误。
