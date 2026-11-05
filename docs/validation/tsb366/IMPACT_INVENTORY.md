# TSB-366 影响面清单（§1.3 全仓追踪结果）

基线：`main@b03b216f900d480fe37b32ede96aae105847f825`（TSB-365 审计提交后）
调研日期：2026-11-05

## 一、旧 EditPlan 的完整读写调用链

### 1.1 唯一写入入口：`build_edit_plan()`（全量替换）

[`backend/app/services/timeline_export.py:156`](../../backend/app/services/timeline_export.py#L156)

```
DELETE FROM edit_plan_shots WHERE project_id = ?
   → 按提交数组位置重新编号 order
   → timeline_start_sec 连续累加
```

**确认**：这是本项目**唯一**会写 `EditPlanShot` 的地方。
`analyzer.py:449` 只**读** `segment_id` 判断是否被引用（删除保护），不写。
`story.py:70` 只**读**用于分析建议。`projects.py:134` 只**读**用于计数。

调用方：
| 位置 | 说明 |
|---|---|
| `api/export.py:26` `POST /projects/{id}/edit-plan` | **唯一 HTTP 写入口** |
| `tools/e2e_drive.py` | 验收脚本调用同一 API |
| 测试若干 | `test_concurrent_annotations` / `test_ffmpeg_integration` / `test_long_paths` / `test_timeline_export` |

### 1.2 数组顺序 = 时间线顺序

`build_edit_plan` 忽略客户端 `order` 字段，**以数组位置为准**重编号并重算 `timeline_start_sec`。
响应中用 `order_adjusted` + `note` 说明被覆盖的顺序。

### 1.3 全部读取方（导出/粗剪）

| 读取方 | 位置 | 读什么 |
|---|---|---|
| `export_json` | `timeline_export.py:248` | `_load_plan()` → 全部字段 + `asset.display_name` |
| `export_csv` | `timeline_export.py:260` | 同上，14 列 |
| `export_otio` | `timeline_export.py:298` | 同上 + `asset.source_path` / `fps` / `duration_sec` |
| `roughcut` | `services/roughcut.py:179` | `select(EditPlanShot).order_by(order)` → 生成粗剪视频 |
| `GET /projects/{id}/edit-plan` | `api/export.py:30` | 直接返回 `export_json()` |
| `story.py:70` | 分析建议 | 读 `segment_id` 集合 |

**关键**：四个导出口都经 `_load_plan()` 读同一组行，因此同源一致。

### 1.4 数据库初始化与迁移的真实机制（与任务书假设不同）

任务书 §1.3 与 §3.4 提到 `backend/app/alembic/versions/` 与 Alembic downgrade。
**本仓库没有 Alembic**：既无 `alembic/` 目录，也无 `alembic.ini`，`alembic_version` 表不存在。

实际机制在 [`backend/app/database.py`](../../backend/app/database.py)：

1. `init_db()` 每次启动执行；
2. 先备份 SQLite 文件（`*.pre-migration-<stamp>.bak`），**仅在检测到待变更时**；
3. `Base.metadata.create_all()` 建缺失表（**不会** ALTER 已有表）；
4. `ADDITIVE_MIGRATIONS` 列表逐条 `ALTER TABLE ADD COLUMN`（幂等）；
5. `_legacy_backfill(applied)` 只回填**本次新增**的列；
6. `_backfill_motion_verdicts()` 迁移人工判定记录。

**对 TSB-366 的约束**：所谓 "downgrade" 在本仓库等价于
「回滚由 `init_db` 之前自动生成的 `.bak` 备份恢复」，而不是 Alembic 脚本。
因此 §3.4 的「Alembic downgrade 遵守项目规范」应表述为
「遵守本仓库的备份/回滚规范」，并在文档中说明该差异。

### 1.5 前端

| 位置 | 现状 |
|---|---|
| `frontend/src/lib/api.ts:223-226` | `editPlan` / `exportJson` / `exportCsv` / `exportOtio` 路径 |
| `frontend/src/pages/Export.tsx:36` | `editor` 状态含 `draft` / `dirty` / `server` |
| `frontend/src/pages/Export.tsx:75` | `POST editPlan` 保存 |
| `frontend/src/pages/Export.test.tsx` | 页面测试 |

**已有草稿机制**：`dirty` 标志 + `draft` 与 `server` 分离，正好是 §5.2「区分草稿与已保存」的基础。

## 二、目标设计要点（基于以上事实）

### 2.1 唯一权威来源

迁移后 **StoryboardVersion 是唯一权威**。
`EditPlanShot` 降级为兼容投影：只由 v1 迁移写入，之后不再接受无版本保护的写入。

### 2.2 兼容策略（§5.4 选型）

采用**适配器 + 基版本门禁**：

- 新服务 `load_storyboard_version()` / `save_storyboard_version()`；
- `POST /projects/{id}/edit-plan` 保留路径，新增**必填** `base_version_id`；
- 缺失 `base_version_id` → **428 Precondition Required**（明确兼容错误）；
- 基版本过期 → **409 Conflict**，不覆盖最新版本；
- `GET /edit-plan` 与三个导出、粗剪统一经适配器读**当前版本**；
- 导出响应头记录 `board_id` / `version_id`。

### 2.3 迁移执行点

不放 Alembic；在 `init_db()` 之后追加 `migrate_legacy_edit_plans()`，
幂等、逐项目原子、可检查标记（`StoryboardVersion.reason='legacy_import'` 作为天然标记 + 唯一约束兜底）。

## 三、风险点

| 风险 | 应对 |
|---|---|
| 旧客户端不带 `base_version_id` 直接 POST | 428 拒绝，不静默接受 |
| 迁移重复执行 | 唯一约束 `(board_id, version_no)` + 先查 `reason=legacy_import` 存在即跳过 |
| 孤儿 Segment / 越界区间 | 不静默修复：快照保留原值，`validation_summary` 标记 `needs_review` |
| 导出混读新旧数据 | 全部经适配器读同一 `version_id` |
| 粗剪仍是前置步骤 | 保持现状（本任务不改粗剪成为必需的逻辑） |
| `EditPlanShot` 的 CASCADE 删除 | 保持不变（项目删除时连带删除，不影响 Storyboard 语义） |
