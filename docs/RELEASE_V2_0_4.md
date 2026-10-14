# Travel Storyboard v2.0.4 — 工程收尾整改

> 基线：`main` @ `ff89e6f69cf8dd8f07e79b60d81e8fb1564965e0`
> 范围：缺陷修复与回归验证（不含 v2.1 的全素材 AI 导演设计）

本轮对应《Travel_Storyboard v2.0.4 工程收尾整改开发任务书》，关闭
TSB-241 ~ TSB-245。**TSB-246（Windows 实机与真实旅行素材端到端验收）尚未执行**，
详见文末。

---

## TSB-241 Windows 长路径全链路兼容（P1）

**问题**：`long_path()` 此前只用于 `probe` / `extract_frame` / `extract_preview`。
`detector` 的抽样 `-i`、`roughcut` 的素材输入与 concat 清单、以及
Pillow / OpenCV / `Path` 操作仍用普通路径，深目录会「部分阶段成功、后续失败」。

**修复**：在 FFmpeg 边界统一转换，并为每个独立兼容边界提供安全封装。

| 边界 | 处理 |
| --- | --- |
| FFmpeg / FFprobe 命令行 | 所有本地输入输出走 `long_path()`；UNC 走 `\\?\UNC\`；非 Windows 行为不变 |
| `Path` 操作 | `safe_stat` / `safe_is_file` / `safe_mkdir` / `safe_unlink` / `safe_replace` / `safe_write_text` |
| 文件读写 | `safe_open` |
| Pillow | `safe_image_open`（自行读字节后交给 Pillow，绕开其内部 `open`） |
| OpenCV | `detector._read_image` / `quality._imread_gray` 改为「读字节 + `cv2.imdecode`」 |
| ffconcat 清单 | `_write_concat_manifest`：单引号条目 + POSIX 分隔符，见下 |

**关键细节**：这些安全封装只在**路径不存在**时回退到扩展长度形式。
`PermissionError` 等真实错误原样抛出 —— 否则「文件被占用」会被误判为长路径问题。

**concat 清单是独立兼容边界**（经本机 FFmpeg 实测）：

- 双引号条目**不被** concat demuxer 当作引号，它会去找名为 `"C:/...` 的文件
  → `Impossible to open`
- 无引号条目在第一个空格处截断
- `file '<posix路径>'` 是唯一可用形式
- ffconcat 把 `\` 当转义符，原生 Windows 路径会被静默破坏
  （`C:\Users` → `C:Users`），因此统一输出 POSIX 形式
- 清单内**不能**用 `\\?\` 前缀（那是命令行专属），且不写 BOM

**可观测性**（消除「返回码 0 却没产出」）：

新增稳定错误码 `PATH_TOO_LONG` / `OUTPUT_NOT_CREATED` / `MEDIA_UNREADABLE` /
`EXECUTABLE_MISSING` / `COMMAND_FAILED`。`run()` 支持 `expect_output` /
`expect_pattern`，在边界就校验产物。

`detector` 另修一处静默成功：原先抽样帧**全部**读不出时会返回单个 `single`
镜头，即「静默跳过全部图片后当作单镜头成功」。现在抛 `MEDIA_UNREADABLE`；
部分失败记 warning。

**验收**：本机为 Windows 且 `LongPathsEnabled=0`（即验收要求的场景）。
`tests/test_long_paths.py` 覆盖 ≥270 字符路径下的抽帧、检测、
扫描→检测→抽帧→HTTP 预览、粗剪发布，并断言原片字节不变。

---

## TSB-242 代理任务创建互斥与幂等（P1）

**问题**：`/projects/{id}/review-requests/proxies` 直接新增 Job 并
`runner.submit`，未走 `/jobs/{job_type}` 的项目锁与互斥检查。快速重复点击
可建立多个排队 Job。

**修复**：新增 `services/job_service.create_project_job()`，两个入口共用。

- 互斥检查与 INSERT 在**同一 `BEGIN IMMEDIATE` 事务**内完成。
  仅靠进程内锁或前端禁用按钮都不够：两个请求可在「检查」与「提交」之间交错。
- 冲突返回 409，`detail` 含 `error_code=JOB_CONFLICT`、`active_job_type`、
  `active_job_id`；被拒绝的请求不新建任何 Job 行。
- 代理入口请求/响应格式不变（仍 202 + `JobOut`），`config_snapshot` 一致。

**验收**：6 线程同时创建 → 恰好 1 个 202、其余 409，库中仅 1 行。
5 种重活两两互斥；不同项目互不阻塞。

---

## TSB-243 代理视频实际时长验收（P1）

**问题**：只校验「文件非空且宽>0 且时长有限」，错误时长的代理仍被标记
`proxied` 并发布。

**修复**：新增 `_verify_proxy_duration()`，用 ffprobe 实测时长与请求区间比对。

- 容差取「绝对 0.25 s」与「相对 10%」中较宽者：极短请求（0.6 s）只有几帧，
  编码器取整就会偏移；长请求按比例约束。
- 时长不符抛 `ReviewValidationError(DURATION_MISMATCH)` → 请求转
  `needs_review`（请求/素材本身需人工确认），**历史代理引用原样保留**。
- 明确区分两类失败：产物不可读（`width=0`/时长非有限）是**系统故障**，
  仍为 `ValueError` → `failed` / `PROXY_FAILED`；只有时长不符才是请求问题。

---

## TSB-244 分析包版本标识与模型一致（P2）

**问题**：`ReviewPackage.version` 声明 `String(32)`，实际生成
`<job_id 或时间戳>-<32 位 hex>`，最长 69 字符。

**修复**：列长度改为 `String(80)`。SQLite 不强制长度所以此前未暴露，
但更严格的后端或未来迁移会截断或拒绝。既有数据与 version 值未改动。

---

## 验证结果

| 项目 | 结果 |
| --- | --- |
| 后端测试 | **329 passed**（基线 258 + 新增 71） |
| 新增测试文件 | `test_long_paths.py`(29)、`test_job_mutex.py`(20)、`test_proxy_duration.py`(22) |
| 本机环境 | Windows / Python 3.13 / `LongPathsEnabled=0` |
| 回归 | 无既有用例被破坏 |

新增测试全部**先于修复编写并复现问题**后转绿；`test_review_proxies` 的
probe 故障用例还守住了一处分类判断（不可读产物属系统故障，非请求问题）。

---

## 尚未完成：TSB-246（发布门禁）

**本轮不包含，也不能由 CI 代替：**

- Windows 实机 + **8～10 段真实旅行素材**端到端验收（RT-18）
- 真实长片 / 横竖屏 / 音轨混合素材验收（RT-19）
- 60+ 视频 / 200GB 全量性能验收

`docs/validation/v2.0.3/WINDOWS_REAL_FOOTAGE.md` 中的 RT-18 / RT-19 仍为
「未执行」。按任务书要求，**在这些记录补齐之前，v2.0.4 不应宣称已满足
真实素材发布门禁**。

CI 全绿只证明自动化与合成素材路径通过，不构成实机验收证据。

---

## 范围声明

本轮**未**改动：镜头检测算法、模型 Prompt、故事线规划、粗剪叙事逻辑。

**未**迁移或改写任何既有产物路径与数据库引用；**未**删除或修改用户原片、
历史分析包、人工评分/备注/裁剪、ReviewRequest 历史、EditPlanShot 与已发布代理。
