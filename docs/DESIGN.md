# Travel Storyboard v2.0 — 实现设计文档

> 本文档描述从需求方案到代码的实现设计，是 `backend/` 与 `frontend/` 的权威依据。
> 需求来源：《Travel_Storyboard_v2.0_详细设计方案.md》。

## 1. 目标与边界

**目标**：Windows 本地运行，把大规模旅行原片（60+ 视频 / 约 200GB）转化为可检索、可筛选、可复核、可导出的素材库，并支持分轮多模态分析生成旅行短片策划。

**非目标（首期）**：不实现专业调色 / 混音 / 完整 NLE；不宣称电影审美评分为客观真值；不承诺所有剪辑软件无损导入（尤其剪映需专项验证）。

## 2. 总体架构

```text
React 前端 (Vite + TS + Tailwind)
        |
  本地 REST API (127.0.0.1 + 访问令牌 + CORS 白名单)
        |
     FastAPI 应用层
        |-- 项目服务 ProjectService
        |-- 资源服务 MediaIndexer      (ffprobe, 稳定 asset_id, 文件变化)
        |-- 切镜服务 SegmentDetector   (场景边界, 长镜头子段)
        |-- 画质服务 QualityAnalyzer   (清晰度, 曝光, 重复)
        |-- 代理服务 FrameService      (代表帧, 低清预览)
        |-- 分析包服务 ReviewPackage   (M3: 上传包, 隐私, manifest)
        |-- 故事服务 StoryPlanner      (M3: AI 请求/结果导入导出)
        |-- 导出服务 TimelineExport    (M4: CSV/JSON/OTIO)
        |
   SQLite (WAL) + 本地缩略图/预览缓存 + JSON 导出
        |
   FFmpeg / FFprobe / OpenCV / 可选 AI（云端或本地）
```

**依赖原则**：核心扫描、抽帧、人工筛选完全离线可用；云端多模态 AI 为可选集成。

## 3. 技术选型

| 层 | 选型 | 说明 |
|---|---|---|
| 后端框架 | FastAPI + Uvicorn | 本地 REST，服务仅绑定环回地址 |
| ORM | SQLAlchemy 2.0 | 类型化、可替换数据库 |
| 数据库 | SQLite (WAL) | 默认；通过 `DATABASE_URL` 可切换 |
| 配置 | pydantic-settings | 读取 `.env` / 环境变量 |
| 媒体探测 | FFprobe | JSON 输出，解析视频/音频轨技术元数据 |
| 抽帧/代理 | FFmpeg | 列表参数调用（不依赖 shell 拼接） |
| 图像分析 | OpenCV + numpy + imagehash | 场景差异、清晰度、曝光、感知哈希 |
| 任务调度 | 内置 Job 表 + 线程池 | 并发 1–2，逐文件/逐段 checkpoint |
| 前端 | React 18 + TS + Vite + Tailwind | 分镜墙、拖拽、筛选 |

## 4. 目录结构

```
backend/
  app/
    main.py          # 入口：lifespan 初始化、静态托管、路由挂载
    config.py        # Settings（pydantic-settings）
    database.py      # engine / SessionLocal / Base / init_db
    models.py        # ORM 模型
    schemas.py       # Pydantic schema
    security.py      # 令牌生成/校验、鉴权依赖
    ffmpeg.py        # FFmpegLocator + probe + extract_frame + preview
    services/
      indexer.py     # M1 素材建库
      detector.py    # M2 场景/长镜头子段检测
      quality.py     # M2 画质指标 + 感知排重
      frames.py      # 代表帧选择与抽取
      runner.py      # Job 调度与断点续跑
    api/
      deps.py        # DB 会话、鉴权依赖
      projects.py    # 项目 CRUD
      jobs.py        # 任务创建/状态
      assets.py      # 素材列表/详情
      segments.py    # 片段列表/筛选/标注/in-out
      media.py       # 缩略图/代理文件服务
      system.py      # health / config / token
frontend/
  src/
    main.tsx, App.tsx, router
    api/client.ts    # 带令牌的 fetch 封装
    api/endpoints.ts
    pages/           # Home / Project / Assets / Review / Settings
    components/      # SegmentCard / FilterBar / JobProgress ...
```

## 5. 数据模型

### 5.1 `projects`
- `id` (UUID 主键), `name`, `source_roots` (JSON: 素材根目录列表), `output_dir`
- `settings` (JSON: 片长/画幅/风格/采样预算等), `created_at`, `updated_at`

### 5.2 `assets`（原始视频）
- `id` (UUID, 稳定 asset_id), `project_id`
- `source_path` (绝对路径), `display_name`, `file_size_bytes`, `mtime_ns`, `quick_fingerprint`
- `duration_sec` (float, 小数秒), `fps_num`, `fps_den`, `vfr_flag`
- `width`, `height`, `rotation`, `codec`, `pixel_format`
- `color_primaries`, `color_transfer`, `color_space`
- `has_audio`, `audio_codec`
- `scan_status` (pending/running/succeeded/failed/skipped), `error_code`, `error_message`
- `analysis_version`, `scanned_at`

**增量判断**：`relative_path + file_size_bytes + mtime_ns` 相同则跳过重扫；`quick_fingerprint`（首尾块哈希 + 大小）用于迁移/重复提示，碰撞时二次确认。

### 5.3 `segments`（候选片段）
- `id` (UUID, 稳定 segment_id), `asset_id`, `project_id`
- `start_sec`, `end_sec` (源文件时间线，小数秒)
- `boundary_reason` (hard_cut / content_change / max_duration / manual)
- `quality_metrics` (JSON: 曝光/模糊/重复/稳定性 + 置信度)
- `content_tags` (JSON: 标签 + 来源 rule/ai/manual)
- `story_role`, `user_rating`, `status`
- `frame_refs` (JSON: 代表帧路径列表), `proxy_ref`
- `parent_segment_id`, `algorithm_version`

### 5.4 `frames`（代表帧）
- `id`, `segment_id`, `asset_id`, `time_sec` (小数秒)
- `image_path` (相对工作区), `phash` (感知哈希), `is_representative`
- `width`, `height`, `file_size_bytes`

### 5.5 `jobs`（任务 / 断点）
- `id`, `project_id`, `job_type` (scan / detect / frames / ...)
- `status` (pending/running/succeeded/failed/cancelled)
- `config_snapshot` (JSON), `stats` (JSON: 总数/成功/失败/进度), `resume_token` (JSON)
- `error`, `created_at`, `started_at`, `finished_at`

### 5.6 `user_annotations`（人工标注，独立保存，重跑不丢失）
- `id`, `segment_id`, `rating` (like/favorite/exclude/pending), `note`
- `in_sec`, `out_sec` (人工调整后的区间), `created_at`, `updated_at`

> M3/M4 新增：`review_requests`、`edit_plan_shots`、`story_plans`（在对应里程碑加入，保持 `create_all` 自动建表）。

## 6. 关键算法

### 6.1 素材建库（M1）
1. 递归遍历 `source_roots`，按扩展名过滤视频文件。
2. `relative_path + size + mtime_ns` 命中既有 asset → 跳过。
3. `ffprobe` 解析技术元数据；新文件生成 UUID `asset_id`。
4. 单文件失败 → 标记 `failed` + `error_code`，不阻塞全局。
5. 每文件落库并推进 `resume_token`，支持中断续跑。

### 6.2 场景边界 + 长镜头子段（M2）
1. 以低分辨率（如 1 fps 或更稀疏）采样帧，计算相邻帧 HSV 直方图差异。
2. 差异超过阈值 → 硬切候选；自适应阈值缓解快速运动误切。
3. 连续无切点超过 `max_segment_duration`（默认 30s，可配置）→ 在内容变化峰值处强制切分。
4. 记录 `boundary_reason` 与 `algorithm_version`。

### 6.3 代表帧选择（M2）
1. 每个候选片段取 1–3 张：起点避黑帧、中间代表帧、内容变化帧。
2. 片段极短时不机械重复取图。
3. 记录每视频覆盖率与未检查区间。

### 6.4 画质指标（M2）
- 清晰度：Laplacian 方差。
- 曝光：亮度均值 + 过曝/欠曝比例。
- 重复：感知哈希（dHash）Hamming 距离 + 时间相邻信息。
- 低技术质量不自动舍弃，保留“有故事但画质一般”的候选。

## 7. 安全与隐私

- 服务仅绑定 `127.0.0.1`；首次启动生成随机访问令牌（写入 `data/token.txt`）。
- 所有 `/api/*` 需携带 `X-Access-Token`；媒体文件服务同样鉴权。
- 原片只读打开；产物写入隔离工作区，危险删除仅限清理工作区缓存且先预览。
- 上传包默认去除绝对路径，敏感信息（GPS/人脸）导出前提示确认，不自动上传。

## 8. 断点续跑与并发

- `runner.py` 维护进程内线程池（`ThreadPoolExecutor(max_workers=2)`）。
- Job 状态机：`pending → running → succeeded/failed`，取消支持 `cancelled`。
- 每个文件/片段完成后更新 `stats` 与 `resume_token`；重启后从 token 继续。
- 临时产物以 `.partial` 写出，完成后原子重命名。

## 9. API 一览（v1）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/health` | 健康检查（含 ffmpeg 探测状态） |
| GET | `/api/system/config` | 当前公开配置（脱敏） |
| GET | `/api/system/token` | 获取本地令牌（供前端注入） |
| GET/POST | `/api/projects` | 列出 / 新建项目 |
| GET/PATCH/DELETE | `/api/projects/{id}` | 项目详情 / 更新 / 删除 |
| POST | `/api/projects/{id}/jobs/scan` | 启动素材建库任务 |
| POST | `/api/projects/{id}/jobs/detect` | 启动片段检测 + 抽帧任务 |
| GET | `/api/jobs/{id}` | 任务状态 |
| GET | `/api/projects/{id}/assets` | 素材列表（分页/筛选） |
| GET | `/api/projects/{id}/segments` | 片段列表（筛选/分页） |
| GET/PATCH | `/api/segments/{id}` | 片段详情 / 更新 |
| POST | `/api/segments/{id}/annotate` | 人工标注（收藏/排除/备注/in-out） |
| GET | `/api/frames/{id}/image` | 代表帧图片 |
| GET | `/api/media/proxy/{...}` | 代理/缩略图文件 |
| POST | `/api/projects/{id}/jobs/package` | 生成第一轮脱敏分析包 |
| GET | `/api/projects/{id}/packages` | 列出分析包 |
| GET | `/api/packages/{id}/download` | 下载分析包 zip |
| POST | `/api/projects/{id}/review-requests/import` | 导入 `review_requests.json`（严格校验） |
| GET | `/api/projects/{id}/review-requests` | 列出复核请求 |
| POST | `/api/projects/{id}/review-requests/proxies` | 生成第二轮低清预览 |
| POST | `/api/projects/{id}/story/analyze` | 可选直连模型分析 |
| POST | `/api/projects/{id}/edit-plan` | 保存剪辑计划（校验） |
| GET | `/api/projects/{id}/edit-plan` | 读取剪辑计划 |
| GET | `/api/projects/{id}/export/{json,csv,otio}` | 导出时间线 |
| POST | `/api/projects/{id}/jobs/roughcut` | 生成 720P 代理粗剪 |

## 10. 里程碑与验收对照

- **M1**：稳定 ID、SQLite、异常隔离、增量 + 断点（验收 T01/T03/T04/T07）。
- **M2**：场景/内容边界、长镜头再分段、画质、排重、覆盖率（验收 T02/T05/T06/T12）。
- **M3**：双轮 AI 闭环（验收 T08/T09）。
- **M4**：剪辑时间线导出 + 粗剪（验收 T10/T11）。

## 11. 已知依赖与后续

- FFmpeg 路径可配置，支持 PATH / 环境变量 / 常见安装目录探测。
- 首期不引入 Alembic，采用 `create_all` 自动建表；数据模型稳定后视需要引入迁移。
