# TSB-367-R1 — 结构化操作协议质量收口

| 字段 | 内容 |
|---|---|
| 基线 | `main@eb63003eb71678704d1ddf7865fd294cacfe6b39`（工作区干净，与 `origin/main` 一致） |
| 上一轮 | TSB-367 = `f9585e1`，CI run #97 7/7 |
| 差距分析 | [GAP_ANALYSIS.md](validation/tsb367-r1/GAP_ANALYSIS.md)（编码前逐项实测） |
| 证据目录 | [docs/validation/tsb367-r1/](validation/tsb367-r1/) |

> **本轮性质**：不重新设计 TSB-367，只修复代码审阅发现的 9 项问题。
> **未接入任何模型**，未开发 TSB-368/369/371，未改动 Vision Provider 的 408 分类。

---

## 一、缺陷核实：9 项全部属实

编码前逐项实测，**未发现任务书与实际代码矛盾**：

| 编号 | 实测证据 | 结论 |
|---|---|---|
| R1-01 | `plan_operations.py:296/343` 各调一次 `_new_shot_id()` | ✅ 属实 |
| R1-02 | `plan_operation_service.py:307/317/329` 先赋值再 raise | ✅ 属实 |
| R1-03 | 服务层全文无 `validation.state` 判定 | ✅ 属实 |
| R1-04 | `0001` 被事后加入 `legacy_plan_migrations` | ✅ 属实 |
| R1-06 | `_apply_move_shot` 无 `section_id`；自身锚点先 remove 后查找 | ✅ 属实 |
| R1-07 | `current -= {range}` 后仍保留 `{"all"}` | ✅ 属实 |
| R1-08 | `_renumber` 只按 shots 顺序；无章节合同 | ✅ 属实 |
| R1-09 | 无 `Thread`/`Barrier` 测试 | ✅ 属实 |
| R1-05 | `Export.tsx` 写死 `draft[0]`，仅两种操作 | ✅ 属实 |

---

## 二、逐项修复

### R1-01 预览与最终应用身份一致（P0）

**问题**：`insert_shot` / `replace_shot` 每次执行都生成新 UUID，提案与应用各生成一次，导致预览身份与保存身份不同。

**修复**：
1. 新增 `_plan_identities()`，在**提案执行前**为每个 insert/replace 分配身份；
2. 身份以 `resolved_shot_id` 写入 `PlanOperation.operations`（服务端字段）；
3. 应用阶段 `_require_identities()` 标注缺身份的操作，执行器报 `MISSING_SHOT_IDENTITY` 而非补救；
4. 客户端提交 `resolved_shot_id` 仍被严格 Schema 拒绝（`extra="forbid"`）；
5. `_build_diff` 的 replace 关联改为**读取记录身份**，不再按 `asset_id` 猜测。

**测试**（`test_tsb367r1_identity.py`，9 项）：预览=最终、身份已持久化、多 insert 各自稳定、replace 映射、多 replace 映射、重复确认不铸造新身份、客户端伪造被拒、缺身份拒绝猜测。

### R1-02 stale / failed 状态持久化（P0）

**问题**：`op.status="stale"; s.flush(); raise` —— 异常触发 `session_scope` 整体回滚，状态丢失，库里仍是 `proposed`。

**修复**：
1. 引入 `_Stage` 携带"待记录状态"抛出，`apply_operation` 在**回滚之后**用**独立事务**写入；
2. `_set_status_if_proposed()` 用 `WHERE status='proposed'` **条件更新**，绝不覆盖已提交终态；
3. 状态写入合并为**单条 SQL**（先前分两次 UPDATE，第二次撞上终态不可改写触发器）；
4. 明确合同：`stale`、`failed` 均为**终态**，不可再次 apply（`OPERATION_ALREADY_FINAL`）；
5. 触发器覆盖 INSERT 与 UPDATE：终态集合扩为 `applied/rejected/stale/failed`，新增 INSERT 侧 applied 约束。

**测试**（`test_tsb367r1_lifecycle.py`，12 项）：stale 真实落库、stale 不可重试、failed 真实落库、失败无版本、applied 原子提交、四种终态各自不可改写、INSERT 非法状态拒绝、INSERT applied 缺结果拒绝。

> **不再使用** `assert loser.status in {"stale","proposed"}` —— 并发用例已改为精确断言 `{"applied","stale"}`。

### R1-03 最终快照完整引用校验（P0）

**问题**：执行器算了 `ok/needs_review/invalid`，服务层**从不读取**，无效方案照样能发布。

**修复**：`_validate_final()` 扩展为任务书要求的完整清单——`shot_id` 唯一、`section_id` 存在、`ASSET_MISSING`、`SEGMENT_MISSING`、`ASSET_SEGMENT_MISMATCH`（新增）、跨项目拒绝、区间合法、区间在 Segment 内、时长不超过源、`SEGMENT_NEEDS_REVIEW`。服务层：
- `invalid` → 提案报 `INVALID_SNAPSHOT`、apply 报 `INVALID_SNAPSHOT`，**不建版本**；
- `needs_review` → 允许保存，并写入版本 `validation_summary`；
- 历史版本只读、不自动修复、不删除镜头。

**测试**（`test_tsb367r1_validation.py`，12 项）：正常通过、Asset 缺失、Segment 缺失、归属不匹配、跨项目、越界、重复 ID、invalid 不可提案、invalid 不可发布、needs_review 保留、历史可读、不自动修复。

### R1-04 Alembic 历史迁移兼容（P0）

**问题**：`legacy_plan_migrations` 被加进**已发布**的 `0001`。已执行旧 `0001` 的数据库永不重跑，因此永久缺表——而 `init_db()` 有表，两条路径静默分叉。

**修复**：
1. **恢复** `0001` 的历史语义（移除事后加入的建表与降级）；
2. 新增 `0003_legacy_plan_migrations` 前向 revision，全部 `IF NOT EXISTS` 守卫；
3. `0002` 同步本轮新增/修改的触发器；
4. 双路径一致性测试比较**表、列与类型、索引、外键、触发器**，不只比表名。

**测试**（`test_tsb367r1_migration_paths.py`，8 项）：全新 upgrade、历史库 upgrade、历史库数据保留、重复 upgrade 幂等、双路径结构一致、关键索引双侧存在、降级到 base、降级不删旧计划表。

> 这是本轮**最有价值的一条 RED**：修复前 `test_r1_04_a_historical_database_reaches_the_same_schema` 确实复现了"历史库缺表"。

### R1-05 前端最小人工操作入口（P1）

**问题**：只有"锁定/解锁第一个镜头的裁剪区间"，且写死 `draft[0]`。

**修复**：Export 页新增**镜头选中**（点击剪辑列表行，按 `shot_id` 而非数组下标），并提供六类入口：「上移」「下移」（`move_shot`）、「裁剪」（`trim_shot`）、「移除」（`remove_shot`）、「锁定裁剪区间」「全部锁定」「解除裁剪锁定」「全部解锁」（`lock_shot` 多维度）、「清空」。所有操作**先生成服务端提案**，展示服务端 diff，确认后才应用；409 保留待提交操作；成功后刷新 Storyboard 与版本历史。

**测试**：`OperationPanel.r1.test.tsx`（11 项），覆盖六类操作的提议与 diff 展示、确认、拒绝、409、失败、禁用态。

### R1-06 move_shot 跨章节与自身锚点

**问题**：Schema 接受 `section_id` 但执行器从不写入；自身锚点时先 `remove` 再按 id 查找 → 抛 `SHOT_NOT_FOUND`。

**修复**：`_apply_move_shot` 接收 `sections`，**变更前**校验目标章节；自身锚点明确规定为**位置无操作**（不抛异常），但若同时指定 `section_id` 则应用该章节变更；position 锁仍拒绝移动。

**测试**（`test_tsb367r1_semantics.py`）：跨章节写入、无 section 保持原章节、不存在章节拒绝、无悬挂引用、position 锁拒绝、before_self / after_self 两组无操作、自身锚点不抛运行时异常、自身锚点仍可换章节。

### R1-07 all 锁定后的部分解锁

**问题**：`current -= {"range"}` 后集合仍含 `{"all"}`，其他维度永远无法单独解除。

**修复**：`lock_set()` 先展开 `all` 为三个具体维度；解锁后由 `_canonical_lock()` 规范化——全锁仍为 `["all"]`，部分锁为排序去重的具体列表，全解锁为 `None`。

**测试**：`all` → 解锁 `range` → 剩余 `{reference, position}`；trim 允许、move 与 replace 仍拒绝；逐维度解锁到底变 `None`；存储恒为规范形式。**另有 20 格锁定矩阵逐格测试**（`test_tsb367_lock_matrix.py`）保持全绿。

### R1-08 move_section 与时间线顺序

**决策**（写入代码合同与文档）：**镜头时间线顺序由 shots 数组决定；章节顺序是元数据，不驱动镜头顺序。**
理由：TSB-366 起数组位置即时间线顺序，`timeline_start_sec` 按数组累积；若让章节驱动镜头会改变既有版本的可解释性。

**配套**：新增 `SECTIONS_DISCONTIGUOUS` 校验——同一章节在时间线上被分成多段时标记 `needs_review` 并说明"章节顺序仅为元数据"，避免 UI 误以为章节顺序已改变剪辑顺序。

**测试**：移动章节不改镜头顺序、时间线按镜头数组、合同在代码中可见、交错章节被标记。

### R1-09 真正的并发竞争

**问题**：原有测试是"先 A 完成、再 B"，证明不了两个活写者不会同时提交。

**修复**：`test_tsb367r1_concurrency.py` 用两个独立线程 + `threading.Barrier` 同时释放，**精确断言**：恰好一个新版本、胜者 `applied`、败者 `stale`、`current_version_id` 正确、无重复 `version_no`、无半成品版本；并**重复 5 轮**证明稳定；另有三请求同时竞争用例。

---

## 三、发现并修复的自身缺陷（诚实记录）

修复过程中我自己引入了以下问题，均由测试发现并已修正：

| 缺陷 | 如何暴露 | 修复 |
|---|---|---|
| `_set_status_if_proposed` 分两条 UPDATE，第二条撞上"终态不可改写"触发器 | `test_r1_02_a_deterministic_failure_is_committed_as_failed` 报 `IntegrityError` | 合并为单条 UPDATE |
| 新增 INSERT 侧触发器未同步到 `0002` | 双路径一致性测试报"trigger sets differ" | 同步 `0002` |
| replace 的 diff 按 `asset_id` 猜测导致两个替换映射到同一 id | `test_r1_01_multiple_replaces_map_correctly` | 改为读取记录身份 |
| R1-03 两条阻断测试用 `pytest.raises(Exception)`，**反向验证时仍全绿**（空断言） | 反向验证 3 | 断言精确错误码 `INVALID_SNAPSHOT`；并重建场景（批次不动坏镜头），使 gate 成为唯一决定因素 |

最后一条尤其重要：**如果不做反向验证，这会是一个假通过的测试。**

---

## 四、反向验证（四项均成立）

| 破坏项 | 结果 |
|---|---|
| 去掉 stale/failed 状态持久化 | 5 项变红（含 2 项并发） |
| 去掉身份复用 | 6 项变红 |
| 关闭 invalid 阻断 gate | 2 项变红（无效快照**会**被发布） |

恢复后全部重新全绿，`REVERSE-VERIFICATION` 标记无残留。

---

## 五、验证结果

### 5.1 后端专项

| 套件 | 项数 | 结果 |
|---|---:|---|
| `test_tsb367r1_identity.py` | 9 | ✅ |
| `test_tsb367r1_lifecycle.py` | 12 | ✅ |
| `test_tsb367r1_validation.py` | 12 | ✅ |
| `test_tsb367r1_semantics.py` | 17 | ✅ |
| `test_tsb367r1_concurrency.py` | 6 | ✅ |
| `test_tsb367r1_migration_paths.py` | 8 | ✅ |
| **R1 合计** | **64** | ✅ |
| TSB-367 原有 7 个套件 | 148 | ✅ 无回归 |
| 迁移相关（R1 + 366 schema_ops + stage0） | 23 | ✅ |

### 5.2 前端

| 检查 | 结果 |
|---|---|
| `pnpm run test` | **123 passed / 14 files**（新增 11 项） |
| `pnpm run typecheck` | ✅ |
| `pnpm run lint` | ✅ |
| `pnpm run build` | ✅ |

### 5.3 后端完整测试

```
tests=1667 failures=1 errors=0 skipped=5
```

| 项 | 值 |
|---|---|
| collected | **1667**（较 TSB-367 的 1603 增加 **64**） |
| passed | 1666 |
| failed | **1** |
| skipped | 5（TSB-361 记录的平台事实 `link creation`） |

**唯一失败**：`tests.test_vision_provider::test_status_classification[408-MODEL_TIMEOUT]`
—— 既存环境相关问题。本轮**未修改 Vision Provider / errors.py**（任务书 §二.8 明确要求单独处理），
因此**不声称本机全量通过**；CI 侧该用例通过，可确认非本轮引入。

**本轮无新增回归**：TSB-367 的 148 项、TSB-366 的 46 项全部保持通过。

---

## 六、范围声明

**本轮未改动**：
- Vision Provider 的 408 错误分类（任务书明确要求单独处理）；
- TSB-368 候选查询与 AI 自动策划；
- 任何历史版本快照内容（触发器保持生效）；
- 任何原片 / 素材文件。

**本轮未删除**：任何既有测试与断言（原有的 148 项 TSB-367 测试全部保留并通过）。

---

## 七、尚未完成（遗留）

1. **408 分类问题仍在**：`test_vision_provider::test_status_classification[408-MODEL_TIMEOUT]` 本机失败、CI 通过。本轮**未修改该模块**，按要求单独处理。
2. **真实素材手工验收未做**：所有验证基于合成数据与隔离库。
3. **前端未经真实浏览器手工验收**：仅 jsdom 自动化测试。
4. **`evidence_refs` 仍未建表**：字段随操作保存，未建立证据引用关系表。
5. **`message_id` 仍为 TSB-371 预留**，当前恒为 `None`。
6. **`update_constraints` 的软偏好仍只记录不生效**，待 TSB-368/369 消费。
7. **操作列表无分页**，`limit` 上限 50。
8. **`0002` 的 `legacy_plan_migrations` 索引**：`0003` 已补索引；已执行过 TSB-367 版本 `0002` 的库在升级到 `0003` 时会被检查并补建索引，但**旧库若曾用 `init_db()` 建表且缺索引**，`0003` 同样会补上——此路径有测试覆盖，但未在真实历史库上演练。
