# v2.0.3 复核闭环整改交付记录

开发修改与自动化门禁完成；**任务书的整体实机验收尚未通过**：RT-18/19 的 Windows 8～10 段真实旅行素材记录未执行。未运行用户 60+ 段 / 200GB 全量项目，不宣称全量性能通过。

## 固定版本与提交

- 任务书基线：`ebcd25da6d5eafcd60339e776169a6a523a5f0d5`。
- 分支：`codex/v2.0.3-review-closure`。
- 最终运行代码与测试 SHA：[`c9cb8c9ecccfa5f59f89dd8e826a7fd1601ad950`](https://github.com/sssssssssh/Travel_Storyboard/commit/c9cb8c9ecccfa5f59f89dd8e826a7fd1601ad950)。随后的交付文档提交只涉及 README/docs，运行代码一致。
- 从当前 v2.0.2 分支开始时，相对指定基线仅有既有交付文档差异；没有改换成浮动 main。
- APP_VERSION / 前端为 2.0.3；检测算法缓存版本 ANALYSIS_VERSION 保持 2.0.2，不因这次升级强制重分析。

| 提交 | 内容 |
|---|---|
| `a5b7d2e` | TSB-231～234 后端：统一校验、包过滤、精确代理、短事务发布、历史关联迁移与回归 |
| `f4e8066` | TSB-234/235 前端：人工处置、原片时间映射、计划阻断、默认快速校验及每任务成本 |
| `02d6c1a` | TSB-236：强制媒体清单与实际 0.6 秒 / 工作区 / 源 SHA 回归 |
| `c9cb8c9` | TSB-231：中断打包后的新尝试目录隔离，直接检查 ZIP 排除历史残留 |

没有自动重写旧 Segment ID、旧计划或人工备注，也没有修改输入目录、升级检测算法或新增自动上传。

## 自动化证据

最终固定 SHA 的 [GitHub Actions 运行 38432491256](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256) 全部 7 个作业成功。UTC 2026-10-13，对应北京时间 2026-10-14。

| 环境 / 作业 | 结果 | 证据 |
|---|---|---|
| 本地 macOS 15.7.7 arm64 / Python 3.12.14 | 258 passed，0 failed / skipped；设置 TSB_REQUIRE_FFMPEG=1 | [本地测量 JSON](validation/v2.0.3/local-automated.json) |
| Ubuntu Python 3.11 | 237 项，成功；OTIO validator 安装成功 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417984349) / [日志摘录](validation/v2.0.3/backend-ubuntu-py3.11.txt) |
| Ubuntu Python 3.13 | 237 项，成功；OTIO validator 安装成功 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417985257) / [日志摘录](validation/v2.0.3/backend-ubuntu-py3.13.txt) |
| Windows Python 3.11 | 237 项，成功；OTIO validator 安装成功 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417984343) / [日志摘录](validation/v2.0.3/backend-windows-py3.11.txt) |
| Windows Python 3.13 | 237 项，成功；OTIO validator 安装成功 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417983987) / [日志摘录](validation/v2.0.3/backend-windows-py3.13.txt) |
| Required FFmpeg Ubuntu | 188 passed，0 skipped，27.52 秒；FFmpeg/FFprobe 6.1.1-3ubuntu5 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417983707) / [完整通过清单摘录](validation/v2.0.3/ubuntu-media.txt) |
| Required FFmpeg Windows Server 2025 | 188 passed，0 skipped，47.97 秒；FFmpeg/FFprobe 9.0.2 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417984112) / [完整通过清单摘录](validation/v2.0.3/windows-media.txt) |
| 前端本地与 CI | 14 Vitest passed；typecheck / lint / build 成功 | [作业](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/job/115417984138) / [日志摘录](validation/v2.0.3/frontend.txt) |

普通后端矩阵排除 `test_ffmpeg_integration.py` 16 项和 `test_cache_recovery.py` 5 项，因此是 258−21=237；强制媒体作业单独执行这两组及稳定性/闭环清单。后端点状日志全部为通过，最终 100%，没有 s/x/f 标记。Windows/Ubuntu 互斥安装步骤按 OS 跳过是正常分支，**测试本身无 skip/xfail**；强制媒体模式会因任何媒体 skip 失败。

媒体 JUnit 报告：[Ubuntu artifact 11832226208](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/artifacts/11832226208)、[Windows artifact 11832291215](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38432491256/artifacts/11832291215)。含测试清单、失败数与实际生成媒体的测量属性。

本地使用 FFmpeg 7.1 与 FFprobe n4.4.1。3.0～3.6 秒测试在默认和自定义目录各生成实际 0.6 秒视频（本地各 8626 字节），经 FFprobe、FFmpeg 实际解码与 HTTP Range 校验。生成的原片前后 SHA-256 相同；值见本地 JSON。CI 同样对实际时长断言 `≈0.6s` 且 `<0.8s`，不能靠只检查命令参数通过。既有链路继续覆盖生成的 601 秒低帧率长片、横竖屏、静音/AAC、4K HEVC、中文/空格路径、缓存与粗剪。

这些测试耗时只描述生成的小样本；不能外推 200GB 性能。CI 合成视频也不能代替 Windows 实机旅行画面的主观播放及时间定位验收。

## 整改对照与状态

| 任务 | 原问题 → 结果 | 状态 |
|---|---|---|
| TSB-231 | 已有 SQL needs_review 过滤不足以解释全部选择：补齐失效状态、源验证、缺帧/裁剪帧/预算排除统计、空包错误、最终选择检查与中断恢复目录隔离；保持 EXIF 重编码和隐私字段 | 开发与自动化完成 |
| TSB-232 | 入口范围判定不一致：共用严格验证器，离线/直连落库同规则，部分成功、稳定错误码、重复定位，模型有限修正明确说明 | 开发与自动化完成 |
| TSB-233 | 短预览被拉长，旧对象可能覆盖历史预览：精确 duration，逐请求重读，staging，源身份和修订检查，短事务独立文件发布，失败/取消保留旧引用；实际预算与作业发布清单一起提交 | 开发与自动化完成 |
| TSB-234 | 黄标缺少可执行动作：显式选择当前有效镜头、创建新请求、旧请求 superseded，暂缓/放弃/验证重试，保留人工记录与计划，显示计划阻断及更新入口 | 开发与自动化完成；真实 Windows UI 验收待补 |
| TSB-235 | 完整校验的每任务成本不明显：默认 quick，完整/严格 opt in，200GB 两任务约额外读 400GB、重读/严格复核/FFmpeg I/O 分开；展示源变化和 needs_review 数量 | 开发与自动化完成；200GB 性能未验证 |
| TSB-236 | 补齐 RT-01～17、20 以及迁移/恢复故障；Windows/Ubuntu 强制媒体与四后端、前端均通过 | 自动化完成；RT-18/19 未关闭 |

逐个用例与测试名称见 [回归矩阵](REGRESSION.md)。[简版技术方案与状态图](DESIGN.md) 说明事务边界和状态流；[JSON 示例](examples/v2.0.3/README.md) 包括导入、错误/重复、selection 统计、显式替代与历史关联。

## 升级与使用

启动时按既有幂等迁移新增 `review_requests.supersedes_request_id/superseded_by_request_id/disposition_at` 和 `assets.analyzed_source_signature`；存在旧表改动时先做 SQLite 在线备份，包含 WAL 已提交内容。旧 Segment、UserAnnotation、EditPlanShot 和历史文件保留。

旧完整检测的轻量签名只从与原 `analyzed_fingerprint` 精确匹配的检测检查点恢复；有效缓存再次跳过时可补签名，不抽帧。没有可验证历史签名的旧完整检测应重新检测，不能用新扫描的当前指纹冒充旧检测证据。

在 AI 导演/分镜审阅里，选择当前有效镜头和有效区间再创建替代请求。旧请求保持历史 ID 与 proxy_ref，新请求等待生成代理；暂缓和放弃不删除镜头/计划。失效镜头仍被旧计划引用时，需要进入导出页人工更新，系统不自动映射。

代理失败/取消不改变旧正式预览。正常成功的有效原请求重试可更新该请求的当前 proxy_ref，但独立旧文件和原作业发布清单保留。预算不足保持 pending，并报告 partial_failed，可增加预算或新建任务。对失败但当前引用有效的请求可明确重试；范围或源不再有效时应选择新镜头。

默认快速校验采样头/中/尾；完整扫描和完整检测各自完整读取，200GB 两任务可能额外读约 400GB；严格二次复核再读，中断重读和 FFmpeg 解码另计。不要勾选强制以替代正常增量缓存，也不要为升级清空库或删除历史帧。

## 已知限制与未通过项

- **RT-18/19 未执行**：本次没有 Windows 实机与用户 8～10 段真实旅行素材；具体步骤与待填写记录见 [WINDOWS_REAL_FOOTAGE.md](validation/v2.0.3/WINDOWS_REAL_FOOTAGE.md)。正式整体验收需要补齐证据。
- **200GB 全量未执行**：仅验证生成样本，未读取或修改用户完整原片目录。未测全量耗时/RSS/总读盘，不能写成性能已通过。
- 代理取消在当前 FFmpeg 调用返回后清理临时文件；不会强制终止所有外部进程。期间项目后续后台工作仍遵守既有串行约束。
- 源检查采用文件身份和轻量采样；极端相同大小/时间及采样碰撞不等于逐字节验证。完整 SHA/严格模式需主动启用，成本按任务计算。外部在任务提交后改文件，仍须下一次验证/扫描/检测识别。
- 文件系统和 SQLite 没有跨资源事务：异常回滚删除本次新文件；进程硬崩溃可能留下未引用的独立文件或目录。它们不覆盖历史、不进入新打包尝试，临时代理不可经 media API 读取。本轮不实现全工作区孤儿清理。
- 替代/暂缓/放弃请求不会自动修复 EditPlanShot；旧计划必须由用户明确确认修改。浏览器验证为组件自动化，实际 Windows 播放/UI 判断仍按人工清单验收。
- 新列可由旧代码忽略，但旧代码不具备新安全校验。需要回滚时先停止程序、备份当前 DB 与工作区；不要删除旧代理/新历史字段，也不要把回滚当作当前版本验收通过。
