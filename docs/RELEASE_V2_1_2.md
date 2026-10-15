# v2.1.2 — M5 收尾整改

基线 main@f68f95446d8ada734843218fc974b321a905e1b4。A=TSB-320，B=321/322，C=323/324；逐批提交、全量回归和同 SHA CI。A 阶段应用仍为 2.1.0，B 批统一真实发布目标为 2.1.2；不更改 ANALYSIS_VERSION=2.0.2 或 Prompt/schema 版本。

## A / TSB-320

问题：模型和 DB 发布成功，清理 PermissionError 已写 stats.cleanup.ok=false，但 worker 自行写 succeeded，Runner 因终态不可逆无法再变 partial_failed；UI 仍出现绿色 succeeded。清理报告持久化异常也只记日志，业务失败对象的旧快照还可能抹掉清理审计。

修复：成功/部分成功 worker 只更新统计；finally 完成用量和清理后由原 Runner/finalize_job/CAS 判定终态。在同一个 SQLite writer 中写 cleanup 健康和结构化 cleanup failure，保留首次故障及单独重试的 resolved_at/resolution。旧 JobFailures 快照合并保留清理事实。报告无法保存则传播 CLEANUP_REPORT_FAILED，不能只记录日志后成功。取消仍优先 cancelled，未修改合法终态集合、项目互斥或媒体进程树机制。

UI：旧 succeeded+失败/未知清理也显示黄色强警示；未知残留数量不写 0，不可重试时明确人工处置。视觉面板直接提供原有安全重试入口，成功显示“清理已补救”，不改终态、不重跑模型。cancelling 持续轮询到真正终态，修复原来收到取消中状态后停轮询的问题。

红绿：最初新增 12 项后端测试，修复前 10 failed/2 passed；新增前端 2 项及强化旧成功显示用例，修复前 3 failed/22 passed；轮询额外 1 项修复前失败。修复后并补充 6 项重启/链接/兄弟目录的 durable API 边界，共 18 项新后端通过。反向断开 cleanup failure 接线，成功+清理失败的测试重新失败，已恢复。原先直接调用 worker 并断言终态的测试改为走 Runner；并发 repository 用例仍故意绕开项目锁，断言 worker 保持 running 且各发布一次，不弱化唯一 current/审计断言。

| A 本地检查 | 结果 |
|---|---|
| macOS arm64 / Python 3.12.14，TSB_REQUIRE_FFMPEG=1 | collected 807 / passed 763 / skipped 44 / failed 0 / errors 0 |
| 前端 typecheck/lint/test/build | 全部通过；43 tests passed（npm 等价脚本，CI 仍 pnpm） |

Windows 专属跳过逐项见 validation/v2.1.2/A_LOCAL.json；JUnit 及原始脱敏红绿/反向证据同目录。状态设计见 [CLEANUP_STATUS.md](validation/v2.1.2/CLEANUP_STATUS.md)。本批同 SHA CI 结果见下方 A 记录。

范围：不删除/改写原片、旧数据库/模型结果/人工字段/历史代理/粗剪；只在隔离测试数据库与暂存中注入故障。未放宽 skip 守卫、所有权/链接/重启未知目录边界，未重做 TSB-311～317，未开发 M6 或原生视频/音频模型。

A 批提交时的未完成项（后续进展见 B/C）：B/C 批待执行；真实素材仍只有 6 段同类型 HEVC MP4，用户确认暂无额外素材，缺少 8～10 段及 H.264/MOV/竖屏/无音轨/HDR 实片。完整浏览器及真人工验收、真实导出/播放和最终 HEAD CI 另行记录，不以本批组件/合成测试替代。阶段性结论：代码级整改通过，正式发布门禁未完成。

## A 同 SHA CI

`30c3635ed0041cfd32a6938e94860343d8bac915`，Run [38761580532](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38761580532)，7/7 success。Windows 常规两 Python 作业各 collected/passed 786，0 skipped；Ubuntu 常规各 collected 786 / passed 742 / skipped 44；强制媒体各 collected 723，Windows 723 passed / 0 skipped，Ubuntu 679 passed / 44 skipped；前端 43 passed。逐 Job/平台记录见 `validation/v2.1.2/A_CI.json`。

## B / TSB-321、TSB-322

TSB-321 问题 → 修复：应用/API/配置为 2.1.0、前端页脚为 2.0.7、启动器/README 仍写 2.0，和整改文档版本混淆。以根 VERSION=2.1.2 为权威，后端 __version__/APP_VERSION/API/发布包读取同值；前端 package.json 使用 scripts/sync_version.py 同步，Vite、启动器与 CI 均拒绝元信息漂移。UI 页脚读取生成的 package 元信息，启动提示/帮助同步；历史 v2.1.1 文档批次不改写成历史应用版本。ANALYSIS_VERSION=2.0.2、Prompt/schema 和已保存缓存键保持不变。

版本红绿证据：初始后端 3 项因缺权威源/检查流程失败，前端实际页脚测试失败；新增当前 README 检查也先失败，再修改。补充构建分析包 manifest 的实际版本和算法/模型缓存键不受产品版本影响的检查，共 5 个后端版本用例通过。反向将配置恢复为独立旧版本，测试失败，已恢复。

TSB-322：新增 16 个强制媒体用例，以真实正式 API → Runner → FFmpeg 完成扫描、检测/抽帧、代理、JSON/CSV/OTIO、1280×720 粗剪、时长/音轨/方向核验、强制检测稳定 ID 和原合成源 SHA/mtime/大小核对。覆盖 H.264、HEVC、MOV、9:16、真实 Display Matrix 旋转、无音轨、60fps、按帧时间戳验证的 VFR、10-bit BT.2020 PQ/HLG、损坏/空/零时长/缺失和混合项目。PQ/HLG 为按标准传递函数编码的灰阶测试信号，逐项核验编码器 VUI，不是 SDR 仅改标签。

媒体夹具最初对粗剪不可变文件名、done 统计含义及编码器元数据存在错误假设，校准这些测试后才形成有效兼容证据；不把夹具错误称作产品缺陷。有效 L1 编解码兼容路径通过，HDR 预览可读但未实现/未验收完整色彩管理，不声明色彩准确或以合成代替实片。

发现并修复的实际接线问题：缺失源的 checkpoint 已有 source_unavailable，但 detect JobFailures 没传 error_code，API 明细丢失结构化分类。新测试先复现 None，再接入已有 item.error_code；未改变分类字典或隐藏失败。反向断开该参数再次失败。另先测试媒体清单未登记即失败，再登记，并反向移除证明守卫有效。

| B 本地检查 | 结果 |
|---|---|
| macOS arm64 / Python 3.12.14，TSB_REQUIRE_FFMPEG=1 | collected 828 / passed 784 / skipped 44 / failed 0 / errors 0 |
| 前端 typecheck/lint/test/build | 全部通过；44 tests passed（npm 等价脚本） |

逐项矩阵见 `validation/v2.1.2/FORMAT_MATRIX.json`，L1/现有 L2 缺口分开；版本/媒体错误码/CI 接线原始红绿与反向证据为 b-*.txt，JUnit 和平台 skip 解释见 B_LOCAL.json。本批首次 Windows 编码失败后，44acadc 的同 SHA CI 7/7；见 B_CI.json 和 B_WINDOWS_ENCODING.md。

范围：不变更原片、正式 SQLite、人工状态、检测算法/边界或模型授权/TLS/清理安全边界；兼容测试仅生成小型隔离文件。没有新增版权来源不明的旅行视频、没有复制同一实片充数、没有开发 M6/音频/原生视频模型。C 批浏览器/真人工 UI 及真实产物验收待执行；8～10 段多类型实片和 HDR 色彩准确性、200GB 性能仍未完成。

B 批首次 CI 的 Windows 编码失败与补救见 [B_WINDOWS_ENCODING.md](validation/v2.1.2/B_WINDOWS_ENCODING.md)。UTF-8 修正后重新执行全量与同 SHA CI；首次失败未记作通过。


## C / TSB-323、TSB-324

问题 → 修复：真实 UI 扫描 2/2 已结束，但统计/素材列表仍是 0，检测按钮未启用；原刷新只在 hasActive 时触发。Project 现在也在最终任务数据到达时刷新，覆盖首次轮询已终态的快任务。语义/动态面板同样漏终态刷新，候选和多帧结果只在开始时加载；VisionPanel 在终态使语义、候选、预估、动态四个查询更新，不触发额外模型推理。

红绿与接线：Project 两项先 2 failed/6 passed，再 8 passed；VisionPanel 两项先 2 failed/19 passed，再 21 passed。反向恢复旧判断/断开终态刷新后分别重新 2 failed，已恢复。原始脱敏日志见 c-project-* / c-vision-*。本轮 C 未改后端实现，未改变任务互斥、状态机或人工判定规则。

验收：实际浏览器 UI 新建隔离项目、扫描、检测 17 镜头、显示 10 张可解码缩略图、8 镜头 HTTPS 分析、同输入 8 缓存/0 调用、多帧复核、自动化确认/拒绝及历史、重跑后输出变化失效、另一页面裁剪后旧提交 409、取消与清理、后续新任务、两代理/6 秒粗剪、CSV/JSON/OTIO 下载。清理故障用真实目录权限 0555 注入，不改生产清理；partial_failed 与残留强警示、单独安全重试、0 新调用及审计保留均实测。详见 MAC_UI_E2E.md 与各 UI_*.json、局部状态截图。

| C 本地检查 | 实际结果 |
|---|---|
| 普通后端全量 | 828 collected / 784 passed / 44 Windows 平台 skips / 0 failed / 0 errors |
| TSB_REQUIRE_FFMPEG=1 后端全量 | 828 collected / 784 passed / 44 Windows 平台 skips / 0 failed / 0 errors |
| 前端 typecheck / lint / test / build | 四项通过，48 tests passed；npm 等价脚本 |
| 真实成品 ffprobe / 完整解码 | 两代理 3.003s、480×270；粗剪视频轨 6.000s、容器 6.021354s、1280×720；H.264/AAC，容差内 |
| 原片与历史保全 | 6 原片和副本 SHA/大小/mtime 全同；正式 SQLite 字节全同、13 表 269 行；正式/副本各 282 历史文件 SHA 全同 |

同 HEAD CI：包含本节的 C 提交推送后验证七个作业，最终答复给确切 SHA/Run URL/数字；CI_FINAL_HEAD.md 说明同 SHA 取证方式，避免再次证据提交改变被验证 SHA。A/B 绿灯不替代 C。

本轮真实资源：现有 6 个不同 HEVC MP4 横屏/带音轨/bt709；UI 仅使用其中 2 个视频、17 镜头，初始语义分析 8 镜头。总 17 次模型请求、29 图片（包含取消在途请求），已记录返回用量 19563 tokens，不把未返回用量推算为免费。完成后服务恢复 off。真实端点/Key/token、原片路径、图片、视频、SQLite 只在被忽略的本地数据目录，不入库；提交截图只有界面状态文字。

范围声明：只增加终态数据刷新、回归与脱敏证据；不删除/覆盖/移动/改名原片，不改正式 SQLite/人工字段/模型结果/旧代理/旧粗剪。隔离旧副本只有明确的名称/路径重定位，其余 269 行旧 ID 和字段逐项相同。未降级 HTTPS、未放宽来源/所有权/链接/终态 CAS/取消安全边界，未接管未知目录，未全局杀进程、未自动清扫系统临时目录。未改检测/Prompt/ANALYSIS_VERSION，不开发 M6 或音视频原生模型。

尚未完成：真人实际播放和点选审阅待用户反馈；浏览器 URL 安全策略拒绝鉴权媒体导航，未绕过，ffprobe/完整解码不等于播放。自动化点选确认/拒绝不冒充真人结论。用户暂无额外素材，缺 ≥8～10 个不同实片及 MOV/H.264/竖屏/无音轨/HDR；HDR 人工色彩对比/准确色彩管理、Windows 真机旅行片全链路、60+ 段/200GB 性能未完成。

最终门禁见 RELEASE_GATE.md：C 同 SHA CI 通过后结论为 **代码级整改通过，正式发布门禁未完成**。不声明正式发布、全面交付或 M6 验收完成。
