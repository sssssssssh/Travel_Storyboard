# Travel Storyboard v2.0.1 稳定性整改交付记录

日期：2026-10-13（Asia/Shanghai）。基线：main@96fccef8a52deccb0103981448579582afa55b6c。
开发前通过 GitHub 与 git fetch 确认远端 main 未增加提交。交付分支：codex/v2.0.1-stability。

**结论：代码整改已交付，本地自动化及全部 7 个 CI Job 通过；正式 Windows 实片验收尚未完成。**
不能将生成素材或 CI 测试替代 5～10 段真实旅行素材验收；RT22～24 的真实素材部分待执行。

## 实现与提交关联

| 任务 | 交付 | 主要提交 |
| --- | --- | --- |
| TSB-201 | 独立评分映射、首轮每素材一段、预算后截断、真实覆盖率与缺图检查 | 4101985 |
| TSB-202 | 自定义工作区粗剪、媒体权限边界、完整/不完整身份、临时产物发布 | 02e76b3、5c66a98（低 FPS 尾帧） |
| TSB-203 | 终态条件更新、取消不覆盖、同项目恢复队列、停止期互斥、包重放幂等 | 2bcccd6、7321c6f、50e5627 |
| TSB-204 | 有媒体/分析状态时禁止改目录，整批拒绝并保留旧引用 | 435aea5、7321c6f |
| TSB-205 | 头/中/尾指纹、全文件校验选项、逐对象快照与检查点、参数冻结、失败重试 | 9d2f61e、3a5b080、a70377c |
| TSB-206 | 10 镜头草稿恢复、dirty 保护、保存失败保留、片段出点/有限数校验 | 9149eba、9997a40 |
| TSB-207 | 独立 run_id、发布检查、DB 失败回滚、旧图保留、孤儿宽限期清理 | a3bb6a0 |
| TSB-208 | Ubuntu/Windows 强制 FFmpeg/FFprobe CI、真实合成链路、故障注入、文档 | 5c66a98 及本记录；Windows 实片待执行 |

无新增数据库列/表；沿用 Asset、Segment、Frame、Job JSON 字段。旧位置 JPEG 仍可访问。
历史迁移备份使用 SQLite Backup API，避免遗漏 WAL；已有合法 detected_start_sec=0 不再
因其他列迁移被回填成用户裁剪起点。旧位置式 Job 检查点保守重新核验，不沿序号跳过。

## 本地验证结果

- 环境：macOS 15.7.7 / Apple M1 / 8 GiB RAM / Python 3.12.14 / pnpm 9.15.9。
- FFmpeg：imageio-ffmpeg 提供的本地 arm64 二进制；FFprobe n4.4.1；均实际执行。
- 后端全量：163 passed，0 failed / 0 error / 0 skipped；JUnit 实测约 6.7 秒。
- 前端：5 项实际组件/接口测试通过，typecheck / lint / production build 全通过。
- 缺工具反向验证：TSB_REQUIRE_FFMPEG=1 时故意指定不存在的两个可执行文件，测试以
  error 和退出码 1 结束，没有静默 skip。媒体 CI 对任何 skipped report 同样返回失败。
- 综合链路：3 个生成视频，总输入 9 秒；H.264 横屏带 AAC、竖屏静音、3 fps 4K HEVC。
  原片/工作区均含中文及单引号。扫描、检测、再次检测跳过、分析包、二轮代理、保存计划、
  粗剪 API 全链路通过；库内 3 素材均覆盖。粗剪实测约 4.933 秒，计划 5 秒，误差 <0.25 秒。
- 缓存：强制检测后 JPEG 数与 Frame 行数一致；收藏/裁剪/Segment ID 保留。抽帧失败、
  发布失败、DB 提交失败时旧帧均可读；清理 OSError 不回滚新版本；孤儿 JPEG 宽限期
  测试和外部文件保护通过。已提交包重放只产生一条 ReviewPackage 行。
- 内存：一次全量 pytest 的 Python 父进程最大 RSS 320,520,192 字节（约 306 MiB）。
  此值不含所有 FFmpeg 子进程，不是 200GB 实片负载的整机峰值结论。

3 个**生成测试视频**哈希证据（前后完全一致，真实旅行原片仍待验收）：

| 测试源 | 分析前/后 SHA-256 |
| --- | --- |
| 横屏 H.264 + AAC | `e61fb5cddf8527bcdf0b6a7aa04aece3507a5e770d7770c906619f4b0583fa7e` |
| 竖屏 H.264 静音 | `df51e67e2c8df818eff40d1e5bdf13dae50ed795eb45de6efc43b0315455c6fa` |
| 4K HEVC 3 fps | `2b5a1dc235d2c64e368c7544bdaa7efe33e90975e52a0634e6f5828a7b6c038c` |

哈希也写入媒体 CI 的 JUnit properties；生成文件的编码版本不同可能产生不同哈希，
断言比较的是同一次运行前后。报告不包含访问 Token、数据库或用户真实素材路径。

## CI 证据

[CI Run 38411377215](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38411377215)，
验证代码提交 `a70377ccaea7f109a33fe0b043d810ac9dc58f01`；**7/7 Job success**。
发布记录文档的后续提交没有改变已验证的代码；工作流仅文档推送免重跑。

| Job | 实际结果 | 媒体工具 |
| --- | --- | --- |
| Required FFmpeg (Ubuntu) | 78 passed / 0 skipped，7.65 秒 | ffmpeg/ffprobe 6.1.1-3ubuntu5 |
| Required FFmpeg (Windows) | 78 passed / 0 skipped，19.37 秒 | ffmpeg/ffprobe 9.0.2 essentials |
| 后端 Ubuntu/Windows × Python 3.11/3.13 | 4/4 Job success | 普通单测，OTIO 验证依赖安装成功 |
| Frontend | 5 tests + typecheck + lint + build success | pnpm 9 / Node 20 |

已读取两平台 Job 日志，确认安装工具、打印版本、执行真实合成/抽帧/HEVC/音频/粗剪和
缓存故障注入，而非仅观察绿灯。媒体 JUnit 证据：
[Ubuntu report](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38411377215/artifacts/11822340791)、
[Windows report](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38411377215/artifacts/11823170655)。
这证明 Windows CI 的默认/自定义工作区与中文路径生成素材链路，仍不等于用户真实原片验收。

## 已知限制与未执行项

- 无用户 Windows 机器和旅行原片：5～10 段、10 分钟以上、真实 4K/HEVC、VFR/HDR、
  真实素材三段 SHA-256、200GB 负载峰值内存/缓存增长尚未验收。中文路径和 4K/HEVC
  已用合成视频替代自动化验证，但真实场景仍标记“未覆盖”。详细步骤见 REGRESSION.md。
- 真实视觉模型服务未调用；direct_analyze 的模型响应使用 stub 验证协议和引用合法性。
  此版本只修复抽样/覆盖率，不承诺 AI 自动完整成片。
- 默认快速指纹只读头/中/尾；其他位置同大小同时间戳修改可能漏检。需要严格内容校验时，
  扫描/检测选 full_hash=true，强制重分析用 force=true，会增加源文件 I/O。
- 检测边界稳定时可匹配并保留人工信息；边界明显变化时无法保证每个旧片段可靠匹配。
  升级前应备份项目；强制更换检测参数后需复核计划。原片本身只读。
- 进程硬退出后发布而未提交的 JPEG 会留存至宽限期，下一次分析前/后回收；不是文件系统
  与 SQLite 的跨介质原子事务。取消当前 FFmpeg 调用也可能需要等它返回。
- OTIO 仍为实验性，已有参考库 round-trip 测试通过，真实 NLE/剪映兼容未覆盖。

故障示例：合成 3 fps HEVC 粗剪原实测 4.667 秒而计划 5 秒，回归断言失败；已通过尾帧
延续修复。原 SQLite 主文件复制缺失 WAL 内表，备份回归失败；已改为 Backup API。
完整失败隔离示例见 test_checkpoints.py 中单文件失败、重试及逐项失败记录去重用例。

## 手动回退

1. 取消任务并退出服务，等待所有 FFmpeg/工作线程结束；不要在写入时替换数据库。
2. 使用 SQLite Backup API 保存完整数据库，并另外备份整个项目缓存根（默认与自定义
   output_dir 都要包含）；保存配置。不要仅复制活跃的 .db 主文件。
3. 在备份基础上恢复升级前 checkout（基线 96fccef），重新安装匹配依赖并构建前端。
   本版没有新增 schema；现有 JSON 多余字段不阻碍旧版读取。若要精确恢复检查点和帧，
   应整体恢复同一时间点的 DB 与缓存快照，不混搭不同版本。
4. 恢复数据库时服务保持关闭，处理旧的 db-wal/db-shm，重新启动后验证项目数、Asset 数、
   Segment 数、收藏数、EditPlanShot 数和代表帧访问。原视频不参与删除或迁移。

正式发布仍需按 REGRESSION.md 完成 Windows 旅行实片项目验收并填补未覆盖记录。
