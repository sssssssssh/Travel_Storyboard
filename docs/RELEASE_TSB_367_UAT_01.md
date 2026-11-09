# TSB-367-UAT-01 真实浏览器验收报告

| 字段 | 内容 |
|---|---|
| 基线 | `a48429335e6457f5dd59d0943dd61cef9846a1a0`（与任务书参考起点一致） |
| 验收性质 | **真实 Chromium 浏览器**（非 jsdom、非组件单测） |
| 浏览器 | **Google Chrome 156.0.8078.80**，Playwright 1.44.1 驱动 |
| OS | Windows 11，Python 3.13.7，Node 18.19.0 |
| 服务 | 生产部署路径（前端构建产物由后端托管），`127.0.0.1:9123` |
| 数据 | **仓库外**隔离目录 `F:\tsb-uat-367-01\run2\`，独立数据库 |
| 素材 | 2 个合成视频、5 个 Segment、3 个已保存镜头（真实 `shot_id`） |
| **交付 SHA** | **`f330bf3c944822ffb7270f0cbf7d9e6821d38c65`** |
| CI | run #117，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42555839337 |

> **验收环境是真实的**：Chrome 由 Playwright 通过 CDP 驱动，页面是 `pnpm run build`
> 的产物由 FastAPI 托管，数据来自真实数据库与真实 FFmpeg 分析。**未使用 jsdom**。

---

## 一、开工核对（原样保留）

```
$ git status --short            →  (空)
$ git branch --show-current     →  main
$ git rev-parse HEAD            →  a48429335e6457f5dd59d0943dd61cef9846a1a0
$ git rev-parse origin/main     →  a48429335e6457f5dd59d0943dd61cef9846a1a0
$ git diff --stat               →  (空)
```

**上游变化记录**：本轮开始时本地 `main` 停在 `5890c10`（TSB-366-R3 收尾），
`git fetch` 后发现 `origin/main` 已是 `a484293`。已 `git pull --ff-only` 同步。
任务书要求的参考提交 `a484293` **确实存在且为当前 HEAD**，无差异需要调和。

> 附带发现：前几轮使用的本地代理已失效；本次 Git 操作**通过直连完成**。

---

## 二、UAT 矩阵（全部真实浏览器执行）

| ID | 场景 | 结果 | 证据来源 |
|---|---|---|---|
| **UAT-01** | 进入故事板/导出页、识别版本、令牌消失 | **PASS** | 页面 + API + 截图 |
| **UAT-02** | 选择指定镜头，目标标签随选择变化 | **PASS** | 页面 `data-testid=selected-shot` / `operation-target` |
| **UAT-03** | 上移/下移锚点、边界禁用、拒绝不变版本 | **PASS** | 页面 + POST 载荷 + 只读 API |
| **UAT-04** | 裁剪：diff 正确、仅改该镜头、Segment 未变 | **PASS** | diff 文本 + 前后快照 |
| **UAT-05** | 锁定/解锁；锁定后裁剪被拒 | **PASS** | 快照 `locked` 字段 + 409 |
| **UAT-06** | 移除：拒绝后仍在、确认后仅该镜头离开 | **PASS** | 前后 `shot_ids` |
| **UAT-07** | 改选镜头后旧提案的作用对象 | **PASS**（绑定正确） | 前后快照对比谁被改 |
| **UAT-08** | 未保存草稿：刷新保留、服务端不变、显式重载丢弃 | **PASS** | 输入框值 + 版本号 |
| **UAT-09** | 两标签页版本竞争 → 409 明显提示、不覆盖 | **PASS** | 409 响应 + 版本不变 + 页面文本 |
| **UAT-10** | 版本历史与恢复（恢复产生新版本、旧版本保留） | **PASS** | 版本数 +1，历史 +1 |
| **UAT-11** | 无效引用 / 跨项目 / 无令牌访问 | **PASS** | 404 / 404 / 401 |
| **UAT-12** | 源片内容、大小、mtime 不变 | **PASS** | SHA-256 前后一致 |

**12/12 场景通过，无 BLOCKED。**

### 关键场景的"三重证据"

任务书要求「不能只看按钮、toast 或截图」。每个关键场景都同时采集：

| 场景 | ① 页面 | ② 实际请求 | ③ 服务端真实状态 |
|---|---|---|---|
| UAT-03 上移 | 点击「上移」 | `POST .../operations` + `POST .../apply` 200 | `[B,A,C]`，版本 +1 |
| UAT-05 锁定裁剪 | 点击「裁剪」 | `POST .../operations` → **409** | 版本号不变 |
| UAT-09 双标签 | 冲突提示可见 | tab A 的 apply → **409** | 版本未被覆盖 |
| UAT-07 绑定 | 目标标签已变 | apply 指向同一 operation | **只有原提案镜头被改** |

---

## 三、缺陷修复：UAT-F01（P1）

### 3.1 缺陷

`frontend/src/pages/Export.tsx` 的「上移」「下移」构造了错误的锚点：

| 按钮 | 原代码 | 后果 |
|---|---|---|
| 上移 | `after_shot_id = draft[idx-1]` | 目标本来就在前一个镜头**之后**，**移动无效** |
| 下移（末项） | `before_shot_id = draft[idx-1]` | 反而把镜头**往上移** |
| 两者 | 只判断 `!selectedShot` | 首项「上移」、末项「下移」**可点**，产生无意义提案 |

服务端语义（`_apply_move_shot`）：`before_shot_id = B` 表示放在 B **前面**，
`after_shot_id = B` 表示放在 B **后面**。原实现把两者用反了。

### 3.2 RED（修复前，真实记录）

```
❯ src/pages/Export.move.test.tsx (6 tests | 5 failed)
× UAT-F01 up on the middle shot anchors BEFORE the previous shot
  → expected undefined to be 'shot-A'
× UAT-F01 up is disabled for the first shot
  → expected false to be true
× UAT-F01 down is disabled for the last shot
  → expected false to be true
× UAT-F01 up on the last shot moves it one step up, not a no-op
  → expected undefined to be 'shot-B'
× UAT-F01 a disabled move never issues a request
  → expected false to be true
Tests  5 failed | 1 passed (6)
```

### 3.3 最小修复

```tsx
const canMoveUp = Boolean(selectedShot) && selectedIndex !== null && selectedIndex > 0
const canMoveDown = Boolean(selectedShot) && selectedIndex !== null
  && selectedIndex < draft.length - 1
```

- 上移 → `before_shot_id: draft[idx-1].shot_id`
- 下移 → `after_shot_id: draft[idx+1].shot_id`
- 两个按钮的 `disabled` 改用 `!canMoveUp` / `!canMoveDown`

**未改动**：操作面板的搭建逻辑、服务端语义、其他任何 UI 结构。

### 3.4 GREEN

```
✓ src/pages/Export.move.test.tsx (6 tests)
Tests  6 passed (6)
```

### 3.5 反向验证

把锚点与边界逻辑还原成缺陷版本：

```
❯ src/pages/Export.move.test.tsx (6 tests | 5 failed)
Tests  5 failed | 1 passed (6)
```

失败集合与 RED 阶段**完全一致**，说明这 6 项确实守护该修复。
恢复后 6/6 绿，源码中无临时标记残留。

### 3.6 真实浏览器确认

修复在生产构建中生效：

```
start v5 shots = 2e95,ef7b,303c
PASS  「上移」把 B 移到 A 前 → 顺序 [B,A,C]
PASS  确认后版本 +1（v5 → v6）
```

---

## 四、验收中观察到的非缺陷事项（如实记录）

### 4.1 UAT-07：旧提案不会误作用到新选中的镜头（**安全**）

实测：选中镜头 1 建裁剪提案 → 改选镜头 2 → 应用：

```
proposal diff: 裁剪 fce8f2d4: 0.00–4.00s → 0.00–3.50s
after re-selection, target label = 目标：镜头 1fc0b69b
apply POSTs: [.../operations/<id>/apply]
changed shots: [ 'fce8f2d4' ]        ← 只有原提案的镜头被改
```

**服务端以提案自身的 `shot_id` 为准，不受 UI 选择影响**——数据安全，无 P0 缺陷。

**可用性观察（未修）**：改选镜头后旧提案仍显示在面板上，目标标签却已更新为
新镜头，可能让用户误以为提案作用于新镜头。这是**认知风险，不是数据错误**。
按任务书「发现 FAIL 才改，未复现不得声称缺陷已修」，本轮**不做改动**，
列为建议改进项。

### 4.2 UAT-10：恢复到一个空方案是合法行为

历史中最早版本（v1）本身就是 0 个镜头的初始方案，恢复它得到空方案属于
正常语义，不是缺陷。

### 4.3 SPA 路由形态

前端使用 `HashRouter`，深层页面正确形式是 `/#/project/<id>/export`。
若用 `/project/<id>/export` 直接访问，`StaticFiles(html=True)` 只回退到根
`index.html` 之外会返回 `{"detail":"Not Found"}`。这是 HashRouter 的**固有形态**，
本轮未改动路由架构。

---

## 五、验证结果

| 项 | 结果 |
|---|---|
| 真实浏览器 UAT | **12/12 场景通过** |
| 新增前端测试 | **6 项，RED→GREEN，反向验证通过** |
| 前端 typecheck / lint / test / build | **全部通过**（129 passed） |
| **后端完整回归** | **1741 collected / 0 failed / 0 errors / 6 skipped** |

6 项 skip 原因均为 `link creation is a platform fact`（Windows 上创建符号链接需
管理员权限这一平台事实），已在 `conftest._EXPECTED_SKIP_REASONS` 白名单中登记，
非静默跳过。

---

## 六、范围声明

**改动**：`frontend/src/pages/Export.tsx`（上移/下移锚点与边界）、
新增 `frontend/src/pages/Export.move.test.tsx`、本轮文档。

**未改动**：后端任何代码、视频模型、代理链路、TSB-368 相关、路由架构、
操作面板其他行为、任何既有测试断言。

**未删除/skip/xfail** 任何既有测试。


---

## 七、CI 执行结果

| 项 | 值 |
|---|---|
| 交付 SHA | `f330bf3c944822ffb7270f0cbf7d9e6821d38c65` |
| run | **#117** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42555839337 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ success |
| `Required FFmpeg (windows-latest)` | ✅ success |
| `Required FFmpeg (ubuntu-latest)` | ✅ success |
| `Backend (Python 3.11)` ×2 | ✅ success |
| `Backend (Python 3.13)` ×2 | ✅ success |

---

## 八、完成度判定

| 门禁 | 状态 |
|---|---|
| 真实浏览器 UAT-01～UAT-12 | ✅ **12/12 通过（真实 Chrome）** |
| UAT-F01 RED → 最小修复 → GREEN | ✅ 5 failed → 6 passed |
| 反向验证 | ✅ 还原缺陷后 5 failed，集合与 RED 一致 |
| 镜头切换 / 待确认提案 / 未保存草稿 / 409 冲突 | ✅ 全部实测通过 |
| 每步同时核对页面 + 请求 + 版本 | ✅ 三重证据逐项留存 |
| 原片内容/大小/mtime 不变 | ✅ SHA-256 前后一致 |
| 前端四项 | ✅ 129 passed |
| 后端完整回归 | ✅ 1741 / 0 failed / 6 skipped（均已登记原因） |
| CI | ✅ run #117 7/7 |
| 发布记录 / 回归清单 / 证据索引 | ✅ 已完成 |
| 工作区干净、已推送 | ✅ 与 origin 一致 |

**结论：本轮 UAT 通过，无 BLOCKED 项。**

### 未完成 / 遗留

1. **真实旅行素材验收未做**：本轮全部使用合成素材（2 个测试图案视频）。
   AGENTS.md §六 的真实素材门禁仍未满足，**不得据此宣称已满足发布门禁**。
2. **UAT-07 可用性改进建议**（未改）：改选镜头后旧提案仍显示、目标标签已更新，
   存在认知风险。实测无数据安全问题，按"发现 FAIL 才改"原则本轮不动。
3. **T03 建议项（未验证）**：任务书提到的其余边界（如超长镜头名、极端时长）
   本轮未逐一执行。
4. **手动验收清单**：本轮已由自动化真实浏览器完成，无需转交人工清单；
   但若需复跑，步骤见证据索引 `README.md` 的"复现方式"。
