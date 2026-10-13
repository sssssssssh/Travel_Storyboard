# Travel Storyboard v2.0.2 稳定性补充整改交付记录

日期：2026-10-13（Asia/Shanghai）。任务基线 a70377c；工作区从 a30b4e1 继续（仅多上一版
发布文档），未覆盖 main 或已有工作区。交付分支 codex/v2.0.2-stability。

**代码整改与本地自动化已完成；正式整体验收未通过，原因是 TSB-226 Windows 真实
旅行素材验收未执行。不能据此批准完整 200GB 正式运行。**

## 已复现问题与修复

| 任务 | 实际复现证据 | 结果/关联提交 |
| --- | --- | --- |
| TSB-221 P0 | Event 控制抽帧中途编辑后，favorite/trim/story_role 被旧对象覆盖；POST note 本来独立存表保留，但 PATCH note 不可读 | 提交时最新行、算法字段白名单、统一备注写入：323e844 |
| TSB-221/224 | 旧 plan/review/annotation/收藏/裁剪/用途/确认镜头被重新分段硬删除；模型返回后失效 ID 仍可持久化 | needs_review 保留旧行/帧；引用验证与写入同事务：b2991a0、674e799 |
| TSB-222 | 在创建接口禁止调用 manifest 的回归失败，证明同步遍历/读盘路径；原 full 签名函数多次调用 | 后台清单、一次全读/显式二次严格复核、累计进度与取消恢复：7ffdd5f |
| TSB-223 | 强制扫描 fake FFprobe 计数仍为一次；force 没有下传 | force 下传，新任务重探测，同 Job 恢复跳过已确认项：7ffdd5f |
| TSB-224 | FFprobe 期间原文件修改后仍写入新 duration；根目录不可读被误报为空成功 | savepoint 回滚派生元数据、逐项明确失败：7ffdd5f |
| TSB-225 | 601 秒 1 fps 合成源尾部请求 600.5 秒时无帧，真实 FFmpeg 报错 | CFR 末帧时间夹取；普通矩阵和强制媒体矩阵加入新测试：66a04cf |
| TSB-226 | 当前会话未提供可用 Windows 环境/8～10 段旅行原片 | 未执行，附待验收表；不标通过 |

两个旧持久化测试使用虚构 seg-x/seg-y，增强当前引用复查后暴露失败；已改用 fixture
中的真实 Segment ID，保留创建/去重断言，并新增不存在/已失效 ID 必须拒绝的测试
（97a4c9c）。这属于测试数据契约修正，没有放宽业务验证或取消媒体测试。

## 本地测试与可复验证据

- macOS 15.7.7 / Apple M1 / 8 GiB RAM / Python 3.12.14 / pnpm 9.15.9。
- FFmpeg arm64 7.1、FFprobe n4.4.1；真实调用探测、抽帧、编码、代理、音轨、粗剪。
- 全量后端：204 passed，0 failed / 0 error / 0 skipped；JUnit 11.73 秒。
- 前端：8 tests；类型检查、lint、production build 均通过。
- 并发四种人工字段及抽帧/发布/DB/取消故障保留测试通过；新增计划在抽帧期间保存后
  仍保护旧 ID。SQLite writer reservation 在验证阶段的独立连接竞争测试通过。
- 读取计数：3 MiB 文件 full 模式只读 3 MiB，同 Job 重放仍为 3 MiB；检测非严格模式
  一个全读，严格模式两个；4 MiB 两文件中断恢复累计读 5 MiB，已完成的 1 MiB 文件
  只读一次，未完成文件重读。累计字节不是整机磁盘 I/O，不含 FFmpeg/快速采样。
- 后台首块被 Event 暂停时 HTTP 仍立即 202；本机实测创建约 4.05 ms。这是路径/协议验证，不是 200GB 性能基准。
- 十分钟中文/空格路径替代验证：160×90、1 fps、H.264 + AAC、601 秒生成视频；
  分镜、尾帧、代理带音轨与授权 API 访问通过。真实 10 分钟旅行视频仍未覆盖。
- 601 秒**生成源**分析前后 SHA-256 一致：`9b41765b43ff2ac35f581e3880ff3c2296aabe6d8b8ca50d077a4f67a57ae14f`。
- 一次 197 项完整 pytest 的 Python 父进程最大 RSS 327,155,712 字节（约 312 MiB），
  不含所有 FFmpeg 子进程，也不是 200GB 整机峰值。最终新增引用测试另见完整测试统计。
- 应用媒体流程只读源；故障注入只故意修改临时测试文件模拟外部写入，未访问用户历史库或真实旅行视频。

## CI

[最终 CI Run 38423597108](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38423597108)
验证 `ebcd25da6d5eafcd60339e776169a6a523a5f0d5`：**7/7 Job success**。
后续交付文档提交只改 README/docs，不改变已验证代码，文档推送免重跑。

| Job | 实际结果 | 工具/环境 |
| --- | --- | --- |
| Ubuntu 强制媒体 | 119 passed / 0 skipped，22.19 秒 | FFmpeg/FFprobe 6.1.1-3ubuntu5，Python 3.11 |
| Windows 强制媒体 | 119 passed / 0 skipped，23.31 秒 | FFmpeg/FFprobe 9.0.2 essentials，Python 3.11 |
| Ubuntu/Windows 后端 × Python 3.11/3.13 | 4/4 Job success | 并发、哈希、身份规范化、引用/计划等新用例均执行 |
| Frontend | 8 tests + typecheck + lint + build success | pnpm 9 / Node 20 |

已读取两平台 Job 日志，确认版本检测、真实 FFprobe force 计数、601 秒音轨代理、
并发故障与旧请求代理拒绝用例都执行；必跑媒体没有 skip。JUnit artifacts：
[Ubuntu](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38423597108/artifacts/11827088060)、
[Windows](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38423597108/artifacts/11827352491)。
缺工具、合成失败或媒体 skip 仍失败；真实旅行素材不因这份 CI 结果被写为通过。

Windows CI 曾实际暴露两项平台问题：Python 3.13 的 path-stat/handle-fstat ctime
语义不同导致误报源变化，以及恢复用例未固定 created_at 时 Windows 时钟相同导致
UUID tie-break 顺序不同。前者按 Windows birthtime 统一身份（大小/mtime/dev/inode
校验保持），后者用显式持久化时间构造确定顺序，保留原串行/终态断言（0c2da20）。
跨 stat 时间语义依据 [CPython #157671](https://github.com/python/cpython/issues/157671)；
新增测试同时证明 Windows creation identity 等价、POSIX change-time 差异仍被拒绝，
大小/mtime/设备/inode/birthtime 任一改变仍拒绝。这些失败没有被 rerun/skip 掩盖。
旧 pending 复核请求若引用 needs_review/不存在/不匹配镜头，不编码新代理；原请求及已有代理保留，并明确失败提示（ebcd25d）。
不可读取的视频现在进入失败项，不会被目录扫描静默遗漏（2e45ade）。Ubuntu CI 优先
验证已安装工具，缺失时安装；安装下载失败或工具缺失仍阻止媒体 Job 通过。

## 不变量与兼容/回退

没有 schema 新表/列；stable Asset/Segment ID 继续使用。UserAnnotation 和所有历史计划/
复核引用保留；不匹配且受保护镜头变为 needs_review。最新 trim 有效时保留，轻微夹取
留审计，不能相交时保留旧选择并告警。用户从当前检测镜头更新计划，旧引用没有自动映射。
旧签名/检查点缺字段时保守重验；2.0.2 检测版本会使旧分析首次按新协议重算，人工字段
在提交阶段读取最新值。完整校验默认只全读一次，新任务不会凭 size/mtime 复用全哈希。

回退时先停止应用和工作线程，使用 SQLite Backup API 备份包含 WAL 的数据库，再备份
默认/自定义项目缓存和配置。恢复到同一时点的 DB+缓存，不能混搭；服务关闭时处理对应
db-wal/db-shm。回到 a30b4e1 并重装/构建对应依赖后，检查项目/资产/镜头/收藏/计划数与
旧图片访问。没有新增 schema，但旧软件不能提供新并发保护，故仅作为临时回退。
源目录不参与移动、删除或恢复写入。机器路径、Token、数据库未进入公开验收报告。

## 未关闭问题与 Windows 真实素材待验收

| 维度 | 结论 |
| --- | --- |
| 代码版本 | 运行代码 `ebcd25da6d5eafcd60339e776169a6a523a5f0d5`；对应 CI 见上节 |
| 单测 | 本地通过，最终 CI 7/7 通过 |
| P0 人工并发一致性 | 本地通过，Event/SQLite 竞争测试及故障回滚证据 |
| 全量哈希异步化 | 本地通过，禁止请求读盘与真实后台暂停/取消测试，读取字节计数 |
| Force 增量语义 | 本地通过，fake + 真实 FFprobe 调用计数，恢复跳过 |
| Windows 真实旅行素材 | **未执行/未通过验收**；合成媒体 CI 不能替代 |
| 完整 200GB 正式运行 | **否**；先完成 8～10 段真实素材正确性及性能验收 |

待选样本：20～60 秒、2～5 分钟、10 分钟左右各若干，横竖屏、4K、HEVC/H.264、
手机 VFR、环境音/静音、中文/空格路径；至少一段有历史收藏和裁剪。无某类素材标未覆盖。
先备份 SQLite（Backup API）和工作区，记录至少三段原片 Get-FileHash SHA256。
按“快速扫描 → 检测 → 收藏/备注/用途/裁剪 → 普通及强制重分析与并发编辑 → 取消/
重启/故意损坏文件 → 完整/严格校验 → 分析包/二轮代理/粗剪”执行。确认 ID/人工字段/
计划/媒体引用不丢，全部源哈希前后一致，然后才扩大到 60+ 素材。

| 应记录项 | 本轮真实 Windows 数据 |
| --- | --- |
| Windows、CPU/GPU、RAM、磁盘与 FFmpeg 版本 | 未获得 |
| 8～10 段原片规格、总输入时长/体积 | 未获得 |
| 首轮/重跑扫描、检测、哈希耗时 | 未执行 |
| 整机峰值内存、工作区新增大小、磁盘读盘量、分析包体积 | 未执行 |
| 成功/失败/跳过计数与故障隔离、重启恢复 | 未执行 |
| 实片源哈希对比、默认/自定义工作区播放器检查 | 未执行 |

快速模式存在未采样区域的已知限制。处理前后轻量身份检查可检测常见写入，不能承诺
对刻意伪造全部元数据的外部修改绝对原子；有此需求使用严格复核且避免外部同时写源。
VFR/HDR 真实视频、输出磁盘性能/容量及整机峰值必须在用户 Windows 样本阶段核验。
对源被占用/删除、目录权限、磁盘空间或 FFmpeg 失败，UI 显示具体失败和恢复提示，旧
分析仍在；修复后再次扫描/检测，不通过删除原片或重建整个库绕过失败。
