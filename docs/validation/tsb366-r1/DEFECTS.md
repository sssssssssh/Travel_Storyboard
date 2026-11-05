# TSB-366-R1 缺陷清单

基线 `8f2d4eb`。每项均**先写失败测试**，确认 RED 后再修。

| 编号 | 缺陷 | RED 证据 | 修复文件 | 守护测试 |
|---|---|---|---|---|
| **P0-01** | 历史版本读取内容错配：`GET /edit-plan?version_id=v1` 返回 v1 的标签 + 当前版本的镜头 | `asked for v1 (3 shots) but got 8 shots — metadata and content disagree` | `backend/app/api/export.py` | `test_p0_01_*`（6 项） |
| **P0-02** | `shot_id` 在前端编辑保存时丢失：只改时长，全部身份重建 | `test_p0_02_the_legacy_endpoint_preserves_shot_ids_too` FAILED；前端 payload 无 `shot_id` | `frontend/src/pages/Export.tsx`、`frontend/src/lib/types.ts`、`backend/app/schemas.py` | `test_p0_02_*`（5 项）+ 前端 2 项 |
| **P1-03** | 恢复历史版本无确认，成功后直接 `dirty=false` → 草稿静默丢失 | 前端 `P1-03 cancelling…` 无门禁可测（缺陷期无确认逻辑） | `frontend/src/pages/Export.tsx` | 前端 3 项 |
| **P1-04** | 迁移失败被吞 + 无 board 时自动建空版本 → 空方案取代旧方案且迁移永久跳过 | `test_p1_04_a_failed_migration_must_not_yield_an_empty_initial_version` FAILED（`LegacyMigrationPending` 不存在，空版本被创建） | `backend/app/models.py`、`backend/app/services/storyboard_migration.py`、`backend/app/services/storyboard_service.py` | `test_p1_04_*`（7 项） |
| **P1-05** | 对账以 `current_version_id` 为基准，编辑后比的是 v2/v3 | `Left contains one more item: '…: shot count 2 -> 3'` | `backend/app/services/storyboard_migration.py` | `test_p1_05_*`（4 项） |
| **P1-06** | 写入校验边界未明确 | 跨项目引用/显式拒绝模式无 `InvalidReference` | `backend/app/services/storyboard_service.py` | `test_p1_*`（2 项） |

## RED → GREEN

| 阶段 | 结果 |
|---|---|
| 修复前 | **6 failed** / 18 passed（专项 24 项） |
| 修复后 | **24 passed** |

修复前完整输出见本地文件 `RED-all.local.txt`（本目录 `.gitignore` 排除）。

## 与任务书的一致性

任务书描述的 5 项缺陷经逐项实测**全部属实**，未发现任务书与代码的矛盾。
**补充发现**：`EditPlanShotIn`（HTTP 请求 schema）同样缺 `shot_id`，属 P0-02 同一根因，
已一并修复——未扩大范围到其他功能。
