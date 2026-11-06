# TSB-367-R1 缺陷核实与修复计划

| 字段 | 内容 |
|---|---|
| 基线 | `main@eb63003eb71678704d1ddf7865fd294cacfe6b39`（工作区干净，与 `origin/main` 一致） |
| 核实日期 | 2026-11-06 |

## 一、逐项实测结论（全部属实）

| 编号 | 任务书描述 | 实测证据 | 结论 |
|---|---|---|---|
| **R1-01** | insert/replace 在提案与应用各生成一次 shot_id | `plan_operations.py:296` 与 `:343` 各调 `_new_shot_id()`；提案与应用是两次独立执行 | ✅ 属实 |
| **R1-02** | stale/failed 赋值后 raise，被 `session_scope` 回滚 | `plan_operation_service.py:307/317/329` 先赋值再 `raise`，无独立事务 | ✅ 属实 |
| **R1-03** | 执行器返回 invalid 后服务层未阻断 | 服务层全文无 `validation.state` 判定；`invalid` 快照仍可生成版本 | ✅ 属实 |
| **R1-04** | 已发布的 0001 被事后加入新表 | `0001_storyboard_versions.py:164-177` 含 `legacy_plan_migrations` 建表 | ✅ 属实 |
| **R1-06** | move_shot 不更新 section_id；自身锚点异常 | `_apply_move_shot` 全文无 `section_id` 处理；先 `remove` 后 `_index_of(anchor)` 在自身锚点时抛错 | ✅ 属实 |
| **R1-07** | `all` 后部分解锁会保留 `all` | `current -= {range}` 后仍是 `{"all"}`，未展开为三具体维度 | ✅ 属实 |
| **R1-08** | move_section 与时间线顺序关系未定义 | `_renumber` 只按 shots 数组顺序累加，章节顺序不参与；无任何合同说明或测试 | ✅ 属实 |
| **R1-09** | 无真并发测试 | `test_tsb367_transactions.py` 中无 `Thread`/`Barrier` | ✅ 属实 |
| **R1-05** | 前端只有锁定/解锁第一个镜头 | `Export.tsx` 仅两个 `lock_shot` 按钮，写死 `draft[0]` | ✅ 属实 |

**未发现任务书与实际代码矛盾。**

---

## 二、设计决策（编码前确定）

### 2.1 R1-01：身份在提案阶段确定并持久化

**方案**：提案时执行器**一次性**产出全部新身份，写入 `PlanOperation.operations` 中每个 insert/replace 的
`resolved_shot_id` 字段；应用阶段执行器**复用**该字段，绝不重新生成。

- `resolved_shot_id` 是**服务端内部字段**：写入路径上由服务端注入；客户端提交时该字段被 Schema 拒绝
  （`extra="forbid"` + 显式黑名单），因此不能伪造。
- 应用阶段若发现 `operations` 中缺少 `resolved_shot_id`，**报错而非补救**（防止绕过提案直接 apply）。

### 2.2 R1-02：状态机与持久化

**合同**（明确写入代码与测试）：

| 当前 | 目标 | 允许 |
|---|---|---|
| proposed | applied / rejected / stale / failed | ✅ |
| applied / rejected / stale / failed | 任何 | ❌ |

- 成功路径：版本 + 指针 + `applied` 在**同一事务**提交。
- 失败路径：业务事务回滚后，用**独立事务**写 `stale`/`failed`，并用**条件更新**
  （`WHERE status='proposed'`）避免覆盖已提交的终态。
- 数据库约束覆盖 INSERT **和** UPDATE。

### 2.3 R1-03：最终校验合同

执行器 `_validate_final()` 扩充为 10 项检查（见任务书 R1-03 清单），服务层：
- `invalid` → 拒绝提案（`INVALID_SNAPSHOT`），不建版本；
- `needs_review` → 允许保存，标记写入版本 `validation_summary`；
- 历史版本只读不受影响。

### 2.4 R1-08：章节顺序语义（明确为元数据）

**决定**：**镜头时间线顺序由 shots 数组决定；章节顺序是元数据，不驱动镜头顺序。**
理由：TSB-366 起数组位置即时间线顺序（已有多处依赖）；若让章节顺序驱动镜头，
会改变既有版本的可解释性且与 `timeline_start_sec` 累积规则冲突。

**配套**：新增 `sections_consistent` 校验——若某章节的镜头在 shots 数组中不连续，
标记 `needs_review` 并说明“章节顺序仅为元数据，镜头实际顺序见时间线”，让 UI 不会误判。

### 2.5 R1-04：新增前向 revision

- **恢复** `0001` 的历史语义（移除事后加入的 `legacy_plan_migrations`）；
- 新增 `0003_legacy_plan_migrations`，用 `IF NOT EXISTS` 安全补表（对已执行旧 0001 的库也能升级）；
- 双路径一致性测试比较**列/类型/索引/外键/唯一约束/触发器**，不只比表名。

---

## 三、拟修改文件

### 后端

| 文件 | 改动 |
|---|---|
| `app/services/plan_operations.py` | 身份复用；move_shot section + 自身锚点；`all` 部分解锁；最终校验扩充；章节一致性 |
| `app/services/plan_operations_schema.py` | 拒绝客户端 `resolved_shot_id` |
| `app/services/plan_operation_service.py` | 身份持久化复用；状态独立事务持久化；invalid 阻断；条件更新 |
| `app/database.py` | 状态约束覆盖 INSERT/UPDATE |
| `alembic/versions/0001_*.py` | 恢复历史语义 |
| `alembic/versions/0003_legacy_plan_migrations.py` | 新增前向 revision |

### 前端

`Export.tsx`、`OperationPanel.tsx`、`types.ts`、测试文件。

### 新增测试

`test_tsb367r1_identity.py`、`test_tsb367r1_lifecycle.py`、`test_tsb367r1_validation.py`、
`test_tsb367r1_migration_paths.py`、`test_tsb367r1_semantics.py`、`test_tsb367r1_concurrency.py`；
前端 `Export.operations.test.tsx`。

---

## 四、数据库变更

| 变更 | 类型 | 兼容性 | 回滚 |
|---|---|---|---|
| `operations` JSON 内新增 `resolved_shot_id` | 无 DDL | 旧提案无该字段时拒绝 apply | 无需 |
| 状态约束扩展到 UPDATE | 触发器 | 现有数据均为合法状态 | 降级删触发器 |
| `0003_legacy_plan_migrations` | 纯新增 + IF NOT EXISTS | 已执行旧 0001 的库可安全升级 | 只删该表 |

**不删除、不重建任何既有数据；不改写任何历史版本快照。**

---

## 五、测试计划

先写 RED，逐项确认失败后再修。覆盖任务书 R1-01～R1-09 的"必须增加测试"全部条目，
外加反向验证（去掉身份复用 / 状态持久化 / invalid 阻断 / 并发保护，各自断言变红）。
