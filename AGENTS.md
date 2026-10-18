# AGENTS.md — 给 AI 协作者的项目约定

本文件是这个仓库的**单一事实源**：面向 AI 编码助手与新贡献者，说明硬约束、
必须遵守的工作流、验证方式，以及容易踩的坑。

> 与其它文档的分工：本文件讲**怎么做**（流程与纪律）；
> 设计原理见 [docs/DESIGN.md](docs/DESIGN.md)；测试用例映射见 [docs/REGRESSION.md](docs/REGRESSION.md)。

---

## 一、项目定位（一句话）

把大规模旅行原片转为**可检索、可筛选、可复核、可导出剪辑计划**的素材库，
并生成有故事感的短片策划。本地运行、原片只读、默认不上传。

---

## 二、不可违反的硬约束

这些是产品承诺，任何改动都不得破坏。**改动前先确认不违反，改动后用测试证明。**

| # | 约束 | 含义 |
| --- | --- | --- |
| C1 | **原片只读** | 绝不删除 / 覆盖 / 改名 / 移动用户原片。产物只写隔离工作区。 |
| C2 | **检测边界不可变** | `detected_start_sec` / `detected_end_sec` 单独存储，人工裁剪只改可编辑区间。重新检测按检测区间匹配，因此裁剪、评分、备注必须保留。 |
| C3 | **稳定映射** | 每个推荐镜头靠 `asset_id + segment_id + 精确起止秒数` 与原片稳定对应。**不要**自动重映射 ID 或自动确认失效镜头。 |
| C4 | **技术分与创作判断分离** | 曝光/清晰度/重复性等技术指标与情绪/故事价值分开。任何评分可人工覆盖，后台重分析**不得**覆盖人工状态。 |
| C5 | **隐私安全** | 只绑定 `127.0.0.1`；`/api/*` 需令牌；上传包去除真实路径与 EXIF/GPS；非回环 `Host`/`Origin`/`Referer` → 403。 |
| C6 | **不静默跳过** | 失败必须可见。禁止"抽帧全失败却当成功"、"被锁文件当不存在直接跳过"这类含糊表现。给稳定错误码。 |
| C7 | **不误伤并发** | 终止/清理**只作用于自己启动的进程或自己拥有的产物**。禁止按项目或按进程名全局杀。 |
| C8 | **不谎报验收** | CI 与合成素材通过**不等于**真机验收通过。见第六节。 |

---

## 三、必须遵守的工作流

### 3.1 每个任务都要有"先复现、再转绿"的回归测试

顺序不能反：

1. **先写测试**，在修复前运行，确认它**失败**并复现问题；
2. 再改代码让它转绿；
3. 在发布记录里写明"修复前是什么状态"。

只断言"终态正确"往往**测不出**缺陷（见 5.3）。要断言**可观测的差异**：
时延、文件是否真的消失、字节是否变化、错误码是什么。

### 3.2 接线要单独测

改了底层函数**不等于**改了行为。TSB-253 的教训：`extract_preview` 支持了
`cancel_check`，但**生产调用点没传**，缺陷照旧。

所以除单元测试外，还要断言**调用点确实传了参数**（可用 `inspect.getsource`
或 monkeypatch 记录 kwargs）。并用**反向验证**确认该用例不是空断言：
临时把接线删掉，确认测试失败，再恢复。

### 3.3 每轮必须写发布记录

新增 `docs/RELEASE_V<版本>.md`，至少包含：

- 基线 commit（`main` @ `<sha>`）
- 每个任务：**问题**（原缺陷是什么）→ **修复**（怎么改）→ **验收**（怎么证明）
- 验证结果表（用例数、环境）
- **范围声明**：本轮**未**改动什么、**未**删除或改写什么
- **尚未完成**项（诚实列出）

测试证据另存 `docs/validation/v<版本>/`。

### 3.4 更新回归清单

在 [docs/REGRESSION.md](docs/REGRESSION.md) 增加新用例映射（用例 → 任务号 → 守护的行为）。
需要人工确认的加进手动表 T01–T25。

### 3.5 文档中的数字必须与实际一致

发布记录与 README 里的 `N passed` 要与实际运行结果一致。
**改了测试就要同步更新数字**——历史上出现过文档写 329、实际 376 的不一致。

### 3.6 提交信息

沿用 `type(TSB-xxx): 中文摘要`，例如：

```
fix(TSB-253): 取消任务时终止在跑的媒体进程
test(scan): 通过正式 API 扫描深层源目录（TSB-254）
docs(TSB-246): v2.0.4 发布记录、回归清单与实机验收门禁
```

---

## 四、环境与验证命令

### 4.1 环境事实（Windows 开发机）

| 项 | 值 / 注意 |
| --- | --- |
| Python | 3.13.7（`backend/.venv`，已装全部依赖） |
| FFmpeg | 9.0.2 essentials，**项目自带**于 `bin/ffmpeg/ffmpeg-9.0.2-essentials_build/bin/` |
| FFmpeg 在 PATH？ | **否**。但 `locate_ffmpeg()` 会自动找到 `bin/` 下的副本，通常无需手动加 |
| `LongPathsEnabled` | `0`（即 Windows 默认，也是验收要求的场景） |
| Node / pnpm | 18.19.0（CI 用 20） |
| `pytest-timeout` | **未安装**，`--timeout=` 参数不可用 |
| ruff / flake8 | **未安装**，CI 也不跑（无需为 lint 折腾） |
| 视觉模型 | 历史 Windows 明文接入不作为生产验收。当前 Mac 使用被忽略的 backend/.env 配置 HTTPS，MODEL_MODE 默认 off，明文例外关闭；私有端点及 Key 不得公开 |

> **`.env` 只在从 `backend/` 运行时才会被读取。**
> `SettingsConfigDict(env_file=".env")` 相对当前工作目录解析，因此从仓库根目录
> 直接跑脚本会**静默丢掉** FFmpeg 路径与模型配置，表现为 `EXECUTABLE_MISSING`
> 或 `MODEL_UPLOAD_DENIED`（后者看似"未配置"，实际是没读到 `.env`）。
> 验收脚本一律先 `cd backend`。

### 4.2 后端测试

```powershell
cd backend
# 全套（含真实 FFmpeg 解码，约 95s）
.\.venv\Scripts\python.exe -m pytest -q

# 需要"缺工具/静默跳过即失败"时（CI media job 用的模式）
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest -q
```

要计数时用 junitxml，比抓 stdout 可靠：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --junitxml=tsb.xml
$x=[xml](Get-Content tsb.xml); $s=$x.testsuites.testsuite; if(-not $s){$s=$x.testsuite}
"tests={0} failures={1} errors={2} skipped={3}" -f $s.tests,$s.failures,$s.errors,$s.skipped
```

### 4.3 前端检查（四项都要）

```powershell
cd frontend
pnpm run typecheck    # tsc --noEmit
pnpm run lint         # eslint
pnpm run test         # vitest run（当前 96 passed）
pnpm run build
```

### 4.4 手动启动

```powershell
.\scripts\start.ps1          # 检查依赖 → 建 venv → 构建前端 → 启动并打开带令牌地址
```

### 4.5 CI 的两个作业与"强制媒体清单"

CI 有两个后端作业，职责不同，**不要混为一谈**：

| 作业 | 命令 | 特点 |
| --- | --- | --- |
| `backend` | 全量 `pytest`（忽略 2 个文件） | 跑得多，但**不设** `TSB_REQUIRE_FFMPEG` |
| `media` | **硬编码文件清单** | 设 `TSB_REQUIRE_FFMPEG=1`：缺工具或静默 skip 即失败 |

因此：**必须"不许静默 skip"的套件要写进媒体作业清单**，只放 `backend` 是不够的
（那里的 skip 会安静地通过）。清单在 `.github/workflows/ci.yml`。

两条容易踩的规则：

1. **新增平台专属套件时，要同步登记它的 skip 原因。**
   `conftest._EXPECTED_SKIP_REASONS` 只容忍"平台事实"类原因；未登记的原因在
   `TSB_REQUIRE_FFMPEG=1` 下会被当成意外 skip 而**让整个作业失败**（曾在 Ubuntu
   上真实发生：深路径套件的原因串与白名单不一致）。
   `tests/test_ci_skip_guard.py` 会自覆盖地扫描全部 `skipif` 原因并提前拦住这类问题。
2. **不要为了让 CI 变绿而放宽守卫。** 工具缺失（`ffmpeg not available`）必须继续
   失败；`test_tool_missing_skips_still_fail_the_job` 就是为此存在的。

本机模拟作业：

```powershell
# 媒体作业（把 ci.yml 的清单原样贴进来）
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_ffmpeg_integration.py ... 
# 平台 skip 是否会被误判（不需要 Linux 机器）
.\.venv\Scripts\python.exe tools\probe_skip_guard.py
```

---

## 五、容易踩的坑

### 5.1 长路径是**多个独立**兼容边界

Windows `MAX_PATH` 不只影响一件事，每处都要单独处理（v2.0.4 全链路修复）：

| 边界 | 处理方式 |
| --- | --- |
| FFmpeg/FFprobe 命令行 | `long_path()` 加 `\\?\`（UNC 用 `\\?\UNC\`） |
| Python `Path` 操作 | `safe_stat` / `safe_is_file` / `safe_mkdir` / `safe_unlink` / `safe_replace` / `safe_write_text` |
| 文件读写 | `safe_open` |
| Pillow | `safe_image_open`（自行读字节，绕开其内部 `open`） |
| OpenCV | 读字节 + `cv2.imdecode`（**不要**用 `cv2.imread`） |
| ffconcat 清单 | 单引号 + POSIX 分隔符；**不能**用 `\\?\`；不写 BOM |
| HTTP 提供产物 | Starlette `FileResponse` 自身也会对长路径抛错，只修存在性判断会把 404 变成 500 |

**关键细节**：`safe_*` 只在路径**不存在**时回退到扩展长度形式，
`PermissionError` 必须**原样抛出**——否则"文件被占用"会被误判成长路径问题
（这正是仅 Linux 暴露过的静默跳过缺陷）。

### 5.2 "FFmpeg 返回 0 但文件不存在"

FFmpeg 退出码为 0 却可能没写出文件（`image2` muxer 遇到无 `%03d` 的文件名会警告并退出 0）。
因此 `run()` 支持 `expect_output` / `expect_pattern`，在边界就校验产物，
并给出稳定错误码而非含糊表现：

`PATH_TOO_LONG` · `OUTPUT_NOT_CREATED` · `MEDIA_UNREADABLE` ·
`EXECUTABLE_MISSING` · `COMMAND_FAILED` · `CANCELLED` · `TIMEOUT`

### 5.3 取消必须**终止进程**，不只是改状态

TSB-253：协作式取消只在**条目之间**检查，正在跑的 FFmpeg 会跑完——
任务显示 `cancelled`，但项目锁被占、CPU 持续烧。

`cancel_check` 从任务一路传进 `ffmpeg.run()`。要点：

- 不传回调用 `subprocess.run`（快路径零开销）；传了才走 `Popen` + 轮询；
- 终止**只作用于自己启动的进程树**（C7）；
- `code == "CANCELLED"` 的异常记为**取消**而非 `failed`；
- **测试必须断言时延**，只断言终态测不出这个缺陷。

### 5.4 任务互斥必须在同一事务内，且要"两层"

仅靠进程内锁或前端禁用按钮挡不住并发：两个请求可在"检查"与"提交"之间交错。
互斥检查与 INSERT 必须在同一 `BEGIN IMMEDIATE` 事务内完成，冲突返回 409
并带冲突任务类型与 ID。

TSB-261 起改为两层，且**锁序固定为「先项目锁、后 DB 写事务」**：
先非阻塞获取 worker 持有的同一 `project_lock`（拿不到即 409），再进事务检查并插入。
反向获取会与 runner 死锁。判定互斥时用 `EXCLUSIVE_STATUSES`
（含 `cancelling`）而不是 `ACTIVE_STATUSES`。

### 5.5 代理时长要实测验收

发布前用 ffprobe 实测时长，与请求区间比对，容差 `max(0.25s, 10%)`。
要区分两类失败：**产物不可读 = 系统故障**（`failed`）；
**时长不符 = 请求问题**（`needs_review`）。失败时**不得覆盖**历史代理。

### 5.6 SQLite 不强制列长度

`String(32)` 存 69 字符在 SQLite 上不会报错，但更严格的后端或未来迁移会截断。
声明列宽时要按**实际生成值**校验（TSB-244 的教训）。

### 5.7 取消有"请求"与"完成"两个阶段

`cancelled` 是终态，只在 worker **真正退出并完成清理后**写；请求阶段写非终态
`cancelling`。把两者混为一谈会让项目在旧任务仍持锁时显得空闲。

- 三组状态集合不可混用（见 [DESIGN.md §8.1](docs/DESIGN.md)）：
  `ACTIVE_STATUSES`（可运行）、`CANCEL_REQUESTED_STATES`（该停了）、
  `EXCLUSIVE_STATUSES`（占着项目）。
- `finished_at` 只在真正停下后写；取消请求时**不写**。
- 排队任务的取消由 `converge_pending_cancellations()` 收敛，否则永远停在 `cancelling`。
- 恢复时不把 `cancelling` 改回 `running`，也不重跑已请求取消的编码。

### 5.8 清理只动"自己的暂存"，且不跟随链接

`staging_cleanup.cleanup_owned_staging()` 是正向白名单：只接受本项目工作区内、
形状为 `staging-*` / `staging/<asset>/<token>` / `proxies/staging/<token>` 的目录。
**绝不**跟随 symlink/junction —— 实测过一个跟随链接的实现会把项目外目录整个删掉。
`shutil.rmtree(ignore_errors=True)` **不能**作为"清理已完成"的证据。
v2.0.7 起，先通过 `create_owned_staging` 原子新建并登记 Job/项目/素材与目录身份；
已有目录或 owner 字符串不可授予删除权。形状必须精确到任务叶目录，父目录和所有
链接拒绝，枚举后重验身份，POSIX fd / Windows 固定目录句柄限定删除。
`cancelled` 仅表示工作停止，清理成功/失败/未知按 cleanup 统计展示。
重试只限停止任务的已登记 stage，不接收路径、不接管重启后的未知孤儿。

### 5.9 终止媒体进程要回收整棵树

`terminate()` 只作用于直接子进程，孙进程会存活。Windows 走 Job Object
（`KILL_ON_JOB_CLOSE`），POSIX 走独立进程组信号。作用域仅限本次启动，
**禁止** `/IM`、`pkill`、`killall` 这类全局终止。

---

## 六、关于"验收"的诚实要求

这是本项目最重要的一条纪律（C8）：

- **CI 全绿只证明自动化与合成素材路径通过**，不构成实机验收证据。
- 真机 + **8～10 段真实旅行素材**端到端验收与 60+ 视频 / 200GB 全量性能
  **尚未执行**。最近一轮记录见
  [docs/validation/v2.1-m5/](docs/validation/v2.1-m5/)（v2.1 M5 视觉理解）
  与 [docs/validation/v2.0.7/WINDOWS_REAL_FOOTAGE.md](docs/validation/v2.0.7/WINDOWS_REAL_FOOTAGE.md)
  （v2.0.7；v2.0.3/v2.0.4 的历史记录在同级目录）。
- 在这些记录补齐前，**不得**宣称已满足真实素材发布门禁。
- 不要用耗时外推 200GB 的性能结论。
- **模型 Mock 通过不等于真实云模型可用；多帧推断不等于原生视频理解；
  语义识别通过不等于 AI 导演决策完成。** 三者必须分开陈述。
- 任务书要求的结论措辞：证据不足时写**「代码级整改通过，发布门禁未完成」**，
  而不是「已修复」「已通过」。

---

## 七、提交前自检清单

```
[ ] 改动没有违反第二节任何一条硬约束
[ ] 新行为有"先复现、再转绿"的测试；修复前的状态已记录
[ ] 生产调用点确实接上了新参数（接线测试 + 反向验证）
[ ] 后端全套 pytest 通过，skipped 已解释
[ ] 前端 typecheck / lint / test / build 四项通过
[ ] 新增/更新 docs/RELEASE_V*.md（含范围声明与未完成项）
[ ] 更新 docs/REGRESSION.md 用例映射
[ ] 文档里的 passed 数字与实际一致
[ ] 提交信息用 type(TSB-xxx): 中文摘要
```

---

## 八、常用路径速查

| 内容 | 位置 |
| --- | --- |
| 后端入口 | `backend/app/main.py` |
| 数据模型 | `backend/app/models.py` |
| 媒体封装（长路径/错误码/取消） | `backend/app/ffmpeg.py` |
| 任务调度与取消 | `backend/app/services/runner.py` |
| 扫描 / 检测 / 抽帧 | `backend/app/services/indexer.py` · `detector.py` · `analyzer.py` |
| 代理与粗剪 | `backend/app/services/story_planner.py` · `roughcut.py` |
| REST 路由 | `backend/app/api/` |
| 数据库、`token.txt` | `<数据目录>`（`TRAVELSB_DATA_DIR`，默认 `backend/data`），**不对外提供** |
| 分镜图 / 代理 / 分析包 / 粗剪 | `<项目工作区>/<项目ID>/...` |
| 原片 | 用户素材目录，**始终只读** |
