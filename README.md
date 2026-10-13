# Travel Storyboard v2.0

**AI 旅行影片导演助手** — 把大规模旅行原片转换为**可检索、可筛选、可复核、可导出剪辑计划**的素材库，并通过多模态分析生成有故事感的旅行短片策划。

> 本地运行、原片只读、默认不上传。给 AI 的仅是用户确认过的脱敏缩略图 / 低清预览。

---

## 定位与核心原则

- **原片只读**：所有产物写入隔离工作区，绝不删除 / 覆盖 / 改名原片。
- **先发现片段，再抽代表帧**：不按文件数量分配固定抽帧数，而是先找到有意义的镜头边界。
- **技术分与创作判断分离**：曝光 / 清晰度 / 重复性等技术指标与情绪 / 故事价值分开，任何评分可人工覆盖。
- **稳定映射**：每个推荐镜头通过 `asset_id + segment_id + 精确起止秒数` 与原视频保持稳定对应。
- **检测边界不可变**：检测得到的区间单独存储（`detected_start_sec` / `detected_end_sec`），
  人工裁剪只改可编辑区间。重新检测时按检测区间匹配，因此裁剪、评分、备注都能保留。
- **断点续跑 + 增量**：单文件、单阶段落盘，中断后可续跑，不反复扫描 200GB。
- **隐私安全**：服务只绑定 `127.0.0.1`，首次启动生成本地随机访问令牌；上传包默认去除真实路径、
  GPS、人脸等敏感信息。

## 功能状态（Roadmap）

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 素材建库：ffprobe 索引、稳定 `asset_id`、SQLite、增量 + 断点续跑 | ✅ |
| M2 | 镜头识别 + 画质：场景边界、长镜头子段、代表帧、感知排重、画质指标 | ✅ |
| M3 | 双轮 AI 闭环：脱敏分析包 + 复核请求导入 + 低清预览 + 可选直连模型 | ✅ |
| M4 | 故事板 + 导出：剪辑时间线 CSV / JSON / OTIO、代理粗剪 | ✅ |

> 说明：OTIO 为实验性中间格式（需在目标 NLE 实测）；直连模型需配置
> `TRAVELSB_MODEL_BASE_URL` / `TRAVELSB_MODEL_API_KEY`，未配置时走离线分析包流程。
> 未做真机验收前，M3/M4 建议按“可用但需实测”看待。


## 技术栈

- **后端**：Python 3.11+ · FastAPI · SQLAlchemy 2 · SQLite (WAL) · Pydantic v2
- **媒体处理**：FFmpeg / FFprobe · OpenCV (headless) · numpy · Pillow（感知哈希为内置 dHash 实现）
- **前端**：React 18 · TypeScript · Vite · Tailwind CSS · React Query · zustand

## 目录结构

```
Travel_Storyboard/
├── backend/            # FastAPI 服务（REST API + 媒体处理 + 任务调度）
│   ├── app/
│   │   ├── main.py         # 应用入口、静态托管、启动逻辑
│   │   ├── config.py       # 配置（.env）
│   │   ├── database.py     # SQLAlchemy 引擎 / 会话
│   │   ├── models.py       # 数据模型（Project/Asset/Segment/Frame/Job...）
│   │   ├── schemas.py      # Pydantic 请求/响应模型
│   │   ├── security.py     # 本地访问令牌
│   │   ├── ffmpeg.py       # ffmpeg/ffprobe 封装与路径探测
│   │   ├── services/       # indexer / detector / quality / frames / runner
│   │   └── api/            # REST 路由
│   ├── tests/              # pytest：安全边界 / 数据一致性 / 任务生命周期
│   ├── requirements.txt
│   └── requirements-dev.txt
├── frontend/           # React + Vite + TS + Tailwind
├── scripts/            # 一键启动：start.ps1 / start.bat / start.sh
├── docs/
│   ├── DESIGN.md       # 完整设计文档
│   └── REGRESSION.md   # 回归测试清单（自动化 + 手动）
├── .github/workflows/  # CI
└── .env.example
```

## 快速开始

### 推荐：一键启动

Windows 双击 `scripts\start.bat`，或在 PowerShell 里：

```powershell
.\scripts\start.ps1
```

macOS / Linux：

```bash
./scripts/start.sh
```

脚本会依次：检查 Python 3.11+ 与 FFmpeg → 创建 `backend\.venv` 并装依赖 →
构建前端 → 启动服务并**自动用带令牌的地址打开浏览器**。每一步都是幂等的，可以反复运行。

常用参数：

| 参数（PowerShell / bash） | 作用 |
| --- | --- |
| `-SkipInstall` / `--skip-install` | 跳过依赖安装（venv 已就绪时更快） |
| `-SkipFrontend` / `--skip-frontend` | 只起后端（前端用 `pnpm dev`） |
| `-Port 8800` / `PORT=8800` | 换端口 |
| `-NoBrowser` | 不自动打开浏览器 |

### 手动启动

**前置依赖**：Python 3.11+（已在 3.13 上验证）、FFmpeg（在 PATH 中，或用 `.env` 指定路径）、
Node.js 18+ 与 pnpm（仅构建前端时需要）。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8737
```

首次启动会在 `data/` 下创建数据库和 `data/token.txt`（随机访问令牌，权限 `0600`）。

**访问方式**：启动日志会打印一条带令牌的地址，形如
`http://127.0.0.1:8737/?token=xxxxxxxx`。用这条链接打开即可 —— 前端读取一次令牌后会把它
从地址栏移除（`history.replaceState`）并存进 `sessionStorage`。

> 服务**不再**通过 API 下发令牌。所有 `/api/*` 请求需带 `X-Access-Token` 头（或 `?token=`）。
> 绑定仅限回环地址；携带非回环 `Host` / `Origin` / `Referer` 的请求会被 403 拒绝（防 DNS rebinding）。
> 两个免令牌接口（`/api/system/health`、`/api/system/config`）只返回可用性与工具名，
> 不返回任何本机绝对路径。

### 前端开发模式

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 Vite 提示的地址（默认 `http://localhost:5173`），前端会把 `/api` 代理到 `127.0.0.1:8737`。
开发模式下把令牌拼到地址后即可：`http://localhost:5173/?token=<token>`。

### 生产模式（后端直接托管前端）

```powershell
cd frontend
pnpm build
cd ../backend
uvicorn app.main:app --host 127.0.0.1 --port 8737
```

访问启动日志里的带令牌地址即可（后端检测到 `frontend/dist` 会自动托管前端页面）。
`scripts\start.ps1` 做的就是这件事。

### 运行测试

```powershell
cd backend
pip install -r requirements-dev.txt
python -m pytest -q
```

前端：

```powershell
cd frontend
pnpm run typecheck
pnpm run lint
pnpm run build
```

自动化测试不依赖 FFmpeg（覆盖安全边界、数据一致性、任务生命周期）。需要真实解码的场景见
[docs/REGRESSION.md](docs/REGRESSION.md) 的手动清单。

## 使用流程（六阶段闭环）

1. **新建项目** — 选择素材根目录、输出工作区，设置片长 / 画幅 / 风格 / 采样预算。
2. **阶段 A 素材建库** — ffprobe 读取技术元数据，赋予稳定 `asset_id`。
3. **阶段 B 候选片段** — 场景边界检测 → 长镜头子段 → 代表帧 → 画质与排重。
4. **阶段 C 本地分镜审阅** — 分镜墙筛选、👍 推荐 / ⭐ 收藏 / 🚫 排除、拖拽设定 in/out。
5. **阶段 D AI 导演分析** — 生成脱敏分析包（离线）或直连模型 API，两轮闭环。
6. **阶段 E 导出** — `edit_plan.json` / `剪辑时间线.csv` / OTIO / 代理粗剪。

## 配置

复制 `.env.example` 为 `.env` 并按需修改。数据库、模型调用 URL、FFmpeg 路径等均可通过环境变量注入。

## 文档

- 完整设计：[docs/DESIGN.md](docs/DESIGN.md)
- 回归测试清单（T01–T14）：[docs/REGRESSION.md](docs/REGRESSION.md)
- 需求来源：`Travel_Storyboard_v2.0_详细设计方案.md`

## 持续集成

`.github/workflows/ci.yml` 在 `main` 的 push / PR 上执行：

- 后端：Python 3.11 与 3.13 × Ubuntu / Windows，跑 `pytest`；
- 前端：`pnpm typecheck` · `pnpm lint` · `pnpm build`。

## License

待定（在正式发布前请勿用于商业用途；配乐等素材需用户自行确认授权）。

## v2.0.2 稳定性与校验模式

后台重分析只提交算法派生字段，评分、收藏、备注、故事用途和提交时最新的合法裁剪
不会被旧对象覆盖。无法匹配且有人工状态、复核请求或剪辑计划引用的旧镜头保留为
“需要复核”，旧 JPEG 仍可访问。请在分镜审阅中检查告警，使用当前检测镜头更新计划；
未复核的旧镜头不会直接进入新分析包、AI 抽样、二轮代理或粗剪。轻微裁剪夹取会保留原选择审计。

| 选项 | 实际行为与代价 |
| --- | --- |
| 默认快速校验 | 每次后台读取头/中/尾各至多 64 KiB，并结合路径、大小、mtime；不能证明全文件未改 |
| 后台完整校验 | 后台流式计算 SHA-256，每个文件每个新作业完整读取一遍；任务接口立即返回 ID/202 |
| 严格复核 | 在处理后、发布前再完整读取一遍，UI 单独显示额外读取量；不会静默增加完整读盘 |
| 强制扫描 | 对新作业纳入的素材重新执行 FFprobe，保持 Asset ID，失败项可重试 |
| 强制检测 | 重新切镜/抽帧；提交最新人工状态，同一作业恢复不重做签名未变的已完成项 |

完整校验在后台显示当前文件、读取字节、计划读取量、校验完成文件数和取消状态。
只在**同一作业**内复用已完成校验（快速签名及文件身份未变）；新完整校验任务总会重新
读取文件，不跨任务仅凭 size/mtime 宣称严格校验通过。中断在半途的单个文件会从头
重新哈希，其他已完成项保留；实际 `bytes_read` 累计包含重试，所以可能超过计划量。
这些字节统计仅指 SHA-256 流式读取，不包含 FFmpeg 解码与快速采样的 I/O。

API 示例：`POST /api/projects/{id}/jobs/scan` 或 `.../detect`，请求体
`{"force":true,"options":{"full_hash":true,"strict_full_hash":false}}`。
`strict_full_hash=true` 会自动启用完整校验。原片始终只读，派生文件只写隔离工作区。

本轮自动化和跨平台 CI 证据、旧库兼容性及回退方法见
[发布记录](docs/RELEASE_V2_0_2.md)。生成视频的测试不等于已完成 200GB 旅行实片验收。
正式扩大到完整素材前，仍需完成 Windows 8～10 段真实原片的小样本验收。
