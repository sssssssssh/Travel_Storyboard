# TSB-372-R2 阶段验收

基线 `main@d699259352c42753f198a3514a0eed4e70516534`。真实验证使用当前用户自有 HTTPS PAI-EAS 和该项目的隔离 SQLite 副本。

| 阶段 | 状态 | 证据 |
| --- | --- | --- |
| R2-01 可信度 | 代码回归通过 | 缺失及无效静帧状态降级。多帧人工否决和待确认读数无法生成事实，人工确认需保留来源。缺时序窗口、低质量或受限的原生视频事件留人工复核 |
| R2-02 真实文字 | `REAL_MODEL_PASS` | 一次请求、重试 0，61/61 发送；GLM-5.3-Flash 返回 11 个镜头、60 秒并通过完整 Validator。见 [服务端回执](REAL_TEXT_PLAN_EVIDENCE.json) |
| R2-03 原生视频 | `NATIVE_VIDEO_BLOCKED` | 文字对照 200，合成视频 400。服务端明确当前部署允许的视频数为 0。见 [Dry-run 与现场结果](NATIVE_VIDEO_PROBE.json) |
| 三路对比 | 阻塞 | 等待现有探测工具验证 `verified_native_video`，以及派生片段预审、逐片授权与预算。见 [对比状态](VIDEO_COMPARISON.json) |
| R2-04 人工质量 | `HUMAN_REVIEW_PENDING` | 新盲评会话已创建并核实未揭示。61 个镜头的核对材料和 5 个实际剪辑试核样本已整理。见 [人工复核表](HUMAN_REVIEW.md) |
| R2-05 结果 | 已记录结构差异，实际优势待评 | [新旧 Prompt 与规则对照](PLAN_COMPARISON.md) |
| R2-06 工程 | 见验证记录 | [工程、隔离与 CI](FINAL_VERIFICATION.md) |

盲评会话 `3ed86402-fb65-4006-b4d5-c430ccd5da51`。在验收机运行随隔离目录提供的 `Open-TSB372-R2-Review.ps1`。脚本启动或复用仅供本机访问的回环服务，从隔离目录读取令牌后打开对应评审页。先盲评，再打开带来源的人工复核表和方案对照。

PAI-EAS 管理员可核对初始 vLLM 的多模态配置，确认模型和服务栈能处理视频后按变更流程将视频限额设为 `--limit-mm-per-prompt video=1`。调整后用同协议先复跑合成多轮探测及文字、单帧对照，再决定是否进入真人脱敏及派生视频对比。
