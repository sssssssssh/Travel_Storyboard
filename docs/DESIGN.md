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
    database.py      # engine / SessionLocal / Base / init_db（含幂等增量迁移）
    models.py        # ORM 模型
    schemas.py       # Pydantic schema
    security.py      # 令牌生成/校验、回环来源校验、鉴权中间件
    paths.py         # 数据根 / 项目工作区解析（含 output_dir）、重叠保护
    ffmpeg.py        # FFmpegLocator + probe + extract_frame + preview
    services/
      indexer.py       # M1 素材建库
      detector.py      # M2 场景/长镜头子段检测
      quality.py       # M2 画质指标 + 感知排重
      analyzer.py      # M2 检测编排：抽帧、增量跳过、缓存回收
      frames.py        # 代表帧选择与抽取
      review_package.py# M3 脱敏分析包（重编码、隐私、体积预算）
      story_planner.py # M3 复核请求导入 / 代理生成 / 直连模型分析
      timeline_export.py# M4 剪辑计划校验与 JSON/CSV/OTIO 导出
      roughcut.py      # M4 代理粗剪（含音频）
      runner.py        # Job 调度、互斥、取消、断点续跑
      model_client.py  # 可选视觉模型调用
    api/
      deps.py        # DB 会话依赖
      projects.py    # 项目 CRUD（输出目录校验）
      jobs.py        # 任务创建/状态/取消
      assets.py      # 素材列表/详情
      segments.py    # 片段列表/筛选/标注/in-out/重置裁剪
      media.py       # 白名单产物服务（帧/代理/包/粗剪）
      packages.py    # 分析包列表与下载
      story.py       # 复核请求与故事分析
      export.py      # 剪辑计划与导出
      system.py      # health / config
  tests/               # pytest：安全边界 / 数据一致性 / 任务 / 隐私 / 导出
frontend/
  src/
    main.tsx, App.tsx, router
    lib/api.ts       # 带令牌的 fetch 封装 + 媒体 URL 构造
    lib/types.ts     # 与 schemas.py 对应的类型
    lib/store.ts     # 应用引导与令牌状态
    lib/useJob.ts    # 任务轮询 hook
    lib/format.ts    # 时间码/体积格式化
    pages/           # Home / Project / Review / AIDirector / Export / Settings
    components/      # SegmentCard / FilterBar / JobProgress ...
scripts/               # start.ps1 / start.bat / start.sh 一键启动
docs/                  # DESIGN.md / REGRESSION.md
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
- `analyzed_fingerprint`, `analyzed_detect_config`, `analyzed_at`（增量检测用）

**唯一约束**：`(project_id, source_path)` 唯一，保证同项目同文件只有一行；
探测失败时复用同一行并置 `scan_status=failed` / `error_code=probe_failed`，不会插入第二行。

**增量判断**：`relative_path + file_size_bytes + mtime_ns` 相同则跳过重扫；`quick_fingerprint`（首尾块哈希 + 大小）用于迁移/重复提示，碰撞时二次确认。
检测阶段另有指纹：`analyzed_fingerprint == quick_fingerprint` 且
`analyzed_detect_config == 当前检测配置指纹` 时跳过，`force=true` 可强制重算。

### 5.3 `segments`（候选片段）
- `id` (UUID, 稳定 segment_id), `asset_id`, `project_id`
- `detected_start_sec`, `detected_end_sec` (**检测产生的不可变边界**), `is_trimmed`
- `start_sec`, `end_sec` (可编辑的有效区间，源文件时间线，小数秒)
- `boundary_reason` (hard_cut / content_change / max_duration / manual)
- `quality_metrics` (JSON: 曝光/模糊/重复/稳定性 + 置信度)
- `content_tags` (JSON: 标签 + 来源 rule/ai/manual)
- `story_role`, `user_rating`, `status`
- `frame_refs` (JSON: 代表帧路径列表), `proxy_ref`
- `parent_segment_id`, `algorithm_version`

> **为什么边界要分两份**：重新检测时按 `detected_*` 匹配既有片段，因此人工裁剪、
> 评分、备注在重复检测后仍然保留。早期版本用 `start_sec/end_sec` 匹配，
> 一旦裁剪过就会被判定为新片段，标注随之丢失。

### 5.4 `frames`（代表帧）
- `id`, `segment_id`, `asset_id`, `time_sec` (小数秒)
- `image_path` (工作区相对路径；自定义输出目录时为绝对路径), `phash` (感知哈希), `is_representative`
- `width`, `height`, `file_size_bytes`

### 5.5 `jobs`（任务 / 断点）
- `id`, `project_id`, `job_type` (scan / detect / package / proxies / roughcut)
- `status` (pending/running/succeeded/partial_failed/failed/cancelled)
- `config_snapshot` (JSON), `stats` (JSON: 总数/成功/失败/进度), `resume_token` (JSON)
- `failures` (JSON: `{count, items:[{kind, ref, message}]}`，部分失败的逐项原因)
- `error`, `created_at`, `started_at`, `finished_at`

### 5.6 `user_annotations`（人工标注，独立保存，重跑不丢失）
- `id`, `segment_id`, `rating` (like/favorite/exclude/pending), `note`
- `in_sec`, `out_sec` (人工调整后的区间), `created_at`, `updated_at`

> M3/M4 新增表：`review_packages`、`review_requests`、`edit_plan_shots`，
> 由 `create_all` 建表；旧库的增量列由 `init_db()` 的迁移列表补齐（迁移前自动备份）。

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

- 服务仅绑定 `127.0.0.1`；首次启动生成随机访问令牌（写入 `data/token.txt`，权限 `0600`）。
- **不提供任何免鉴权的令牌下发接口**（早期的 `/api/system/token` 已删除）。令牌由启动日志给出，
  前端通过 `/?token=…` 读取一次后存入 `sessionStorage` 并从地址栏移除。
- 所有 `/api/*` 需携带 `X-Access-Token`（或 `?token=`）。仅 `/api/system/health` 与
  `/api/system/config` 免令牌，且两者只返回可用性与工具**名称**，不含任何本机绝对路径。
- 请求的 `Host` / `Origin` / `Referer` 必须是回环地址，否则 403（防 DNS rebinding）。
- 媒体服务不接受任意路径：只服务「已解析后确实位于该项目工作区、且位于白名单子目录
  （`frames`/`proxies`/`packages`/`roughcut`/`contact_sheets`）、扩展名在白名单内」的文件。
  数据库与 `token.txt` 位于数据根目录，不在任何项目工作区内，因此无法被这些路由触达。
- 原片只读打开；产物写入隔离工作区。项目工作区不得与素材目录重叠（含父子关系），
  否则创建/修改项目返回 422。
- 分析包的隐私承诺是**被强制执行**的：分镜图一律用 Pillow 解码后重编码（EXIF/GPS/相机信息
  无法存活）、只收录被选中片段引用到的素材、manifest 不写 ffmpeg 绝对路径、受体积预算约束。
  这不等于人脸/车牌已脱敏 —— 仍需用户自行查看后再上传。

## 8. 断点续跑与并发

- `runner.py` 维护进程内线程池（`ThreadPoolExecutor(max_workers=max_concurrency)`），
  线程池关闭后可重建，便于测试与多次 lifespan。
- Job 状态机：`pending → running → succeeded / partial_failed / failed / cancelled`。
  - `partial_failed`：任务本身跑完，但有逐项失败（`failures` 记录 kind/ref/message）。
  - `cancelled`：通过 `POST /api/jobs/{id}/cancel` 触发，任务内的 `raise_if_cancelled()` 生效。
- 同一项目内 `scan`/`detect`/`package`/`proxies`/`roughcut` 互斥，重复提交返回 409。
- 每个文件/片段完成后更新 `stats` 与 `resume_token`；进程重启时 `resume_interrupted_jobs()`
  会把遗留的 `pending`/`running` 任务重新派发。
- 抽帧先写 `staging/`，成功后原子替换并回收上一轮无引用的 JPEG；检测按素材指纹 +
  算法/配置指纹跳过未变化的素材。

## 9. API 一览

实际路由由 `app.openapi()` 生成，共 33 个操作。下表为人工归纳。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/health` | 健康检查（免令牌；只返回工具名，不含路径） |
| GET | `/api/system/config` | 公开配置（免令牌；数据目录只给目录名） |
| GET/POST | `/api/projects` | 列出 / 新建项目（含 `cache_dir`） |
| GET/PATCH/DELETE | `/api/projects/{id}` | 项目详情 / 更新 / 删除（校验输出目录重叠） |
| GET | `/api/projects/{id}/assets` | 素材列表（分页/筛选） |
| GET | `/api/assets/{id}` | 素材详情 |
| POST | `/api/projects/{id}/jobs/{job_type}` | 启动任务（`scan`/`detect`/`package`/`proxies`/`roughcut`） |
| GET | `/api/projects/{id}/jobs` | 项目任务列表 |
| GET | `/api/jobs/{id}` | 任务状态（含 `stats`、`failures`、`resume_token`） |
| POST | `/api/jobs/{id}/cancel` | 取消任务 |
| GET | `/api/projects/{id}/segments` | 片段列表（筛选/分页；含检测边界与裁剪标记） |
| GET/PATCH | `/api/segments/{id}` | 片段详情 / 更新 |
| POST | `/api/segments/{id}/annotate` | 人工标注（收藏/排除/备注/in-out，校验裁剪范围） |
| POST | `/api/segments/{id}/reset-trim` | 清除裁剪，回到检测边界 |
| GET | `/api/frames/{id}/image` | 代表帧图片（按数据库记录授权） |
| GET | `/api/projects/{id}/media` | 项目工作区内的白名单产物 |
| GET | `/api/projects/{id}/review-requests/{rid}/proxy` | 第二轮低清预览 |
| GET | `/api/projects/{id}/roughcut` | 代理粗剪（缺镜头时为 `roughcut_INCOMPLETE.mp4`） |
| GET | `/api/projects/{id}/packages` | 列出分析包 |
| GET | `/api/packages/{id}/download` | 下载分析包 zip |
| POST | `/api/projects/{id}/review-requests/import` | 导入 `review_requests.json`（严格校验） |
| GET | `/api/projects/{id}/review-requests` | 列出复核请求 |
| POST | `/api/projects/{id}/review-requests/proxies` | 生成第二轮低清预览 |
| POST | `/api/projects/{id}/story/analyze` | 可选直连模型分析（含覆盖统计与结构化校验） |
| GET/POST | `/api/projects/{id}/edit-plan` | 读取 / 保存剪辑计划（全量校验） |
| GET | `/api/projects/{id}/export/{json,csv,otio}` | 导出时间线 |

## 10. 里程碑与验收对照

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 素材建库：ffprobe 索引、稳定 `asset_id`、SQLite、增量 + 断点续跑 | ✅ |
| M2 | 镜头识别 + 画质：场景边界、长镜头子段、代表帧、感知排重、画质指标 | ✅ |
| M3 | 双轮 AI 闭环：脱敏分析包 + 复核请求导入 + 低清预览 + 可选直连模型 | ✅ |
| M4 | 故事板 + 导出：剪辑时间线 CSV / JSON / OTIO、代理粗剪 | ✅ |

具体验收场景见 [REGRESSION.md](REGRESSION.md) 的 T01–T19 清单（自动化部分由
`backend/tests/` 覆盖，需要真实解码的保持手工）。

> OTIO 已用参考实现 `opentimelineio` 做读回校验（结构、clip 数、源区间、
> 总时长一致），但**尚未在真实 NLE 中实测**；直连模型路径在未配置
> `TRAVELSB_MODEL_BASE_URL` / `TRAVELSB_MODEL_API_KEY` 时走离线分析包流程。

## 11. 已知依赖与后续

- FFmpeg 路径可配置，支持 PATH / 环境变量 / 常见安装目录探测；
  `scripts/start.ps1` 会在启动前提示是否就绪。
- **数据库迁移**：`init_db()` 执行幂等的增量迁移（补列），迁移前自动把 SQLite 文件
  复制为 `<name>.pre-migration-<时间戳>.bak`。目前未引入 Alembic；
  当出现需要重建表或改写数据的变更时应切换到 Alembic。
- 已知限制：升级前就已裁剪过的镜头，其原始检测边界无法还原，回填后等于裁剪区间，
  再做检测会被视为新镜头（升级后新产生的裁剪不受影响）。
- 检测算法为 1 fps HSV 直方图硬切，快速移动可能误切、短转场可能漏切；
  尚未在真实旅行片上做混淆矩阵与阈值校准。
