# Travel Storyboard v2.1.2

**AI 旅行影片导演助手** — 把大规模旅行原片转换为**可检索、可筛选、可复核、可导出剪辑计划**的素材库，并通过多模态分析生成有故事感的旅行短片策划。

当前应用发布目标为 **2.1.2**，权威版本见根目录 `VERSION`；后端/API/启动器直接读取，前端 package.json 由 `python scripts/sync_version.py` 同步，构建与 CI 检查一致性。升级仅更新产品元信息，`ANALYSIS_VERSION=2.0.2` 和已有 Prompt/缓存键保持不变。此前 v2.1.1 是整改文档批次，当时应用仍为 2.1.0；历史发布记录保留原口径。正式发布门禁仍待补充真实素材和真人工 UI 验收，见 [v2.1.2 记录](docs/RELEASE_V2_1_2.md)。

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
  EXIF/GPS 元数据；可辨认的人脸、车牌、屏幕等视觉信息须由用户自行检查。

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

- **给 AI 协作者与新贡献者（先读这个）**：[AGENTS.md](AGENTS.md) —— 硬约束、工作流纪律、环境事实与常见坑
- 完整设计：[docs/DESIGN.md](docs/DESIGN.md)
- 回归测试清单（T01–T25 + v2.0.4/v2.0.5/v2.0.6 新增）：[docs/REGRESSION.md](docs/REGRESSION.md)
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

## v2.0.3 复核请求处置

分析包只包含当前有效镜头，默认排除 🚫 和 `needs_review/archived/invalid`；收藏筛选也遵守这一规则。失效、缺帧和预算排除数量在 Manifest.selection 中可查。没有当前有效镜头时明确报错，请重新扫描/检测、复核或调整筛选。

AI 导演与分镜审阅现在可查看旧区间、最新检测候选、收藏、用途、备注和历史预览。失效请求需要明确选择当前镜头和区间，点击“创建新请求并替代旧请求”；旧请求保留为 superseded，旧 ID、计划和预览保留。也可暂缓或放弃旧请求；计划仍引用失效镜头时，按提示进入导出页人工修改。系统不会自动确认失效镜头或自动映射 ID。

代理遵守精确请求时长；例如 3.0～3.6 秒生成约 0.6 秒视频。执行与提交前都检查最新裁剪、状态和源身份；取消、失败或期间变更均保留旧正式代理。预算不足保留待处理请求并报错，可增加预算重新生成。

默认采用头/中/尾快速校验。完整 SHA-256 是**每个任务**的读盘成本：200GB 分别运行完整扫描和完整检测，可能额外读取约 400GB；严格二次复核继续增加读盘，文件中断重读与 FFmpeg 解码 I/O 另计。界面显示字节进度与重读次数，不承诺固定 ETA。

技术方案与关闭表见 [v2.0.3 发布记录](docs/RELEASE_V2_0_3.md)，JSON 示例见 [docs/examples/v2.0.3](docs/examples/v2.0.3)。Windows 真实旅行素材验收和 200GB 全量性能仍未执行，不能视作已通过。

## v2.0.4 工程收尾整改

修复 Windows 长路径全链路、任务创建互斥、代理时长验收与包版本字段不一致，关闭 TSB-241 ~ TSB-245。

**Windows 长路径**：FFmpeg/FFprobe 命令行、`Path` 操作、Pillow、OpenCV 各自是独立的兼容边界，已在每处统一处理（命令行走 `\\?\` 前缀，Python 侧走 `safe_*` 封装，图像改用 `imdecode`/字节读取）。ffconcat 清单单独处理：实测双引号条目会被当作文件名、无引号在空格处截断、反斜杠会被当转义符，因此固定使用单引号 + POSIX 分隔符。失败时给出 `PATH_TOO_LONG` / `OUTPUT_NOT_CREATED` / `MEDIA_UNREADABLE` 等稳定错误码，不再出现「FFmpeg 返回 0、Python 找不到文件」。

**任务互斥**：`/jobs/{job_type}` 与 `/review-requests/proxies` 现在共用同一创建服务，互斥检查与插入在同一 `BEGIN IMMEDIATE` 事务内完成（前端禁用按钮和进程内锁都挡不住并发）。冲突返回 409 并带冲突任务类型与 ID。

**代理时长**：发布前用 ffprobe 实测时长，容差取 `max(0.25s, 10%)`；不符则转 `needs_review` 并保留历史代理。产物不可读仍按系统故障记为 `failed`。

实测：Windows（`LongPathsEnabled=0`）下 **329 passed / 0 skipped**，其中深路径、并发互斥、时长验收用例 71 个。详见 [v2.0.4 发布记录](docs/RELEASE_V2_0_4.md) 与 [测试记录](docs/validation/v2.0.4/backend-windows-py3.13.md)。

**本轮未完成**：Windows 实机 8～10 段真实旅行素材验收（RT-18/19）仍未执行，CI 与合成素材通过不能替代。门禁清单见 [实机验收记录](docs/validation/v2.0.4/WINDOWS_REAL_FOOTAGE.md)。

## v2.0.5 深路径收尾与取消语义

修复深路径 HTTP 读取、安全删除与深目录扫描 API（TSB-251/252/254），并让**取消真正终止在跑的媒体进程**（TSB-253）。

**取消语义**：此前取消是纯协作式的 —— 只在**条目之间**检查。取消一个长编码时任务行立刻显示 `cancelled`，但 FFmpeg 会继续跑完：项目锁被占、CPU 持续占用。现在 `cancel_check` 回调从任务一路传进 `ffmpeg.run()`，回调为真即终止进程组并抛 `CANCELLED`。终止**只作用于自己启动的进程树**，不波及并发任务；不传回调时行为与原先完全一致。

实测同一条 8 秒媒体命令、0.6 秒后请求取消：旧路径 **8.44s** 才返回，新路径 **0.73s** 即中止。真实长素材上这是分钟级的无效占用。被杀的媒体进程记为**取消**而非失败；代理与检测任务同样接线，取消不再被记成「一条失败请求」后继续编码下一个。

实测：Windows（`LongPathsEnabled=0`）下 **398 passed / 0 failed / 0 skipped**（基线 376 + 新增 22），`skipped=0` 表示媒体用例实际执行。详见 [v2.0.5 发布记录](docs/RELEASE_V2_0_5.md) 与 [测试记录](docs/validation/v2.0.5/backend-windows-py3.13.md)。

**本轮未完成**：Windows 实机 8～10 段真实旅行素材验收（RT-18/19）仍未执行。在其实机记录补齐前，v2.0.5 同样不应宣称已满足真实素材发布门禁。

## v2.0.6 取消状态与资源清理闭环

建立「请求取消 → 停止实际处理 → 完成清理 → 释放互斥 → 最终取消」的可验证闭环，关闭 TSB-261 ~ TSB-264。

**取消中状态**：此前 cancel 直接把状态写成终态 `cancelled`，而互斥只认 `pending/running`。用户一点取消，项目立刻显得空闲 —— 尽管 worker 还在编码、仍持有项目锁、暂存尚未回收，新互斥任务因此可被提前接受。现在引入**非终态** `cancelling`：取消只写「已请求」，worker 真正退出并完成清理后才写 `cancelled` 与 `finished_at`；`cancelling` 期间新互斥任务返回 **409**（不是排队）。互斥改为「项目锁 + 数据库」两层保证，锁序固定为先项目锁后写事务。

**暂存必达清理**：粗剪此前只在成功路径删除暂存，取消/超时/失败均提前返回并留下整目录已编码片段。现在每个任务的暂存回收包在 `try/finally` 中，**先于**互斥释放；清理走正向白名单（只允许 `staging-*`、`staging/<asset>/<token>`、`proxies/staging/<token>`），拒绝正式产物目录、数据目录、素材目录与项目外路径，且**不跟随链接**。失败以 `CleanupResult` 记录残留路径、字节与可重试标记，不用 `ignore_errors=True` 当「已清理」的证据。

**进程树终止**：`terminate()` 此前只作用于直接子进程，孙进程会存活。现在 Windows 用 **Job Object + `KILL_ON_JOB_CLOSE`**、POSIX 用独立进程组信号回收整棵树，含父进程已先退出的情况。作用域仅限本次启动，**禁止**按进程名全局终止。

修复过程中由测试发现并修复三个真实缺陷：清理会**跟随 junction 删除项目外数据**；深路径下 `FileNotFoundError` 被当成「空目录」导致清理「成功」却什么都没删；清理计数在目录已删除时报告 `removed_files=0`。

实测：Windows（`LongPathsEnabled=0`）下 **456 passed / 0 failed / 0 skipped**（基线 398 + 新增 58），主线 CI **7/7 成功**（含 Ubuntu 与 Windows 的强制媒体作业）。详见 [v2.0.6 发布记录](docs/RELEASE_V2_0_6.md) 与 [测试记录](docs/validation/v2.0.6/backend-windows-py3.13.md)。

### v2.1.1 / M5-R2 整改

新增人工动态复核独立历史与有效性显示、单镜头证据缓存和重跑审计、事务内取消/源状态终检、HTTPS 传输诊断与视觉暂存所有权清理；真实路由保留前端的本次上传同意并兼容旧 options 请求。

本轮本地 macOS arm64 / Python 3.12.14：强制媒体全量 **745 passed / 44 个 Windows 平台专属 skipped / 0 failed**（collected 789），前端四项检查通过、**40 tests passed**。已通过有效 HTTPS 在隔离副本分析 6 段真实素材中的 8 个镜头及 4 个多帧镜头，原片 SHA 不变；素材数量/类型及完整 UI 人工验收仍不足。**代码级整改通过，发布门禁未完成**。详见 [发布记录](docs/RELEASE_V2_1_1.md) 和 [Mac 实测及门禁](docs/validation/v2.1.1/MAC_REAL_FOOTAGE.md)。

模型地址与凭据仅填写被忽略的 backend/.env，默认 MODEL_MODE=off；公网服务须有效 HTTPS 或认证加密隧道，开发明文例外不作为生产验收。Mac 启动前确保 Python 3.11+ 在 PATH，已有虚拟环境可先激活。

**本轮未完成**：真实素材实机验收（TC-29/30）未执行。按任务书标准，本轮结论为**「代码级整改通过，发布门禁未完成」**。

## v2.0.7 取消竞态与暂存安全

取消与自然完成由同一终态 CAS 路径收敛。暂存回收仅限当前任务创建并登记的精确叶目录，
拒绝跨任务、跨项目及链接穿越。已取消任务会按清理结果显示「已清理」「有暂存残留」
或「尚无本次清理统计」；残留可在任务停止、项目空闲且所有权仍有效时安全重试。
进程重启后不会自动接管未知孤儿目录。

新粗剪保留历史文件，通过原来的预览地址读取最新已提交版本。
实现和验证见 [v2.0.7 发布记录](docs/RELEASE_V2_0_7.md)。真实 Windows 旅行素材门禁
与 200GB 全量性能仍未执行。


### v2.1.2 M5 收尾

应用/API/前端/启动器统一使用根 VERSION=2.1.2；清理故障由 Runner 单一终态收敛，界面显示残留与安全补救，重试不重跑模型。新增强制媒体格式矩阵，并修正扫描/视觉任务结束后的界面刷新。

本地 macOS 后端普通与严格全量各 **784 passed / 44 Windows 平台专属 skipped / 0 failed / 0 errors**（collected 828），前端四项通过、**48 tests passed**。隔离浏览器使用现有 6 段中的 2 段真实视频，检测 17 镜头，完成 HTTPS 分析、缓存、动态复核、清理/取消、代理/粗剪及三种剪辑计划下载；6 个原片、正式 SQLite 和 282 个历史产物保持不变。真人播放/审阅、8～10 个不同实片与补充类型仍不足；合成 HDR 不证明色彩准确，未测 200GB 性能。结论为**代码级整改通过，正式发布门禁未完成**（最终同 HEAD CI 以交付记录为准）。见 [发布记录](docs/RELEASE_V2_1_2.md)、[Mac UI 证据](docs/validation/v2.1.2/MAC_UI_E2E.md)与[发布门禁](docs/validation/v2.1.2/RELEASE_GATE.md)。

### M5.3-R3.2 回退执行与界面闭环（TSB-356～359）

父任务结束后继续跟踪活跃的回退子任务，完成时刷新并显示实际多帧结果；失败、取消、空结果与部分成功分别展示。真实 Runner 的父子执行、合成视频抽帧、Mock 模型、实际入库及公开 API 查询已有闭环测试，旧 B23 不再接受 running 作为最终通过。成功安排降级不再误报父部分失败，配置保留 force，已入库状态需本次实际证据支持。

本地 macOS 严格后端 **1349 collected / 1305 passed / 44 Windows 平台 skipped / 0 failed / 0 errors**，前端 typecheck/lint/test/build 全部通过、**98 passed**。最终同 SHA CI 7/7 须另外核验，完整交付链接和回执见 [发布记录](docs/RELEASE_M5_3_R3_2.md)与[证据索引](docs/validation/m5.3-r3.2/README.md)。本轮只使用合成媒体与回环 Mock；G5=blocked_by_endpoint、G6=not_evaluated、Windows 真人验收 not_done，发布门禁未完成。
