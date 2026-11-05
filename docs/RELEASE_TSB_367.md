# TSB-367 — 结构化操作协议与引用校验

| 字段 | 内容 |
|---|---|
| 基线 | `main@bca2433f968c3e62522c4c7b8647aa9452172af3`（工作区干净，与 `origin/main` 一致） |
| 产品版本 | `2.1.2`（未升版） |
| 差距分析 | [GAP_ANALYSIS.md](validation/tsb367/GAP_ANALYSIS.md)（编码前输出） |
| 证据目录 | [docs/validation/tsb367/](validation/tsb367/) |
| **交付 SHA** | **`f9585e11ee5fd7abea01d658d281d69ee9f7d533`** |
| CI | run #97，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/41986870243 |

> **结论**：任何 Storyboard 修改现在都必须表达为受 Schema 约束的 `PlanOperation`；
> 服务端按稳定 `shot_id` 执行、校验项目引用与锁定、以单个事务生成新的不可变版本与回执。
> **未接入任何模型**，未开发 TSB-368/369/371。

---

## 一、阶段 0：TSB-366-R1 遗留项收口

开工实测三项遗留**全部仍在**，现已全部解决：

| # | 遗留项 | 修复 | 测试 |
|---|---|---|---|
| 1 | `LegacyMigrationPending` 无 HTTP 映射 | 映射为 **409 `LEGACY_MIGRATION_PENDING`**，响应含可读 `action` 与 `retry_endpoint`，不含堆栈/SQL/路径 | `test_leftover_1_*`（3 项） |
| 2 | 状态/重试无 HTTP 入口 | `GET /projects/{p}/storyboard/migration-status`、`POST .../retry-migration` | `test_leftover_2_*`（6 项） |
| 3 | `legacy_plan_migrations` 未入 Alembic | 补入 `0001` revision（`IF NOT EXISTS` 幂等），downgrade 一并移除 | `test_leftover_3_*`（3 项） |

**建表策略结论**（§2.3）：运行时唯一入口是 `init_db()`（`create_all` + additive migrations + 触发器）；
测试同样走 `init_db()`；Alembic 是**运维层**（dev 依赖）。现在两条路径对 TSB-366/367 的表集合一致，
由 `test_leftover_3_init_db_remains_the_runtime_entry_and_alembic_matches` 守护。

**401 安全要求**：错误体经测试断言**不含** `traceback` / `sqlite` / `.db` / `select ` / 内部异常原文。

---

## 二、PlanOperation 领域模型（§3）

### 2.1 表与约束

`plan_operations`：`id / project_id / board_id / base_version_id / message_id / idempotency_key /
schema_version / operations / diff / status / reason / result_version_id / error_code / error_detail /
actor / payload_digest / created_at / updated_at`

| 约束 | 实现 |
|---|---|
| 幂等键在 board 内唯一 | `UNIQUE(board_id, idempotency_key)` |
| base 版本必须属于同一 board | 触发器 `trg_plan_operation_base_same_board` |
| 状态值受约束 | 触发器 `trg_plan_operation_status_valid`（`proposed/applied/rejected/stale/failed`） |
| applied 必须有 result 且属同 board | 触发器 `trg_plan_operation_applied_has_result` |
| 终态不可改写 | 触发器 `trg_plan_operation_terminal_immutable` |
| **一个结果版本只被一个 applied 操作认领** | 部分唯一索引 `uq_plan_operation_applied_result` |

**职责划分**：列级 → 数据库约束；跨行不变量 → 触发器；业务意图 → 服务层。前端不承担任何校验。

### 2.2 状态机

```
proposed ──apply 校验通过──> applied    （必须有 result_version_id）
proposed ──reject─────────> rejected   （不产生版本）
proposed ──base 已变化────> stale
proposed ──执行错误───────> failed
```

终态不可再次应用；`applied` 重放返回原结果而非新建版本；`rejected` 不可再 apply。

### 2.3 锁定维度扩展

原快照只有 `locked: bool | None`。现改为 `locked: list[str] | None`，取值
`reference | range | position | all`；`None` 表示未锁定（与旧语义一致）；
旧布尔值在读取时映射为 `["all"]` / `None`，**向后兼容**且不改写任何历史快照。

---

## 三、结构化操作协议（§4）

### 3.1 信封严格校验

`OperationEnvelope`（`plan_operations_schema.py`）：

- `schema_version` **必填**，非 `1` 一律拒绝（不再默认填充）；
- 未知信封字段 / 未知操作字段 **一律拒绝**（`extra="forbid"`）；
- 客户端提交 `status` / `diff` / `result_version_id` → **422**（HTTP 层 body 模型同样 `forbid`）；
- NaN / Infinity / 负时长 / 字符串数字 → 拒绝；
- 操作数上限 **50**、`reason` 上限 **2000** 字符；
- 未知操作类型 → `400 UNKNOWN_OPERATION`。

### 3.2 八类操作

| 类型 | 关键规则 |
|---|---|
| `move_shot` | `before_shot_id` / `after_shot_id` **恰好一个**；position 锁禁止；保留 shot_id 与区间 |
| `move_section` | 同样恰好一个锚点；不改变章节内镜头身份 |
| `insert_shot` | shot_id **服务端生成**；Asset/Segment 必须属本项目且相互匹配；区间落在 Segment 内 |
| `remove_shot` | 只从快照移除；不删除任何素材；锁定则拒绝 |
| `replace_shot` | **新身份**（不复用旧 shot_id）；位置保持；reference 锁禁止 |
| `trim_shot` | 秒数为权威；入点<出点；区间在 Segment 内；不隐式变速；range 锁禁止 |
| `update_constraints` | 硬约束冲突**报告**不静默取舍；从不增删镜头 |
| `lock_shot` | 维度白名单；`all` 为并集规范形式；锁定变化进 diff |

### 3.3 批量语义（§4.4）

操作按序作用于内存副本 → 任一失败整批失败 → 结束后对**最终快照**再完整校验。
原子性是**结构性**的：执行器不接触数据库，没有地方存放半个结果。

---

## 四、统一引用校验与锁定矩阵（§5）

### 4.1 锁定矩阵（逐格测试）

| 维度 | move | trim | replace | remove | update_constraints |
|---|:--:|:--:|:--:|:--:|:--:|
| reference | ✓ | ✓ | ✗ | ✗ | ✓ |
| range | ✓ | ✗ | ✗ | ✗ | ✓ |
| position | ✗ | ✓ | ✓（保持位置） | ✗ | ✓ |
| all | ✗ | ✗ | ✗ | ✗ | ✓ |

测试 `test_tsb367_lock_matrix.py` 把上表**转写为数据**，4×5=20 格逐格断言；
另有一个元测试比对 `LOCK_MATRIX` 与公布表，两者不一致即失败。

### 4.2 `invalid` 与 `needs_review`（§5.2）

| 状态 | 新操作 | 历史读取 |
|---|---|---|
| `invalid` | 拒绝（`reject_invalid_references` 或结构错误） | 仍可读 |
| `needs_review` | 保留并标记 | 仍可读 |

**不自动替换**任何 Asset/Segment/证据 ID；跨项目引用**永远**不被视为有效。

---

## 五、事务、并发与幂等（§6）

### 5.1 事务顺序（§6.2）

读 board → 取写锁（`BEGIN IMMEDIATE`）→ 查幂等键 → 校验 base 仍为当前 → 读不可变基快照 →
解析项目媒体 → 执行整批 → 校验最终快照 → **同事务**写版本 → 写 SourceRef → 标记 applied →
推进 `current_version_id` → 提交。

任一步失败：整事务回滚，**无版本、无 ref、无 applied、指针不动**。

### 5.2 并发（§6.3）

- 每个操作绑定 `base_version_id`；
- 事务内重读当前版本，不一致 → **409 `PLAN_VERSION_CONFLICT`**；
- 过期提案标记 `stale`，**不自动 rebase**；
- 同 base 两个客户端最多一个成功（测试 `test_two_clients_on_the_same_base_only_one_applies`）。

### 5.3 幂等（§6.4）

| 场景 | 行为 |
|---|---|
| 同 key 同 payload | 返回**原操作**，不新建版本 |
| 同 key 异 payload | **409 `IDEMPOTENCY_KEY_REUSED`** |
| applied 重放 | 返回原结果 |
| 并发重复 | 数据库唯一约束兜底 |

---

## 六、API 契约（§7）

| 方法 | 路由 |
|---|---|
| POST | `/api/projects/{p}/storyboards/{b}/operations` |
| GET | `…/operations`（列表）、`…/operations/{o}`（详情） |
| POST | `…/operations/{o}/apply` |
| POST | `…/operations/{o}/reject` |
| GET | `/api/projects/{p}/storyboards/{b}/versions/{v}/diff` |

**错误码**（实测）：`400 UNKNOWN_OPERATION` / `400 INVALID_OPERATION_SCHEMA` /
`404 SHOT_NOT_FOUND` / `404 OPERATION_NOT_FOUND` / `409 PLAN_VERSION_CONFLICT` /
`409 LOCK_CONFLICT` / `409 IDEMPOTENCY_KEY_REUSED` / `409 OPERATION_ALREADY_FINAL` /
`422 INVALID_RANGE` / `422 INVALID_REFERENCE` / `422` 伪造字段。

**隔离**：`board_id` 属其他项目 → 404；`operation_id` 属其他 board → 404；
未知 project → 404。**ID 不可被探测**。

---

## 七、前端最小接线（§8）

新增 `frontend/src/components/OperationPanel.tsx`，接在 Export 页：

- 展示当前版本号与待提交操作数；
- 「预览变更」调用 propose，展示**服务端计算的 diff**（本地不计算）；
- 「确认应用」提交 apply，成功后刷新版本/历史/镜头查询；
- 「拒绝」调用 reject，不产生版本；
- **409 时保留待提交操作**，显示冲突说明，不覆盖服务端版本；
- 失败明确提示，绝不显示成成功。

---

## 八、验证结果

### 8.1 专项（新增 148 项）

| 套件 | 项数 | 内容 |
|---|---:|---|
| `test_tsb367_stage0.py` | 13 | 三项遗留收口 |
| `test_tsb367_model.py` | 13 | 表、约束、触发器、状态机、锁定维度 |
| `test_tsb367_schema.py` | 32 | 信封与八类操作严格校验 |
| `test_tsb367_executor.py` | 31 | 纯函数执行器 |
| `test_tsb367_lock_matrix.py` | 25 | 20 格矩阵 + 元测试 + 组合锁 |
| `test_tsb367_transactions.py` | 16 | 事务/并发/幂等/拒绝/跨项目 |
| `test_tsb367_api.py` | 19 | HTTP 契约、错误码、权限隔离、diff |
| **合计** | **148** | |

**RED 证据**：修复前 `test_tsb367_stage0.py` **10 failed**（原始输出见 `RED-stage0.local.txt`）。

### 8.2 反向验证（三项均成立）

| 破坏项 | 结果 |
|---|---|
| 去掉 apply 时的 base_version CAS | `test_two_clients_on_the_same_base_only_one_applies` **变红** |
| 去掉 `_check_lock` | 锁定矩阵多项**变红**（含 `remove_shot-*` 三格与组合锁） |
| 绕过幂等键检查 | `test_replaying_the_same_key...` 与 `...different_body_is_refused` **双红** |

恢复后全套 148 项重新全绿，`git grep REVERSE-VERIFICATION` 无残留。

### 8.3 前端

| 检查 | 结果 |
|---|---|
| `pnpm run test` | **112 passed / 13 files**（新增 7 项 OperationPanel） |
| `pnpm run typecheck` | ✅ |
| `pnpm run lint` | ✅ |
| `pnpm run build` | ✅ |

### 8.4 后端完整测试

```
tests=1603 failures=1 errors=0 skipped=5
```

| 项 | 值 |
|---|---|
| collected | **1603**（较 TSB-366-R1 的 1455 增加 148） |
| passed | 1602 |
| failed | **1** |
| skipped | 5（TSB-361 记录的平台事实 `link creation`） |

**唯一失败**：`tests.test_vision_provider::test_status_classification[408-MODEL_TIMEOUT]`
—— 本机既存问题。本轮**未修改 Vision Provider / errors.py**，故**不声称本机全量通过**。

**本轮自己引入并已修复的两项回归**（诚实记录）：

| 回归 | 原因 | 修复 |
|---|---|---|
| `test_tsb366_schema_ops::test_the_alembic_revision_declares_the_same_triggers_as_the_app` | 新增 4 个 PlanOperation 触发器，Alembic 侧未同步 | 新增 `0002_plan_operations` revision；漂移测试扩展为扫描**全部** revision 的两组触发器定义 |
| `test_tsb367_transactions::test_two_clients_on_the_same_base_only_one_applies` | 全量运行下偶发：断言只报"未抛冲突"，无法定位是哪条不变量破了 | 补显式前置断言（同 base、base==v2、apply 后指针确已推进、base 确已过期）；修复后全量稳定通过 |

两项均在全量中发现、修复后再次全量验证，**未通过放宽断言或跳测试解决**。

---

## 九、数据安全（§10）

| 底线 | 是否满足 |
|---|---|
| 不删除 Asset/Segment/Frame/原片 | ✅ `remove_shot` 只改快照；跨项目/素材表无写操作 |
| 不修改历史版本快照 | ✅ 不可变触发器仍生效 |
| 不跨项目引用 | ✅ 执行器只接受本项目 `ProjectContext` |
| 不自动修复失效 ID | ✅ 校验只拒绝或标记 |
| 不通过放宽校验/跳测试变绿 | ✅ 未删除任何既有断言，未新增 skip |
| 不把模型输出直接写库 | ✅ 本轮无任何模型调用 |
| 不允许过期提案自动 rebase | ✅ stale 明确返回，需用户基于新版本重建 |
| downgrade 不删除旧 EditPlan | ✅ `test_leftover_3_downgrade_never_drops_the_legacy_plan_table` 守护 |

### 数据库变更与回滚

| 变更 | 兼容性 | 备份 | 回滚 |
|---|---|---|---|
| 新增 `plan_operations` 表 + 4 触发器 + 1 部分唯一索引 | 纯新增，`create_all` 自动建 | `init_db` 自动 `.bak` | Alembic 或删表；不影响既有数据 |
| `locked` 字段语义扩展（JSON 内） | 旧值 `None` 语义不变 | — | 无需回滚 |

---

## 十、未完成项与风险

1. **本机 408 分类失败继续存在**（`test_vision_provider::test_status_classification[408-MODEL_TIMEOUT]`）：
   本轮**未修改 Vision Provider**，因此**不声称本机全量通过**。见 §8.4 的实际数字。
2. **真实素材/模型验收未做**：所有操作测试基于合成数据与隔离库；没有用真实旅行素材跑过完整操作闭环。
3. **前端未做真实浏览器手工验收**：只有 jsdom 自动化测试。
4. **`evidence_refs` 尚未落库**：Schema 接受该字段，但本轮未建立证据引用表，字段仅随操作记录保存。
5. **`message_id` 预留未用**：为 TSB-371 预留，当前恒为 `None`。
6. **`update_constraints` 的软偏好不好影响任何后续行为**：本轮只记录到 brief.notes，TSB-368/369 才会消费。
7. **diff 端点仅做结构比较**：不含语义解释（那属于 TSB-370/371）。
8. **未提供操作列表的分页**：`limit` 上限 50，长期使用需分页。


---

## 十一、CI 执行结果

| 项 | 值 |
|---|---|
| 交付 SHA | `f9585e11ee5fd7abea01d658d281d69ee9f7d533` |
| run | **#97** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/41986870243 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Required FFmpeg (windows-latest)` | ✅ success |
| `Required FFmpeg (ubuntu-latest)` | ✅ success |
| `Backend (Python 3.11)` ×2 | ✅ success |
| `Backend (Python 3.13)` ×2 | ✅ success |
| `Frontend` | ✅ success |

**关于 408 用例的环境差异（与 TSB-366-R1 相同的现象）**：该用例在本机稳定失败，
在 CI 中通过。本轮**没有修改该模块**，且 CI 全绿，因此可以确认它不是本轮引入的回归；
但它仍是一个**环境相关的真实不稳定**，保留为遗留项，不据此声称"本机全量通过"。

## 十二、完成判定（对照任务书 §8）

| 条件 | 状态 |
|---|---|
| P0-01 历史版本内容错配 | ✅（TSB-366-R1 已修，本轮回归保持） |
| P0-02 shot_id 稳定性 | ✅（同上，操作协议复用稳定身份） |
| P1-03 草稿保护 | ✅（同上） |
| P1-04 迁移失败安全保护 | ✅（阶段 0 补上 HTTP 映射与重试） |
| P1-05 迁移对账固定 v1 | ✅（同上） |
| 写入校验边界有结论和测试 | ✅ §4.2 + `test_p1_*` |
| 新增专项测试全部通过 | ✅ 148 项 |
| 前端质量检查全部通过 | ✅ typecheck/lint/test(112)/build |
| 后端全量真实记录 | ✅ 1603 / 1 failed（既存）/ 5 skipped |
| 数据库兼容性验证 | ✅ Alembic 0002 隔离库升降级，旧数据保留 |
| 代码已提交并推送 | ✅ `f9585e1` |
| GitHub Actions 结果已记录 | ✅ run #97 7/7 |
