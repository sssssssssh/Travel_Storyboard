# TSB-366 — 方案版本与兼容迁移

| 字段 | 内容 |
|---|---|
| 任务编号 | TSB-366（P0，方案版本底座） |
| 基线 | `main@b03b216f900d480fe37b32ede96aae105847f825`（TSB-365 审计提交，工作区干净） |
| 产品版本 | `2.1.2`（未升版） |
| 证据目录 | [docs/validation/tsb366/](validation/tsb366/) |

> 一句话结论：**项目级可全量替换的 EditPlan 已迁移为有身份、可持久读取、历史不可覆盖的
> Storyboard / StoryboardVersion 结构；旧计划无损导入 v1；所有写入不再能绕过版本控制。**

---

## 一、问题（改前状态）

`EditPlanShot` 是一张项目级平面表，`build_edit_plan()` **每次保存先 `DELETE` 全部行再重插**：

```sql
DELETE FROM edit_plan_shots WHERE project_id = ?
```

后果：

1. **没有任何历史**——保存一次，上一次的方案就彻底消失，无法比较、无法回退；
2. **没有身份**——镜头只有数组位置，无法稳定引用；
3. **没有并发保护**——两个客户端同时保存，后到的静默覆盖先到的；
4. **没有权威边界**——任何调用方都能无保护地全量替换。

## 二、目标结构与实现

### 2.1 三张表（`backend/app/models.py`）

| 表 | 作用 | 关键约束 |
|---|---|---|
| `storyboards` | 每个项目的方案头，持有 `current_version_id` 指针 | `UNIQUE(project_id)` → **每项目最多一个主方案** |
| `storyboard_versions` | 每次保存一条不可变快照 | `UNIQUE(board_id, version_no)`；`UNIQUE(board_id, idempotency_key)` |
| `storyboard_source_refs` | 版本到 Asset/Segment 的**派生索引** | `UNIQUE(version_id, shot_id)`；单向 FK，**不级联删除素材** |

### 2.2 跨行约束用 SQLite 触发器强制

SQLAlchemy 无法表达"父版本必须同 board"这类规则，而这些是任务书要求的**保证**。因此
`database._create_storyboard_triggers()` 在启动时安装 4 个触发器：

| 触发器 | 保证 |
|---|---|
| `..._parent_same_board` | `parent_version_id` 必须属于同一 board |
| `..._current_version_same_board` | `current_version_id` 必须指向本 board 的版本 |
| `..._version_immutable` | 快照列（shots/sections/brief/constraints/version_no…）**UPDATE 一律 ABORT** |
| `..._restore_target_same_board` | 恢复目标必须同 board |

> 这比只在服务层校验更强：**任何写入者**（包括未来的直接 SQL）都绕不过去。

### 2.3 快照 Schema（`backend/app/services/storyboard_schema.py`）

Pydantic 模型，`extra="forbid"`：

- `schema_version` **必填**——没有版本的快照无法日后迁移；
- **未知字段拒绝**，但显式放行旧客户端的 `order` 并在转换时丢弃（顺序以数组位置为准）；
- 每个镜头必须有 `shot_id`——**数组下标不是身份**；
- 秒数保持 `float`，**不提前取整**；
- `source_out_sec > source_in_sec` 在结构层就拒绝。

## 三、旧计划迁移

`backend/app/services/storyboard_migration.py`：

| 原则 | 实现 |
|---|---|
| 每项目最多一个 board | `UNIQUE(project_id)` |
| 非空计划 → v1，`reason=legacy_import` | ✅ |
| 空计划 → 空版本，**不虚构镜头** | ✅ |
| 保留数组顺序与全部旧字段 | ✅ `_shot_snapshot()` 逐字段复制 |
| 不编造理由/章节/置信度 | brief 与 constraints 留空；只加**一个** `kind="legacy_import"` 的章节 |
| 旧表保留 | `edit_plan_shots` **不删、不改** |
| 幂等 | 已有 board 即 skip |
| 逐项目原子 | 每项目独立 `session_scope` + `BEGIN IMMEDIATE` |
| 异常不静默修复 | 孤儿 segment / asset 与 segment 不匹配 / 越界 → `validation_summary.state = invalid`，**快照仍可读** |

**迁移报告**（§3.2 要求）：`projects / migrated / empty / invalid / skipped / failed`。

**对账**（§3.3）：`reconcile_project()` 逐字段比对旧行与 v1 快照，返回差异列表。

## 四、版本 API

| 方法与路由 | 说明 |
|---|---|
| `GET /api/projects/{p}/storyboard` | 当前版本（无 board 时创建空 v1） |
| `GET /api/projects/{p}/storyboard/versions` | 历史，**新→旧** |
| `GET /api/projects/{p}/storyboard/versions/{v}` | 单个不可变快照 |
| `GET /api/projects/{p}/storyboard/versions/{v}/source-refs` | 派生索引 |
| `POST /api/projects/{p}/storyboard/versions` | 保存新版本 |
| `POST /api/projects/{p}/storyboard/restore` | 恢复 → **生成新版本** |

**错误语义**（稳定、可分支）：

| HTTP | code | 含义 |
|---|---|---|
| 404 | `STORYBOARD_NOT_FOUND` | 不存在，**或属于其他项目**（不可探测） |
| 409 | `PLAN_VERSION_CONFLICT` | `base_version_id` 过期；响应带当前版本；**未写入** |
| 428 | `BASE_VERSION_REQUIRED` | 写入缺 `base_version_id`（**兼容门禁**） |
| 422 | `INVALID_SNAPSHOT` | 快照不合法；**未写入** |

**并发规则**：基版本比对、`version_no` 分配、INSERT、指针更新全在同一个
`BEGIN IMMEDIATE` 事务内 → 两个客户端同基版本**只能一个成功**。
同一 `idempotency_key` 重试**返回同一版本**，不产生 v+2。

## 五、旧 EditPlan 兼容策略（§5.1 / §5.4）

**唯一权威**：`StoryboardVersion`。`EditPlanShot` 降级为迁移来源与兼容投影。

| 入口 | 现状 |
|---|---|
| `POST /edit-plan` | **保留路径与请求体**，新增**必填** `base_version_id`。缺失 → **428** |
| `GET /edit-plan` | 经适配器读**当前版本**，响应结构不变 |
| `export/json` / `csv` / `otio` | 全部经 `load_export_context()` 读**同一 version_id** |
| `roughcut` | 改为读**版本快照**（`PlannedShot` 适配器） |
| `story.py` 的 `plan_shot_ids` | 改为读版本快照 |
| `build_edit_plan()` | 保留为**薄适配器**，内部走 `save_version()`，**必须带基版本** |

**是否还有绕过版本控制的旧写入口？** 没有：
- `_validate_shot` / `_load_plan` / `_shot_dict` 等旧实现已删除；
- 全仓 `grep` 后 `EditPlanShot` 仅剩：`models.py`（迁移来源）、`analyzer.py`（判断是否被引用，只读）、`projects.py`（计数，只读）；
- 测试 `test_no_legacy_write_path_bypasses_the_version_gate` 断言适配器必须含
  `base_version_id` 且不得直写旧表。

## 六、前端

`frontend/src/pages/Export.tsx`：

- 加载后显示**当前版本号**（`当前 vN`）；
- 「版本历史」面板：列出全部版本（版本号/reason/actor/时间/镜头数/校验状态）；
- 非当前版本提供「恢复此版本」→ 调用 restore API → 切换为新版本；
- 保存提交 `base_version_id`；
- **409/428 冲突时草稿保留**，横幅提示，仅显式「收录服务端版本（放弃草稿）」才丢弃；
- 草稿与已保存版本区分：`draft-dirty` 提示明确写「尚未写入任何版本」。

## 七、Alembic 运维层（与运行时路径的关系）

**重要事实**：本仓库**没有**运行时 Alembic —— 建表由 `app.database.init_db()`
（`create_all` + `ADDITIVE_MIGRATIONS` + 触发器）完成，测试与生产都走这条路径。

按用户决定，本轮**额外**提供可审阅、可回滚的 Alembic 运维层：

```
backend/alembic.ini
backend/alembic/env.py            # URL 从 TRAVELSB_DATA_DIR 解析，仓库不存路径
backend/alembic/versions/0001_storyboard_versions.py
```

**只创建 TSB-366 三张表 + 触发器；不触碰 `edit_plan_shots`。**
`downgrade` 只删 storyboard 表，**旧计划数据完好保留**。

操作步骤（写在 `docs/validation/tsb366/MIGRATION_GUIDE.md`）：

```powershell
cd backend
$env:TRAVELSB_DATA_DIR = "<隔离目录>"
python -m alembic stamp 0001_storyboard_versions   # 库已由 init_db 建好时
python -m alembic downgrade base
python -m alembic upgrade head
```

## 八、验证结果

| 项目 | 实际结果 |
|---|---|
| 后端严格全量（`TSB_REQUIRE_FFMPEG=1`） | **1428 collected / 1422 passed / 1 failed / 5 skipped / 0 errors** |
| 唯一失败 | `test_vision_provider::test_status_classification[408-MODEL_TIMEOUT]` — **基线既有**，已在纯净基线复现，与本轮无关 |
| TSB-366 新增测试 | model 14 + migration 10 + versions 19 + schema_ops 3 = **46** |
| 前端 typecheck / lint / test / build | 四项通过，**101 passed**（新增 3 个版本 UI 用例） |
| Alembic 隔离库 upgrade/downgrade | ✅ 通过，旧计划 1 行保留 |
| 反向验证 | 3 项（冲突检查 / 幂等性 / 历史不可变）均成立 |

**反向验证明细**：

| 破坏项 | 结果 |
|---|---|
| 去掉基版本冲突检查 | `test_two_clients_on_the_same_base_only_one_wins` + HTTP 契约 2 红 |
| 去掉迁移幂等 | `test_running_the_migration_twice` + `test_a_project_that_already_has` 2 红 |
| 去掉不可变触发器 | 直接改快照**成功**（`IMMUTABILITY NOT ENFORCED`）；恢复后 `IntegrityError` |

## 九、范围声明

**未做**（任务书 §8 明确禁止）：
AI 自动策划、对话导演、`DirectorMessage`/`PlanOperation` 完整模型、镜头锁定与差异确认、
语义候选检索、`director_plan` Job、Markdown/PDF 指导书、最终成片渲染、原生视频 Provider。

**未改**：原片与素材库（迁移与版本管理不触碰原始视频）；既有粗剪/OTIO 功能（保留并改为读版本）；
Vision Provider 与能力门禁；安全开关默认值。

## 十、尚未完成项（诚实列出）

1. **`test_vision_provider` 的 408 分类失败**是基线既有问题，本轮**未修复**（不在 TSB-366 范围）。
2. **真实素材验证未做**：本轮只用隔离数据库与合成数据验证迁移和版本读写；
   8～10 段真实素材的版本化操作未实测。
3. **Windows 真机 UI 手工验收未做**：前端只跑了自动化测试，未在真实浏览器点击验证。
4. **Alembic 未接入 CI 为强制步骤**：只在本地验证，CI 仍以 `init_db` 路径为准。
5. **`source_signature` 对旧数据写 `legacy`**：这是诚实的"未知"，但意味着旧计划的
   来源身份无法与当前素材做强比对（需重新检测才能建立真签名）。
6. 未做 **多 Storyboard**（任务书明确首期只允许一个主方案）。
