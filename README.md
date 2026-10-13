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
├── docs/
│   ├── DESIGN.md       # 完整设计文档
│   └── REGRESSION.md   # 回归测试清单（自动化 + 手动）
├── .github/workflows/  # CI
└── .env.example
```

## 快速开始

### 0. 前置依赖

- Python 3.11+（开发机已验证 3.13）
- [FFmpeg](https://ffmpeg.org/download.html)（`ffmpeg` / `ffprobe` 需在 PATH 中，或通过 `.env` 指定路径）
- Node.js 18+ 与 pnpm（仅构建前端时需要）

### 1. 启动后端

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

### 2. 启动前端（开发模式）

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 Vite 提示的地址（默认 `http://localhost:5173`），前端会把 `/api` 代理到 `127.0.0.1:8737`。
开发模式下把令牌拼到地址后即可：`http://localhost:5173/?token=<token>`。

### 3. 生产模式（后端直接托管前端）

```powershell
cd frontend
pnpm build
cd ../backend
uvicorn app.main:app --host 127.0.0.1 --port 8737
```

访问启动日志里的带令牌地址即可（后端检测到 `frontend/dist` 会自动托管前端页面）。

### 4. 运行测试

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
