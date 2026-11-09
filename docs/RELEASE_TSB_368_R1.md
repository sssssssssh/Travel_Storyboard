# TSB-368-R1 真实素材验收与策划输入合同收口

| 字段 | 内容 |
|---|---|
| 基线 | `4520636999a45425f5b0c06f5924adda7d99554b` |
| 上一轮代码 | `2bd05293424bbd7983bfe893a67415e8b7f08848`（TSB-368-A/B） |
| 证据目录 | [docs/validation/tsb368-r1/](validation/tsb368-r1/) |

---

## 一、结论速览（六项状态分开陈述）

| 项 | 状态 |
|---|---|
| **代码合同修复**（P0-01 / P1-02 / P1-03） | ✅ **完成**，RED→GREEN→反向验证 |
| **真实素材 UAT（数据聚合/未知状态）** | ✅ **PASS**，12/12 项 + 页面 8/8 |
| **真实模型输出 UAT** | ✅ **PASS**（静帧 8 条、多帧 4 条真实结果，内容核对无误） |
| **真实浏览器页面验收** | ✅ **PASS**（真实 Chrome，99 镜头） |
| **合成数据测试** | ✅ 通过 |
| **Windows/Ubuntu CI** | 见 §七 |
| **TSB-369 输入合同** | ✅ **已定型**，见 `CONTEXT_CONSUMPTION_CONTRACT.md` |

> ⚠️ **真实素材 UAT 能执行，是因为本机存在用户授权的真实项目**
> （`M5-real-acceptance`，6 个 DJI 4K 航拍视频、99 个镜头）。
> 这不是合成素材冒充。但**原生视频渠道 0 条结果**、**人工标注 0 条**，
> 这两个维度的验收**无法执行**，见 §五。

---

## 二、P0-01：人工排除被 `pending` 注解覆盖

### 2.1 缺陷

`_eligibility()` 用 `annotation_rating or segment.user_rating or "pending"` 取评分。
应用在用户只写了备注时会自动创建一条 `rating="pending"` 的注解行 —— 它**非空**，
于是覆盖了 Segment 上的 `exclude`。

**后果**：用户明确排除的镜头，会以 `eligible` 出现在默认查询和策划输入里。

### 2.2 修复

抽出单一事实源 `normalized_rating(segment, annotation) -> (rating, source)`：

1. 注解有**明确**评分（`favorite`/`like`/`exclude`）→ 用它；
2. 否则用 Segment 列；
3. 否则 `pending`。

`_human()` 与 `_eligibility()` **共用这一个函数**，`_eligibility()` 改为接收
已归一化的评分，不再自己推导。两套优先级正是二者漂移的根源。

### 2.3 RED / GREEN / 反向

| 阶段 | 结果 |
|---|---|
| RED | 5 项失败（`exclude+pending` 仍 eligible、进入 context、出现在 eligible 查询…） |
| GREEN | 全部通过 |
| 反向验证 | 恢复旧优先级 → **5 项精确失败** |

---

## 三、P1-02：`needs_review` 进入自动策划候选

### 3.1 缺陷

`build_context()` 用 `c.eligibility in (ELIGIBLE, NEEDS_REVIEW)`。
文档写明 `needs_review` **不得**自动策划，代码却把它放了进去。

### 3.2 修复

- 默认规划候选**只来自 `eligibility == eligible`**；
- `needs_review`/`excluded` 仍可在 `/candidates?eligibility=all` 查看，仍计入 coverage；
- 新增 `withheld: {needs_review_segments, excluded_segments}`，
  让下游知道"没给"≠"没有"；
- **未分析但合法**的镜头（`not_analyzed`）**仍然保留**在候选里。

### 3.3 RED / GREEN / 反向

RED 4 项失败（`needs_review`/`archived`/`invalid` 各 1 项 + 计数项）；
GREEN 全过；反向验证恢复旧条件 → **4 项精确失败**。

---

## 四、P1-03：`input_digest` 对内容变化不敏感

### 4.1 缺陷

原 digest 只覆盖 ID、时间窗、评分、角色、渠道 state、优先级。
人工备注从「普通海景」改成「旅程情绪高潮」，digest **不变**。

### 4.2 修复

重写 `input_digest`，覆盖下游**实际会读**的一切：

- 身份与时间窗
- 人工评分 + 来源 + 故事角色 + **备注全文**
- 资格状态
- 每渠道：`state` + `result_id` + `source_version` + **内容指纹**
- 会改变语义的 `limitations`

### 4.3 内容指纹为何必要

只记 `result_id` 不够：模型重跑可能**原地改写同一行**，或复用同一签名而修正内容。
新增 `_content_fingerprint()`，对投影后的内容（scene_tags/subjects/actions/
motion_summary/video_summary/events）做 SHA-256 前 16 位。

**专门的测试**：`test_digest_tracks_reading_content_even_when_the_version_id_is_unchanged`
—— 结果 ID 与 `source_signature` 都不变，只改内容，digest 必须变。
移除指纹后该测试**精确失败**。

### 4.4 保持确定性

- 同一输入、不同调用 → 相同 digest（实测 + 测试）
- 只改**无关项目** → digest 不变
- 只改**已过期**结果 → digest 不变（只有当前读数才是输入）
- 不含路径、令牌、随机值、时间戳

---

## 五、UAT-01：真实素材库只读验收

### 5.1 素材库事实

| 项 | 值 |
|---|---|
| 项目 | `M5-real-acceptance` |
| Asset | **6 个**（DJI 4K 航拍，合计约 333 秒） |
| Segment | **99 个** |
| 源文件 | **5 个可访问，1 个缺失** |
| 静帧语义结果 | 14 条，**8 条当前有效** |
| 多帧运动结果 | 10 条，**4 条当前有效** |
| 原生视频观察 | **0 条** |
| 人工标注 | **0 条** |

### 5.2 API 验收 12/12

| # | 检查 | 结果 |
|---|---|---|
| 1 | `library_segments` == DB Segment 数 | **PASS**（99 == 99） |
| 2 | 三种资格之和 == library | **PASS**（99） |
| 3 | 分渠道有效数 == DB 当前记录 | **PASS**（8/4/0） |
| 4 | 分渠道有效数 ≤ library（去重） | **PASS** |
| 5 | `no_valid + any_valid` == library | **PASS**（89 + 10 = 99） |
| 6 | 新鲜度如实标注 | **PASS**（静帧 8 条全为 `freshness_unverified`，无一伪称 `current`） |
| 7 | 人工评分与资格一致 | **PASS**（99 全 `pending`，0 排除） |
| 8 | `needs_review`/`excluded` 不进 context | **PASS**（泄漏 0） |
| 9 | `input_digest` 重复请求一致 | **PASS** |
| 10 | 分页无重复、total 一致 | **PASS**（30+30，重叠 0） |
| 11 | 无路径/令牌/原始 payload | **PASS**（命中 0） |
| 12 | 未创建 Job/版本/改动人工数据 | **PASS**（前后完全一致） |

**实测延迟**（99 镜头）：全部 124ms / 合格 24ms / 待复核 80ms / 排除 15ms / context 17ms。

### 5.3 页面验收 8/8（真实 Chrome）

| 检查 | 结果 |
|---|---|
| 页面覆盖率 == API | **PASS**（99 / 10 / 89） |
| 三渠道独立徽标 | **PASS**（静帧 8 未验证 + 12 未分析；多帧 2 未验证 + 18 未分析；原生 20 全未分析） |
| 未分析真实展示 | **PASS**（`current` 计数 **0**，没有一个镜头被伪称已验证） |
| 切换资格筛选分母不变 | **PASS**（全为 99） |
| 分页 | **PASS**（1–20 → 21–40） |
| 多样性 | **PASS**（首页覆盖 6 个素材） |
| 零写请求 | **PASS** |
| 无应用异常 | **PASS** |

**UI 限制（已记录，不算缺陷）**：素材筛选下拉只含当前页出现的素材，不是全局 Asset 列表。

### 5.4 真实内容抽样核对

抽取 25 个镜头（覆盖有读数与未分析两类）。

**多帧结果抽查**（真实输出，质量高）：

> "Scenic lakeside shot with paddle boats. ... the white pagoda on the right drifts
> rightward ... Boats drift slowly on the water throughout. The reframing between
> t=8.67s and t=9.83s is noticeably larger than between earlier frames; **the
> sampling cannot confirm whether this was a fast continuation of the pan or a
> cut**."

模型**主动声明不确定性**，没有编造。这与清真寺、湖畔、街景等多个镜头互相印证。

**`content_mismatch` 记录**：**0 条**。抽样范围内未发现模型描述与时间窗错配。

### 5.5 ⚠️ 真实素材 UAT 发现的产品缺陷（P0-02）

**这是本轮最重要的发现。**

真实库的 8 条静帧结果，`scene_tags`/`subjects`/`visible_actions` **全部为空**。
最初误判为"模型没产出内容"。

**根因**：生产写入点是 `semantic._store_result` 的
`analysis_json=validated.visual` —— **扁平结构**。而 `director_candidates.py`
读的是 `analysis_json["visual"]["scene_tags"]` —— **嵌套结构**。

**全库统计**：嵌套 772 条、扁平 14 条。**两种格式同时存在**，而代码只兼容一种。

**后果**：本项目所有真实静帧内容在导演候选页**完全不可见**。

**修复**：新增 `_visual_payload()`，两种格式都支持。

**修复后真实内容立即恢复**：

| 镜头 | 静帧标签 | 主体（节选） |
|---|---|---|
| 4af9f722 0–3s | indoor, transport, architecture, urban | "woman in blue denim jacket ... walking toward camera"、"travelers with rolling suitcases" |
| 4b482acf 0–1s | transport, street, urban, sky | "white van on street"、"blue vehicle" |
| 6b46d94d 0–2s | indoor, religious, other | "crowd of people standing indoors"、"pairs of shoes on a white marble floor" |
| 6f6a7226 0–16s | architecture, indoor, religious | "domed ceiling"、"stained glass windows"、"hanging lamps" |

**这个缺陷不可能被合成测试发现** —— 我上一轮造的测试数据全部是嵌套格式，
"测试通过"只是证明了代码能读它自己造的数据。

### 5.6 只读与隐私

- 查询前后 DB 关键记录数、Storyboard 版本、Job 数**完全一致**；
- 页面零写请求；
- 响应中无 `source_path`、令牌、`request_usage`、Windows 路径；
- **未上传任何视频、未调用外部模型、未启动分析任务**；
- 真实截图未提交到仓库（含真实文件名与画面信息）。

---

## 六、TSB-369 输入合同

见 [CONTEXT_CONSUMPTION_CONTRACT.md](validation/tsb368-r1/CONTEXT_CONSUMPTION_CONTRACT.md)。

核心答案：**TSB-369 通过 `candidate_summaries[].readings` 的 `evidence_refs`
二次调用已有只读端点取内容**（方案 A），而不是只拿 ID 编故事。

合同明确规定：
- 只对 `current`/`freshness_unverified` 取内容；`stale`/`failed`/`not_analyzed` 不取；
- `uncertain`/`not_visible` 原样传递；
- 取不到记 `content_unavailable`，不得补默认值；
- `brief.theme` 是用户输入，**不是视频事实**。

---

## 七、测试与 CI

| 项 | 结果 |
|---|---|
| 新增合同测试 | **28 项**（`test_tsb368r1_contracts.py`） |
| 后端全量 | **1832 collected / 0 failed / 0 errors / 6 skipped** |
| 前端四项 | typecheck / lint / **148 passed** / build 全部通过 |

6 项 skip 均为 `link creation is a platform fact`，已在白名单登记。

---

## 八、范围声明

**修改**：
- `backend/app/services/director_candidates.py`（P0-01 归一化评分、P0-02 扁平 JSON）
- `backend/app/services/director_query.py`（P1-02 资格、P1-03 digest）
- `docs/REGRESSION.md`
- 新增 `backend/tests/test_tsb368r1_contracts.py`（28 项）
- 新增本轮文档

**未改动**：原片、Storyboard、Alembic 迁移、既有测试断言、TSB-366/367 基础设施。

**未引入**：新数据库表、向量检索、新评分算法、新依赖。

---

## 九、未完成 / 遗留

1. **原生视频渠道零覆盖**：本库 `SegmentVideoObservation` 为 0 条，
   该渠道的"真实效果"无法验收。需要一次真实的视频分析才能验证。
2. **人工标注为零**：本库无任何 `UserAnnotation`，`favorite`/`exclude`/备注的
   真实验收只能靠单元测试，无法在真实库上观察。
3. **一个源文件缺失**（`DJI_..._0007_D.MP4`）：`verify_freshness=true` 时该素材的
   静帧结果会返回 `freshness_unverified`。这符合设计，但意味着该素材的
   新鲜度永远无法确认。
4. **未验证 `verify_freshness=true` 的真实 I/O 成本**：任务书要求先征得同意并对
   受控小批评估，本轮未执行（默认 `false`，浏览不读源文件）。
5. **UI 素材筛选下拉只含当前页素材**：已记录为 UI 限制，未修（非本轮范围）。
6. **嵌套/扁平两种 `analysis_json` 长期并存**：本轮做了兼容读取，但**未迁移历史数据**。
   建议后续统一写入格式并清理，否则每个新读取方都要重复这段兼容逻辑。
