# v2.0.7 取消竞态与暂存安全整改

基线：`main@f7ab58155e3554952b986aca1d2e94095158efb6`（拉取后确认未变化）。
开发、分批提交和推送均在 `main`；不创建任务分支，不改写历史。
最终完整 HEAD 与对应 CI run 由交付回复及该 run 的 `media-platform.json`
据实提供。后续证据索引见 `validation/v2.0.7/CI.md`。

## 问题、修复与证据

| 任务 | 修复前问题 | 修复及验证 |
|---|---|---|
| TSB-271 | 成功/部分失败 CAS 输给取消后，任务遗留 `cancelling` | 唯一 `finalize_job` 路径，4 次有界 CAS/重读；已提交终态不改时间或错误。任务 finally 返回后、项目解锁前收敛。成功、部分失败、真实失败均覆盖两种先后顺序，2,000 次受控竞争全部收敛。底层 update_job 即使传显式 expected_statuses 也拒绝覆盖终态/cancelling 倒退，4 个用例先失败再通过。DB I/O 和无法收敛记录 `STATE_CONVERGENCE_FAILED`、Job ID，保留 DB 独占状态，不能声称释放已确认。 |
| TSB-272 | 顶层目录白名单误认 history/父目录；`resolve()` 可把内部链接变为另一个 stage；owner 字符串只是日志 | 精确匹配三种叶目录；创建时 `mkdir(exist_ok=False)` 登记真实 Job/项目/素材归属及目录、祖先 dev/inode。拒绝接管已有目录。删除时重验原始段、所有权、目录身份和每一级链接；POSIX 用 no-follow dir_fd，Windows 用禁止删除/改名共享的目录句柄。预扫描拒绝链接，枚举后再次验证身份；只 unlink 文件和 rmdir 空目录，不递归 rmtree。 |
| TSB-273 | 分析写事务只识别 `cancelled`，已接受 `cancelling` 仍提交 | 共享 `raise_if_cancelled_in_session` 在同一 SQLite 写事务读取 `CANCEL_REQUESTED_STATES`；回滚只回收本轮唯一且无永久引用的新 JPEG，使用长路径安全删除。验证取消先提交、分析写锁先取得、历史人工状态/计划/复核引用不变。代理、分析包也统一事务内判断。 |
| TSB-273 审查扩展 | 粗剪最后一次轮询到发布间仍有窗口，固定文件名覆盖历史 | 定向测试先复现：取消后仍发布、旧文件字节改变。新粗剪使用独立文件名，发布和 Job 路径在同一写事务；失败仅回收本次文件。原 REST 地址读取最新已提交引用，旧固定文件名兼容回退。历史粗剪不删除/覆盖。 |
| TSB-274 | 只保存失败统计，进度替换 stats 后丢失；UI 固定声称已回收 | 每个 stage 成功/失败均聚合保存，单次成功不掩盖其他残留；进度更新和统计合并使用写事务保留 cleanup。UI 显示成功/失败/未知，有限项目相对路径。新增认证、项目/Job 限定的 cleanup-retry（不接收路径），仅停止的任务、项目无活跃任务、进程内真实创建记录才能重试；每次重验安全边界并实测残留。 |
| TSB-275 | rglob/stat/unlink 在深路径下失效，旧逻辑也遍历 stage | 单独的孤儿 JPEG 回收器，只进入 frames；不进入任何 stage。项目锁+DB 写锁固定引用快照；检测活跃时延后回收。POSIX fd/Windows 长路径和固定目录句柄遍历，不跟随 symlink/junction；只删过宽限期、无引用的普通 JPEG，失败/拒绝可观察且可重试。 |
| TSB-276 | 缺确定交错、安全反向验证及完整证据 | 新增 6 个后端套件共 56 个参数化用例，前端新增 6 项。全部加入 Ubuntu/Windows 强制媒体清单。Windows 媒体作业验证 `LongPathsEnabled=0`，上传 JUnit 和精确 SHA/版本/策略；文档提交也触发 CI，以便最终 HEAD 有独立运行。 |

基线 P1 定向测试：**11 failed / 7 passed**，见 `p1-before.txt`。
TSB-273 补充复现：**2 failed / 4 passed**，见 `tsb273-before.txt`。
粗剪扩展：见 `publication-before.txt`。P2 复现见 `tsb274-before.txt`、
`tsb275-before.txt`。反向撤掉创建者登记或事务内取消接线，两次均按预期失败；
正确实现随后恢复并验证，见 `reverse-*-wiring.txt`。

## 线性化与状态语义

取消事务先提交 → 分析/代理/包/粗剪写事务发现 `cancelling`，不得发布新引用。
写事务先取得 SQLite writer 并提交 → 内容提交先于取消被数据库接受，是合法顺序。
终态 CAS 先成功 → 取消 API 返回 409；取消 CAS 先成功 → worker 的最终收敛为
`cancelled`。真实失败遇到尚未终态的取消按请求收敛，保留原失败 error/failures。

`cancelled` 表示工作已停止，**不保证暂存已全部删除**。清理失败仍保留残留数、
字节、`CLEANUP_FAILED`/`STAGING_NOT_OWNED` 与重试结果；不能显示为资源已回收。

## 验证

本机 macOS arm64 / Python 3.12.14，真实 FFmpeg 7.1 / FFprobe n4.4.1：

| 检查 | 结果 |
|---|---|
| 后端全量，`TSB_REQUIRE_FFMPEG=1` | **468 passed / 0 failed / 44 skipped**，512 项；44 项均 Windows 专属平台事实，缺工具 skip 仍失败 |
| 前端 typecheck / lint / build | 全部通过 |
| 前端测试 | **20 passed** |
| 受控终态 CAS | **2,000 次**，无遗留 cancelling |
| 反向接线验证 | 两次均失败，恢复后通过 |
| Windows / Ubuntu CI | a28820d 的 7/7 成功：Windows 媒体 442 passed / 0 skipped，Ubuntu 398 passed / 44 平台 skipped；最终交付 HEAD 的对应 run 见交付回复 |

后端命令：指定 `TRAVELSB_FFMPEG_PATH`、`TRAVELSB_FFPROBE_PATH`，执行
`TSB_REQUIRE_FFMPEG=1 python -m pytest -q -o addopts='' --tb=short --junitxml=... backend/tests`。
前端使用 Node + pnpm 9，执行 `typecheck`、`lint`、`test`、`build`。
证据只包含隔离合成素材/模拟 DB；不含真实素材、令牌或用户原片路径。

全量首次运行发现两处原测试的调度缺口，未放宽断言：回调/DB 测试禁止自动执行
空分析包，且明确断言取消 200；恢复测试在 submit 注册锁内读取 Future，而非以任务
进入事件冒充 Future 已登记。其后相关 30 项通过，全量转绿。首轮 Windows 强制媒体作业进一步发现旧深路径粗剪
测试仍找固定 roughcut.mp4（产品已改为保留历史的独立路径）。测试改为核对精确 Job
提交引用，保留真实 ffprobe 解码，并加强 >=270 路径、时长、HTTP Range 和完整标识断言；
不 skip、不放宽断言。失败 run `38583030723` 为 437 passed / 1 failed，LongPathsEnabled=0；
该 run 其余 6 个作业成功。后续 a28820d 的 7/7 全部成功（run 38583888355），见 CI.md。
文档 HEAD 893e069 的再次验证在 Ubuntu/Python 3.13 暴露 TC-29 旧测试将终态 DB
快照误当作项目已解锁。按设计终态必须先于解锁，期间返回 409 正确。该测试改为两个
Event 清理阶段 + worker Future 完成后再尝试；加强两阶段 409 无新增行、取消中不写
finished_at、最终严格 202，取消固定 sleep。失败证据见 terminal-before-unlock-test.txt。

## 范围与尚未完成

- 不变更 SQLite schema、检测边界身份、人工评分/收藏/备注/用途/裁剪、复核历史、
  EditPlanShot、分析版本号/算法与导出格式。版本标签统一到 2.0.7。
- 不访问/删除/改写真实源素材；不全局杀媒体进程；不删除历史 JPEG、代理、分析包、粗剪。
- stage 创建者登记仅在进程内有效；重启后不会通过 owner 字符串或统计路径接管孤儿目录。
  重试无登记返回 409/STAGING_NOT_OWNED，保留残留以供后续单独、可审计的孤儿治理。
  frames 的过期孤儿清理是不同入口，不能用于广泛清除 stage。
- 数据库终态确认失败时线程有界退出，DB 状态继续挡住同项目任务并记录错误，需要恢复/运维处理。
- **TSB-277 Windows 8～10 段真实旅行素材验收未执行**。
- **60+ 段 / 200GB 全量性能未执行**，不依据合成素材或样本耗时外推。

代码 SHA a28820d 的 CI 已达标，最终交付 HEAD 对应 CI 在交付前另行核对。结论：**代码整改完成，真实素材发布门禁未完成**。
