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

Windows 专属跳过逐项见 validation/v2.1.2/A_LOCAL.json；JUnit 及原始脱敏红绿/反向证据同目录。状态设计见 [CLEANUP_STATUS.md](validation/v2.1.2/CLEANUP_STATUS.md)。本批同 SHA CI 待提交后核对。

范围：不删除/改写原片、旧数据库/模型结果/人工字段/历史代理/粗剪；只在隔离测试数据库与暂存中注入故障。未放宽 skip 守卫、所有权/链接/重启未知目录边界，未重做 TSB-311～317，未开发 M6 或原生视频/音频模型。

尚未完成：B/C 批待执行；真实素材仍只有 6 段同类型 HEVC MP4，用户确认暂无额外素材，缺少 8～10 段及 H.264/MOV/竖屏/无音轨/HDR 实片。完整浏览器及真人工验收、真实导出/播放和最终 HEAD CI 另行记录，不以本批组件/合成测试替代。阶段性结论：代码级整改通过，正式发布门禁未完成。
