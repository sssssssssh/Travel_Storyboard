# TSB-368 发布记录：AI 导演候选素材查询与策划输入

| 字段 | 内容 |
|---|---|
| 基线 | `bddadd82a7d37fc681549fb3e6036511914985d3` |
| 阶段 | 368-0 / 368-A / 368-B |
| 368-0 提交 | `68477a4034d1067754ad8fe0c4ec1bbf4c8936d3` |
| 368-A/B 提交 | `2bd05293424bbd7983bfe893a67415e8b7f08848` |
| 证据目录 | [docs/validation/tsb368/](validation/tsb368/) |

---

## 一、总览：这一轮回答了什么

系统现在可以**在不调用模型、不修改任何素材**的情况下回答四个问题：

1. 当前项目有哪些可用镜头？→ 整个项目的 Segment，含从未被分析过的
2. 哪些内容有可靠观测证据？→ 三渠道分开标注，各带新鲜度状态
3. 哪些只是推测或未看过？→ `not_analyzed` / `stale` / `freshness_unverified` / `failed`
4. 给后续 AI 导演的输入是什么？→ `GET .../director/context` 的版本化 DTO

**没有做**：自动写故事、发模型请求、修改镜头、自动保存 Storyboard、上传原片。

---

## 二、368-0：UAT-07 可用性收口

### 2.1 问题

`pendingOps` 在 `Export` 父组件，`proposal`/`error`/`conflict` 在 `OperationPanel`
子组件。改选镜头时父组件重建 `pendingOps` 与 `targetLabel`，子组件旧 `proposal` 存活
——面板用「目标：镜头 B」的标签显示仍属于镜头 A 的 diff。

### 2.2 修复（最小）

- `OperationPanel` 新增 `selectionKey` 属性，标识「面板当前关于什么」
- 面板记录可见提案是为哪个 key 构建的，key 变化时**在 render 阶段**丢弃
  `proposal`/`conflict`/`error`（不是 effect，避免旧 diff 在过渡帧被画出来）
- 在途 propose 响应用 `issuedRef` 标记发起时的 key，迟到响应整条丢弃
- `Export` 传入 `selectionKey = 选中镜头 id + 序列化后的 pendingOps`

**未把 `setPendingOps([])` 当作完整修复**——那只能清父组件状态。

### 2.3 RED → GREEN → 反向验证

| 阶段 | 结果 |
|---|---|
| RED | **3 failed \| 4 passed** |
| GREEN | **7 passed** |
| 反向验证 | 移除保护 → **恰好 3 failed**，集合与 RED 一致，无残留标记 |

### 2.4 真实 Chrome

生产构建下 **9/9 通过**：改选后旧提案从 DOM 消失、无可确认按钮、目标标签为 B、
全程无 apply 请求、服务端版本未变。

---

## 三、368-A：只读聚合层

新模块 `backend/app/services/director_candidates.py`。

### 3.1 核心决策

| 决策 | 理由 |
|---|---|
| 候选全域 = 项目全部 Segment | 不是 `vision-candidates`，不是送模型队列 |
| 三渠道状态互不替代 | 静帧 ≠ 视频，多帧 ≠ 原生视频 |
| 人工判断优先 | `favorite` 不被质量分否决，`exclude` 不被 AI 推翻 |
| 签名可验证则验证，不可则承认 | `freshness_unverified` 优于猜 `current` |
| 批量加载 | 1000 镜头固定 7 条 SQL，不是 3003 条 |

### 3.2 修复的两个自身缺陷（由测试捕获）

| 缺陷 | 发现方式 | 修复 |
|---|---|---|
| **N+1**：每 Segment 查 3 次，1000 镜头 = 3003 条 SQL | `test_statement_count_does_not_grow_with_the_number_of_segments` | 改批量加载，固定 7 条 |
| **failed 不冒泡**：失败结果在候选级不可见 | `test_a_failed_result_is_failed_not_silently_missing` | 失败渠道写入 `limitations` |

---

## 四、368-B：API、筛选、排序、context、UI

### 4.1 接口

```
GET /api/projects/{project_id}/director/candidates
GET /api/projects/{project_id}/director/context
```

两个接口复用同一 `collect_candidates` / `coverage`，不可能各写一套逻辑。

### 4.2 实现中修复的两个自身缺陷

| 缺陷 | 发现方式 | 修复 |
|---|---|---|
| `scene_tag` 过滤永远返回 0 | `test_scene_tag_only_matches_current_static_readings` | 允许 `freshness_unverified` 匹配（否则离线机器上该筛选不可用） |
| 多样性只覆盖 2/3 个资产 | `test_diversity_spreads_across_assets` | 在全量上轮转而非仅当前页窗口 |

第二个缺陷根因值得记录：最初只对 `offset+limit` 窗口做轮转，导致
`asset2` 的镜头还没进入窗口就被截断。测试用 15 个均匀分布的镜头暴露了它。

### 4.3 只读 UI

`frontend/src/pages/DirectorCandidates.tsx`，路由 `/project/:id/director`。

- 三渠道三个独立徽标，状态文字与颜色不同
- 覆盖率**同时显示分子和分母**，并明确提示"分析覆盖不完整"
- 区分「项目没有素材」与「筛选无命中」
- **没有任何**应用/保存/生成按钮
- 默认 `verify_freshness=false`，浏览不触碰文件系统

### 4.4 真实 Chrome 验收 12/12

三重对照（页面 + API + 数据库）：

| 检查 | 结果 |
|---|---|
| 页面渲染只读界面 | PASS |
| 页面覆盖率 == API 覆盖率 | PASS |
| API 覆盖率分母 == 数据库镜头总数 | PASS |
| 页面展示「未分析」而非把未知当已看 | PASS |
| 三渠道独立 badge | PASS |
| 无任何写请求 | PASS |
| 浏览后故事板版本不变 | PASS |
| context 含 digest 且不含故事线 | PASS |
| digest 稳定可复现 | PASS |
| 响应不含源路径/令牌 | PASS |
| 页面无应用异常 | PASS |

---

## 五、关键数据

| 项 | 值 |
|---|---|
| SQL 语句数（4→1000 镜头） | **恒定 7 条** |
| 测试新增 | 后端 **63**，前端 **19**（7 + 12） |
| 反向验证 | 两轮，均精确失败 |

---

## 六、诚实边界（不得越过）

1. **静帧语义结果 ≠ 看过视频**
2. **多帧运动分析 ≠ 原生视频理解**
3. **原生视频观察 ≠ 已看完整素材库**
4. **模型"精彩/动作"线索 ≠ 人工确认事实**
5. **合成素材 + Chrome UAT + CI ≠ 真实旅行素材 / 真实模型能力（G5/G6）**

本轮全部测试使用**合成视频**，项目中的真实素材库与真实模型输出**未经验证**。

---

## 七、最终状态

| 项 | 值 |
|---|---|
| 368-0 提交 | `68477a4034d1067754ad8fe0c4ec1bbf4c8936d3` |
| 368-A/B 提交 | `2bd05293424bbd7983bfe893a67415e8b7f08848` |
| **后端全量** | **1804 collected / 0 failed / 0 errors / 6 skipped** |
| **前端全量** | **148 passed**（17 文件） |
| 前端四项 | typecheck / lint / test / build 全部通过 |

6 项 skip 原因均为 `link creation is a platform fact`（Windows 创建符号链接需管理员
权限这一平台事实），已在 `conftest._EXPECTED_SKIP_REASONS` 登记，非静默跳过。

### 阶段状态

| 阶段 | 状态 |
|---|---|
| 368-0 | **通过**（RED→GREEN→反向验证 → Chrome 9/9） |
| 368-A | **通过**（24 + 4 项，含 N+1 实测） |
| 368-B | **通过**（35 + 12 项，Chrome 12/12） |

---

## 八、范围声明

**新增**：
- `backend/app/services/director_candidates.py`
- `backend/app/services/director_query.py`
- `backend/app/services/director_schemas.py`
- `backend/app/api/director.py`
- `backend/tests/test_tsb368_candidates.py`（24）
- `backend/tests/test_tsb368_query_cost.py`（4）
- `backend/tests/test_tsb368_director_api.py`（35）
- `frontend/src/pages/DirectorCandidates.tsx`
- `frontend/src/pages/DirectorCandidates.test.tsx`（12）
- `frontend/src/components/OperationPanel.stale.test.tsx`（7）

**修改**：
- `backend/app/main.py`（注册路由）
- `frontend/src/components/OperationPanel.tsx`（selectionKey）
- `frontend/src/pages/Export.tsx`（传入 selectionKey）
- `frontend/src/lib/api.ts`、`frontend/src/lib/types.ts`、`frontend/src/App.tsx`

**未改动**：TSB-366-R4 视频长路径与代理修复、工作流测试语义、Alembic 迁移、
任何既有测试断言、原片。

**未引入**：新数据库、向量检索、新依赖、新表。


---

## 九、CI 执行结果

| 项 | 值 |
|---|---|
| 主提交 SHA | `2bd05293424bbd7983bfe893a67415e8b7f08848` |
| run | **#120** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42575414061 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ success |
| `Required FFmpeg (windows-latest)` | ✅ success |
| `Required FFmpeg (ubuntu-latest)` | ✅ success |
| `Backend (Python 3.11)` ×2 | ✅ success |
| `Backend (Python 3.13)` ×2 | ✅ success |

---

## 十、未完成 / 遗留

1. **真实旅行素材验收未做**：本轮全部使用合成视频。G5/G6 门禁仍未满足，
   **不得**据此宣称可发布。
2. **真实模型输出未验证**：三渠道的语义/运动/视频结果在测试中由 ORM 直接构造，
   未经真实模型产生。字段映射按**真实写入点**核对过，但真实模型的取值分布未经检验。
3. **`freshness_unverified` 是常态**：多帧与原生视频的签名在无重分析前提下无法重算，
   因此这两类在正常浏览中几乎总是 `freshness_unverified`。这是**如实反映**，
   不是缺陷；若未来需要精确新鲜度，需要设计可离线重算的签名方案。
4. **`source_identity` 需要读源文件**：`verify_freshness=true` 时会对每个有静帧结果的
   素材做一次有界采样（192KB）。默认关闭；开启时的 I/O 成本未做大规模实测。
5. **UI 未接入代表帧缩略图**：任务书 §3.5 提到"点击候选可沿用已有安全的代表帧 URL"，
   本轮未实现该交互，只展示了结构化信息。
6. **分页一致性**：跨多次请求不冻结快照（任务书 §4.3 允许），并发改动期间翻页可能
   看到新增/移除的镜头。
7. **多样性策略只有一种**：`diversify` 布尔开关，未提供多种跨素材策略。
