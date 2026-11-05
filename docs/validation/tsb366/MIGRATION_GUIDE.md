# TSB-366 运维指南：备份、升级、对账、回滚

适用对象：需要把现有数据库升级到带 Storyboard 版本的运维/开发者。

> **运行时路径不会自动改表。** 服务启动时 `init_db()` 会自动建表、加触发器、
> 并在每项目第一次启动时把旧 `EditPlan` 冻结为 v1。本指南描述的是
> **受控运维流程**（先备份、再升级、后对账），以及出问题时的回滚。

---

## 一、升级前

### 1. 备份数据库

`init_db()` 在检测到 schema 变更时会自动生成
`<data>/travel_storyboard.pre-migration-<时间戳>.bak`，但**受控升级建议手动再备一份**：

```powershell
cd backend
$src = "$env:TRAVELSB_DATA_DIR\travel_storyboard.db"
$dst = "$env:TRAVELSB_DATA_DIR\travel_storyboard.manual-$(Get-Date -Format yyyyMMdd-HHmmss).bak"
Copy-Item $src $dst
```

WAL 模式下请先让服务停止写入，或用 SQLite 的 `.backup`（`init_db` 内部就是这么做的，
能拿到一致的快照而不是半截 WAL）。

### 2. 记录升级前基线

```powershell
python -c "import sqlite3,os; c=sqlite3.connect(os.path.join(os.environ['TRAVELSB_DATA_DIR'],'travel_storyboard.db')); print({t: c.execute(f'select count(*) from {t}').fetchone()[0] for t in ('projects','edit_plan_shots')})"
```

把这两个数字记下来，后面和第 2 步对账。

---

## 二、升级

### 方式 A：自动（推荐）

直接启动服务即可。`init_db()` 之后会执行 `migrate_legacy_edit_plans()`：

- 每项目独立事务；
- 已有 board 的项目**跳过**（幂等，重复启动安全）；
- 失败项目整项目回滚，并在日志里给出 `failed` 计数。

日志里会出现：

```
Storyboard legacy import: {'projects': 7, 'migrated': 5, 'empty': 2, 'invalid': 0, 'skipped': 0, 'failed': 0}
```

### 方式 B：显式 Alembic（受控/可审阅）

若数据库已由 `init_db()` 建好（三张表已存在），先 stamp 再按需升降：

```powershell
cd backend
$env:TRAVELSB_DATA_DIR = "<数据目录>"
python -m alembic stamp 0001_storyboard_versions
python -m alembic upgrade head     # 若表已存在则为幂等空操作
```

---

## 三、升级后对账

### 1. 每个项目都必须有 board 与 v1

```powershell
python -c @"
import sqlite3, os
db = os.path.join(os.environ['TRAVELSB_DATA_DIR'], 'travel_storyboard.db')
c = sqlite3.connect(db)
missing = c.execute('''
  select p.id, p.name from projects p
  left join storyboards b on b.project_id = p.id
  where b.id is null
''').fetchall()
print('projects without a storyboard:', missing)
print('versions:', c.execute('select count(*) from storyboard_versions').fetchone()[0])
print('source refs:', c.execute('select count(*) from storyboard_source_refs').fetchone()[0])
"@
```

预期：`projects without a storyboard: []`。

### 2. 逐字段对账旧计划与 v1

```powershell
python -c @"
import os, sys
sys.path.insert(0, os.getcwd())
from app.database import SessionLocal
from app.services.storyboard_migration import reconcile_project
from app.models import Project
with SessionLocal() as s:
    for pid in s.scalars(__import__('sqlalchemy').select(Project.id)):
        problems = reconcile_project(s, pid)
        if problems:
            print('DRIFT', pid, problems)
print('reconcile done')
"@
```

**预期输出只有 `reconcile done`。** 任何 `DRIFT` 行都是必须处理的差异——
迁移器**不会**"尽量迁移"后算成功。

### 3. 校验状态分布

```powershell
python -c @"
import sqlite3, os, json
db = os.path.join(os.environ['TRAVELSB_DATA_DIR'], 'travel_storyboard.db')
c = sqlite3.connect(db)
for (state, n) in c.execute('''
  select json_extract(validation_summary, '$.state'), count(*)
  from storyboard_versions group by 1
'''):
    print(f'  {state}: {n}')
"@
```

旧计划里存在孤儿 segment 或越界区间时，对应对照的版本会是 `invalid` 或
`needs_review`——**这是正确行为**：问题被记录下来，而不是被悄悄修复。

---

## 四、回滚

### 4.1 什么时候可以回滚

| 情况 | 可否回滚 | 做法 |
|---|---|---|
| 迁移失败、用户还没开始用新版本 | ✅ | 恢复备份（见 4.3） |
| 迁移成功，但用户**已经开始编辑方案** | ⚠️ **不要回滚** | 见 4.2 |

### 4.2 为什么有历史后禁止自动 downgrade

一旦用户基于 v2、v3 编辑过，降级会**丢弃版本历史**——这正是版本化要防止的事。
任务书 §3.4 同样要求「新版本已被用户使用后，禁止自动 downgrade 丢弃版本历史」。

判断依据：

```powershell
python -c @"
import sqlite3, os
db = os.path.join(os.environ['TRAVELSB_DATA_DIR'], 'travel_storyboard.db')
c = sqlite3.connect(db)
n = c.execute(\"select count(*) from storyboard_versions where reason != 'legacy_import'\").fetchone()[0]
print('user-created versions:', n, '-> rollback is', 'FORBIDDEN' if n else 'safe')
"@
```

### 4.3 最终回滚手段：恢复备份

数据库备份恢复是**最终**手段，也是唯一能保证回到迁移前精确状态的方案：

```powershell
# 1) 停止服务
# 2) 移除当前文件（含 -wal/-shm，否则会回放旧事务）
Remove-Item "$env:TRAVELSB_DATA_DIR\travel_storyboard.db*"
# 3) 放回备份
Copy-Item "<备份文件>" "$env:TRAVELSB_DATA_DIR\travel_storyboard.db"
# 4) 重新启动服务
```

### 4.4 Alembic 层级的 downgrade

只想移除 Storyboard 三张表（**不碰旧计划表**）：

```powershell
cd backend
$env:TRAVELSB_DATA_DIR = "<数据目录>"
python -m alembic downgrade base
```

已验证：执行后 `storyboards`/`storyboard_versions`/`storyboard_source_refs` 与
其触发器全部移除，而 `edit_plan_shots` 的**行数与内容不变**。

> ⚠️ 执行前请满足 4.1 的"尚未被用户使用"条件，否则会丢失用户版本历史。

---

## 五、原片安全

迁移和版本管理**不触碰原始视频**：

- 本指南所有步骤只操作 `travel_storyboard.db`；
- 版本快照只存 `asset_id`/`segment_id` 与秒数，不复制媒体；
- 删除一个 Storyboard 不会删除 Asset/Segment（单向 FK，`test_deleting_a_storyboard_never_deletes_assets_or_segments` 守护）。
