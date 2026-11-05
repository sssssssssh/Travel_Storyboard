# TSB-367 开工差距分析（先于编码，任务书 §1 / §14 要求）

| 字段 | 内容 |
|---|---|
| 实际基线 | `main@bca2433f968c3e62522c4c7b8647aa9452172af3` |
| 工作区 | 干净（`git status --short` 无输出） |
| 分支 | `main`，与 `origin/main` 一致 |
| 任务书参考 SHA | `f9c552e`（代码）/ `bca2433`（文档回填）→ **实际 HEAD 即 `bca2433`，与参考一致** |

---

## 一、阶段 0：TSB-366-R1 遗留项复核（逐项实测）

| # | 遗留项 | 实测结果 | 结论 |
|---|---|---|---|
| 1 | `LegacyMigrationPending` 无稳定 HTTP 映射 | `git grep LegacyMigrationPending -- app/api/` → **空** | ❌ **仍未解决** |
| 2 | `migration_status()` / `retry_migration()` 无 HTTP 入口 | `git grep` 两个函数名于 `app/api/` → **空** | ❌ **仍未解决** |
| 3 | `legacy_plan_migrations` 未纳入 Alembic revision | `alembic/versions/0001_*.py` 中无该表 | ❌ **仍未解决** |

三项全部需要本轮补齐。**不重复建设**：服务层函数已存在，只补 HTTP 接线与迁移 revision。

### 建表策略确认（§2.3）

| 事实 | 证据 |
|---|---|
| 运行时建表入口 | `app.database.init_db()`（`create_all` + `ADDITIVE_MIGRATIONS` + 触发器） |
| 测试入口 | `tests/conftest.py` 直接调 `init_db()` |
| Alembic 用途 | **仅运维层**（`requirements-dev.txt` 中的 dev 依赖，`alembic.ini` 注释写明） |
| 结论 | **运行时唯一入口是 `init_db()`**；Alembic 作为可审阅/可回滚的并行描述，需补齐 `legacy_plan_migrations` 与之对齐 |

---

## 二、可复用项（不重复建设）

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 版本快照 / 不可变历史 | `storyboard_service.save_version()` | 操作执行成功后调用它写版本 |
| 基版本并发保护（CAS） | `save_version` 内 `BEGIN IMMEDIATE` + `VersionConflict` | 直接复用 |
| 幂等键（版本级） | `StoryboardVersion.idempotency_key` | 新表 `PlanOperation` 用**独立**幂等键（作用域 = board） |
| 引用校验 | `storyboard_service.validate_references()` | 扩展为统一校验器入口 |
| `invalid` / `needs_review` 区分 | `ValidationSummary` | 沿用 |
| 源引用派生索引 | `StoryboardSourceRef` | 由 `save_version` 同事务写入 |
| 不可变触发器 | `database._STORYBOARD_TRIGGERS` | 沿用，新增操作表的触发器 |
| 迁移状态/重试 | `storyboard_migration.migration_status/retry_migration` | 直接接线到 HTTP |

---

## 三、实际差距（需要新建的）

| 差距 | 说明 | 影响面 |
|---|---|---|
| **G1** 无 `PlanOperation` 表 | 无操作意图/状态/回执的持久化 | 新增 1 表 |
| **G2** 锁定只有单一 `locked: bool\|None` | 任务书 §5.3 需要 `reference/range/position/all` 四维度 | 快照 Schema 扩展（向后兼容） |
| **G3** 无操作信封 Schema | 无严格校验的 operations 数组 | 新增 `plan_operations_schema.py` |
| **G4** 无纯函数执行器 | 无 move/insert/remove/replace/trim/lock/constraints 执行 | 新增 `plan_operations.py` |
| **G5** 无提案/应用/拒绝 API | 无 propose → apply → version 的闭环 | 新增 5 条路由 |
| **G6** 无版本 diff API | §7 要求 `versions/{v}/diff` | 新增 1 条路由 |
| **G7** 前端无操作面板 | §8 要求最小闭环 | `Export.tsx` 增面板 |
| **G8** 阶段 0 三项遗留 | 见上 | API + Alembic |

---

## 四、锁定维度扩展的设计取舍（G2）

**问题**：现有快照 `locked: bool | None`（单值）。

**方案**：改为 `locked: list[str] | None`，取值 `reference|range|position|all`。

**向后兼容**：
- `None` = 未锁定（与现状一致）；
- 迁移写入的旧数据为 `None`，语义不变；
- `locked=True`（布尔）在读取时映射为 `["all"]`（旧数据只可能是人工未用过的新字段，实际无存量）；
- **不改变快照不可变语义**，锁定变化通过新版本表达。

**为什么不用四列布尔**：数组天然表达"多维度同时锁定"，且 `all` 是并集而非独立维度，用四列会出现 `all=true` 与单项冲突的非法组合。

---

## 五、拟修改文件清单

### 后端 - 新增

| 文件 | 用途 |
|---|---|
| `app/services/plan_operations_schema.py` | 操作信封与 8 类操作的严格 Pydantic Schema |
| `app/services/plan_operations.py` | **纯函数**执行器（不碰 DB、不调模型、不写文件） |
| `app/services/plan_validation.py` | 统一校验器（项目归属/区间/签名/锁定矩阵/硬约束） |
| `app/services/plan_operation_service.py` | 事务编排：提案/应用/拒绝/idempotency/CAS |
| `app/api/plan_operations.py` | 7 条路由 |
| `alembic/versions/0002_plan_operations.py` | 运维层 revision |
| `tests/test_tsb367_schema.py` | §9.1 |
| `tests/test_tsb367_executor.py` | §9.2 |
| `tests/test_tsb367_references.py` | §9.3 |
| `tests/test_tsb367_lock_matrix.py` | §9.4 |
| `tests/test_tsb367_transactions.py` | §9.5 + §9.6 |
| `tests/test_tsb367_api.py` | §9.7 |
| `tests/test_tsb367_stage0.py` | 阶段 0 遗留 |

### 后端 - 修改

| 文件 | 改动 |
|---|---|
| `app/models.py` | 新增 `PlanOperation` 表 |
| `app/database.py` | 新增操作表触发器（同 board、applied↔result_version 一致性） |
| `app/services/storyboard_schema.py` | `locked` 改为维度数组 |
| `app/services/storyboard_service.py` | `save_version` 支持传入 PlanOperation 关联 |
| `app/api/storyboard.py` | 迁移状态/重试路由 + `LegacyMigrationPending` 映射 |
| `app/main.py` | 注册新路由 |
| `alembic/versions/0001_storyboard_versions.py` | 补 `legacy_plan_migrations` |

### 前端 - 修改

| 文件 | 改动 |
|---|---|
| `src/pages/Export.tsx` | 最小操作面板（移动/裁剪/移除/锁定 + preview/confirm/reject） |
| `src/lib/api.ts` / `src/lib/types.ts` | 操作信封类型与端点 |
| `src/pages/Export.test.tsx` | 前端交互测试 |

### 文档

`docs/REGRESSION.md`、`docs/RELEASE_TSB_367.md`、`docs/validation/tsb367/README.md`

---

## 六、数据库变更

| 变更 | 类型 | 兼容性 | 备份 | 回滚 |
|---|---|---|---|---|
| 新增 `plan_operations` 表 | 纯新增 | ✅ 向前兼容（`create_all` 自动建） | `init_db` 自动 `.bak` | Alembic `downgrade` 只删本表 |
| `locked` 字段语义扩展（JSON 内） | 无 DDL 变更 | ✅ 旧值 `None` 语义不变 | — | 无需回滚 |
| Alembic 0001 补 `legacy_plan_migrations` | 运维层对齐 | ✅ 幂等（`IF NOT EXISTS`） | — | downgrade 只删表 |

**不删除、不重建任何现有数据**；旧 EditPlan 表保持不动。

---

## 七、测试计划（先 RED 后 GREEN）

| 组 | 覆盖 | 与任务书对应 |
|---|---|---|
| Schema | 缺字段/未知字段/未知版本/未知操作/缺 base/空 operations/超限/NaN/Infinity/伪造服务端字段 | §9.1 |
| 执行器 | 8 类操作 × (合法/对象不存在/跨项目/锁定/边界/重复 ID/组合/中途失败/原快照不变) | §9.2 |
| 引用校验 | 归属/关系/缺失/签名变化/invalid 拒绝/needs_review 保留/历史可读/不自动替换 | §9.3 |
| 锁定矩阵 | 4 维度 × 5 操作 = 20 组合逐项 | §9.4 |
| 事务并发 | 唯一新版本/失败版本数不变/指针不动/同 base 只一个成功/同事务/回滚/无 applied 缺 result | §9.5 |
| 幂等 | 同 key 同 payload/同 key 异 payload/并发同 key/applied 重放/终态不重置 | §9.6 |
| API 权限 | 鉴权/跨项目/board-project 不匹配/operation 越权/apply 重校验/reject 不建版本/错误码 | §9.7 |
| 前端 | preview 展示/cancel 不 apply/confirm 一次/409 保留/成功同步/失败提示 | §9.8 |

**反向验证**：至少对 base_version CAS、幂等键、锁定检查各做一次"删掉保护→测试变红"。

---

## 八、执行顺序

严格按任务书 §12：阶段 0 → 模型/Schema → 执行器 → 校验/锁定 → 事务幂等 → API → 前端 → 全量验证。
**不先做 UI**。
