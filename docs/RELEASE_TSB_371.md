# TSB-371 发布记录：真实视频理解人工验收、故事线 A/B 盲评与可信效果报告

| 字段 | 内容 |
|---|---|
| 基线 | `cca8e973f054fac5924483efa0cff54445c127d1`（与任务书一致） |
| 证据目录 | [docs/validation/tsb371/](validation/tsb371/) |

---

## 一、终态定义（分开陈述）

| 终态 | 状态 |
|---|---|
| **`CODE_PASS`** | ✅ 功能、合成测试、真实浏览器无副作用验证全部通过 |
| **`FOOTAGE_REVIEW_READY`** | ✅ 有可播放样本、有可用评分 UI |
| **`FOOTAGE_REVIEW_PARTIAL/PASS`** | 🔒 **`MANUAL_REVIEW_PENDING`** —— 真人尚未系统打分 |
| **`AB_BLOCKED_NO_REAL_MODEL_PLAN`** | ✅ 如实显示（无真实模型方案） |
| **`G3_BLOCKED_UNAVAILABLE`** | ✅ 保持 |
| **`RELEASE_BLOCKED`** | ✅ 不宣称可发布 |

> **Codex 没有代替任何人打分。** UAT 中的一次点击是自动化验证「保存链路是否工作」，
> 它不构成人工观看结论，已在此明确标注并非人工验收数据。

---

## 二、371-0：账本对账与 G4 请求保护

### 2.1 三份文档的矛盾（任务书 §1.1）

| 来源 | 总量 | plan_text |
|---|---|---|
| `RELEASE_TSB_370.md` | 20/20 | 2 |
| `PAI_EAS_CAPABILITY_AND_AUTH.md` | 20/20 | 2 |
| `REAL_MODEL_RUN_LEDGER.md` | 17/20 | 0 |

**结论**：第三份是**未更新的中途快照**，与另两份不构成并列事实。

### 2.2 原始账本与数据库交叉核对

| 渠道 | 账本条目 | Job `calls` | 持久化结果 | 一致性 |
|---|---:|---:|---:|---|
| 静帧 | 12 | 11 | 11 | ⚠️ 账本多 1 |
| 多帧 | 2 | 3 | 2 | ⚠️ 账本少 1 |

- 20 条中有 **3 条是事后补记**（`bytes=0`），此前与实测条目无法区分；
- 两处缺口**无法闭合**（账本无 `request_id`，Job 无逐次字节）。

**标记 `RECONCILIATION_UNRESOLVED`**，采信口径：Job 记录+持久化结果为下界，
账本条目为上界。详见 [LEDGER_RECONCILIATION.md](validation/tsb371/LEDGER_RECONCILIATION.md)。

### 2.3 真正的跨进程锁（真实缺陷）

TSB-370 用 `msvcrt.locking` + 线程锁，**声称**跨进程原子。

**最小复现证明该声称是假的**：两个 spawn 进程都拿到了 `LK_NBLCK`。

修复过程中又发现自己的两个缺陷：
1. `INVALID_HANDLE_VALUE` 用有符号 `-1` 比较，永不成立 → 每个竞争者都"成功"；
2. 文件句柄被 spawn 子进程继承。

**最终用真实并发子进程验证**：2 进程抢 1 额度恰好 1 个赢；6 进程抢 3 额度恰好 3 个赢。
测试用 `subprocess` 而非 `multiprocessing.Pool`，因为池会复用 worker 掩盖竞态。

### 2.4 账本 schema v2

| 新增 | 作用 |
|---|---|
| `call_id` | 每次 charge 有唯一身份，可对账 |
| `dispatched` / `source` | 区分实测与补记 |
| `grant_id` + `grant()` | 新额度必须带 id、说明、渠道 caps；历史全部保留 |

**不通过删除文件获得额度**；未识别 schema 一律 `BudgetGuardBlocked`。
旧 v1 账本已无损迁移（20 条全保留：17 measured + 3 backfill）。

### 2.5 A/B 驱动同输入（任务书 §1.2）

**修复前**：digest 用 `max_candidates=12` 读取，离线方案却用 `40` 请求。

**RED 精确复现**：`max_candidates differs between requests: [12, 40]`

**修复**：单一 `INPUT` 常量，测试断言「两侧差异只允许是 planner 与授权字段」。

### 2.6 G4 完整往返（任务书 §1.3）

新增 16 项测试，全部喂入**模型真实形状的文本**（不是预解析对象），走完
`extract_json → parse_model_plan → validate_plan`。

覆盖 TSB-370 实际丢额度的两种情形：`response.text=None` 与解析路径崩溃。
另注入非法 JSON、截断 JSON、空 variants、幻觉镜头、音频幻觉、错误证据 ID、
同质方案、传输错误、无授权零调用。

删除 `director_plan_providers.py` 中 **37 行死代码** `_unused_legacy`。

---

## 三、371-A：样本与安全播放器

### 3.1 新增能力

| 端点 | 作用 |
|---|---|
| `GET .../evaluation/eligible-samples` | 真实候选、三渠道、证据状态、来源可用性 |
| `POST .../evaluation/clips` | 按需生成受保护预览片段 |
| `GET .../evaluation/clips/{id}` | 状态与时序映射（**不含路径**） |
| `GET/HEAD .../clips/{id}/media` | 流式播放，支持 Range/206 |

### 3.2 安全断言（全部有测试）

| 项 | 结果 |
|---|---|
| 路径穿越（4 种） | 全部 404/422 |
| 跨项目 clip | 404 |
| 响应不含文件路径 | ✅ |
| HEAD 200 + Range 206 | ✅ 真实 FFmpeg 产物验证 |
| 源不可用 | `SOURCE_UNAVAILABLE` 明确状态，**不计入模型错误** |
| 片段静音 | ✅ 页面注明「音频从未被模型分析」 |

### 3.3 样本选择

静帧 3 + 多帧 2 的推荐组合；同一镜头两渠道算**两项证据、一个镜头**；
未分析镜头作为对照但不进准确率分母；采样按 seed 可复现。

---

## 四、371-B：人工评分

### 4.1 独立存储

新增三张表：`evaluation_sessions` / `evaluation_items` / `evaluation_ratings`。

**与 `UserAnnotation`、Storyboard 完全分离**——评价者对模型描述的看法
不是用户对素材的看法。

### 4.2 评分维度

场景 / 主体 / 动作 / 时序（2=准确 1=部分 0=错误 null=无法判断），
幻觉（严重/轻微/无），剪辑可用性（有用/部分/无用）。

**「无法判断」是独立按钮，不是 0。**

### 4.3 发现并修复的统计缺陷

**`cannot judge` 被计入分母**：`evaluated_claims` 用了未过滤 `None` 的列表。

修复后：`null` 不进入分母，与任务书 §3.3 一致。

### 4.4 指标定义（严格按 §3.3）

| 指标 | 无评分时 |
|---|---|
| `strict_accuracy` | **`null`（N/A），不是 0 或 100%** |
| `partial_or_better_rate` | `null` |
| `hallucination.rate` | `null` |
| `timing.median_sec` | `null` |

**源不可用的镜头**：保留评分供审计，但**排除出准确率**，且服务端**拒绝**为其评分。

**证据被取代**：标 `STALE_EVIDENCE`，旧评分不再计入。

---

## 五、371-C：A/B 盲评

### 5.1 发现并修复的漏洞（重要）

**Mock 冒充 model 未被拒绝**：原实现只检查 `planning_mode == "model"`，
任何调用者都能设置这个标签。

**修复**：要求**真实调用回执**（`model_id` / `provider` / `call_id` / `elapsed_ms`），
由真实 adapter 在成功调用后签发。桩、手写 `PlanDraft`、离线方案都无法伪造。

### 5.2 配对规则

| 检查 | 拒绝码 |
|---|---|
| 左侧非离线 | `AB_LEFT_NOT_OFFLINE` |
| 项目不同 | `AB_PROJECT_MISMATCH` |
| **digest 不同** | `AB_INPUT_DIGEST_MISMATCH` |
| 目标时长不同 | `AB_TARGET_MISMATCH` |
| 无真实回执 | `AB_NO_REAL_MODEL_PLAN` |
| 内容完全相同 | `AB_NOT_DISTINCT` |
| 未通过校验器 | `AB_PLAN_INVALID` |

**检查顺序**：可比性问题（digest/target）先于来源问题报告，因为前者更可操作。

### 5.3 盲的保证

- A/B 映射由 `secrets.randbelow` 生成，**服务端存储**；
- 盲视图**不含** mapping / model_id / planning_mode / 原 title / variant_id；
- 提交后才揭示，且盲评结果**不可再改**；
- 单评价者时明示「只代表该评价者的偏好，不是人群胜率」。

---

## 六、371-D：报告导出

JSON / CSV / Markdown 三种格式，字段一致。

**关键保证**：无评分时打印 `N/A`，绝不打印 `0%` 或 `100%`。
三种格式都有测试断言不含路径、token、用户备注。

---

## 七、RED → GREEN → 反向验证

### 7.1 新增测试

| 文件 | 数量 |
|---|---:|
| `test_tsb371_ledger.py` | 16 |
| `test_tsb371_ab_driver.py` | 8 |
| `test_tsb371_g4_roundtrip.py` | 16 |
| `test_tsb371_samples.py` | 18 |
| `test_tsb371_ratings.py` | 17 |
| `test_tsb371_ab.py` | 14 |
| `test_tsb371_report.py` | 14 |
| `Evaluation.test.tsx` | 9 |
| **合计** | **112** |

### 7.2 反向验证（5 组，全部精确命中）

| # | 注入缺陷 | 失败用例 |
|---|---|---|
| RV-1 | `null` 算作 0 | `test_cannot_judge_is_excluded_from_the_denominator` |
| RV-2 | 盲视图泄漏 mapping | `test_the_blind_view_contains_no_provenance` |
| RV-3 | 跳过 digest 检查 | `test_a_different_input_digest_is_refused` |
| RV-4 | 去掉回执要求 | `test_a_mock_plan_dressed_as_model_is_refused` |
| RV-5 | 绕过账本额度 | 4 个 ledger 用例 |

**RV-2 首次未命中**——因为盲视图测试用的是新建 pair（mapping 尚为 `None`）。
改为从数据库重载后精确命中。**这是反向验证发现测试盲区的实例。**

---

## 八、真实 Chrome UAT（真实库）

**12/12 功能通过**，另有 2 项 INFO。

| # | 验证项 | 结果 |
|---|---|---|
| 1 | 镜头页显示真实覆盖率 | PASS |
| 2 | 可播放或明确说明原因 | PASS |
| 3 | media HEAD/Range | INFO（抽样未命中可播放 clip） |
| 4 | 会话创建并加载 items | PASS |
| 5 | session id 在 URL（可恢复） | PASS |
| 6 | **一次点击保存评分并更新指标** | PASS |
| 7 | **刷新后进度保留** | PASS |
| 8 | 报告显示真实指标或 N/A | PASS |
| 9 | **报告不伪造 100%** | PASS |
| 10 | 报告列出未完成门禁 | PASS |
| 11 | A/B 如实显示 BLOCKED | PASS |
| 12 | **生产表全部未变** | PASS |
| 13 | 只有评价表被写入 | PASS |
| 14 | 页面无未处理异常 | INFO（含预期的 404/409） |

### UAT 过程中发现并修复的 3 个真实缺陷

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | 评分后 summary 不刷新，第二次点击 409 | 保存后失效 summary 查询；版本从缓存实时读取 |
| 2 | 源不可用的 item 仍可评分 | UI 禁用 + 服务端 409 拒绝 |
| 3 | `AUDIO_CLAIM_WITHOUT_ANALYSIS` 覆盖不足 | （TSB-369 已修，本轮回归确认） |

---

## 九、回归

| 项 | 结果 |
|---|---|
| 后端全量 | **2103+ collected / 0 failed / 6 skipped** |
| 前端 | **175 passed**，typecheck/lint/build 全绿 |

---

## 十、范围声明

**新增**：6 个服务模块、1 个 API 模块（6 个路由组）、4 个前端页面、8 个测试文件。

**修改**：`model_call_ledger.py`（重写）、`director_plan_providers.py`（清理+回执）、
`models.py`（3 张评价表）、`main.py`（注册路由）。

**未改动**：原片、`UserAnnotation`、Storyboard/版本/锁、模型历史证据、
TSB-366/367/368/369/370 的全部修复。

**未做**：真实模型调用（额度已尽且需新授权）、自动渲染、音乐推荐、
把草案写成正式 Storyboard。

---

## 十一、未完成与下一步

| # | 项 | 下一步 |
|---|---|---|
| 1 | **真人未打分** | 需人看 5 个镜头并点击评分 → 才能从 `MANUAL_REVIEW_PENDING` 转为 `FOOTAGE_REVIEW_PASS` |
| 2 | **G4 无真实模型方案** | 需新 grant（≤2 次 `plan_text`），代码已就绪 |
| 3 | **G3 端点曾 503** | 恢复后先文字对照再探视频 |
| 4 | 一次 UAT 点击被记录 | 已在报告标注为链路验证，**不计入人工验收** |

### 建议的下一轮（最低成本）

1. **你亲自打开 `/project/<id>/evaluation/rating` 打 5 个镜头的分** —— 这是把
   `MANUAL_REVIEW_PENDING` 变成真实结论的**唯一**方式，不需要任何授权或费用。
2. 打分后导出报告，即为本轮真正的产出。
3. 若需 A/B：先申请 2 次文字策划额度。

> **本轮交付的是"可用的验收工具"，不是"AI 很准"的结论。**
> 那个结论只有人看过画面之后才存在。
