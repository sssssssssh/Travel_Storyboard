# TSB-372-R2-FIX-01 验收

基线 `main@2a670e8f2a5beef5ff9e4735d3286c4267fece30`，同一 SHA 的 [CI #163](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/45107293231) 7/7 通过。

- [能力分类测试](CLASSIFICATION_TESTS.md)记录 HTTP 判定、文字对照及多轮诊断。
- [证据投影测试](EVIDENCE_PROJECTION_TESTS.md)记录静帧过滤、61 个候选和两份真实数据快照的离线摘要对照。
- [工程验证](FINAL_VERIFICATION.md)记录回归和最终 CI 的核验方式。

历史真实回执 `8aeb9e26a7d249b7` 与盲评会话 `3ed86402-fb65-4006-b4d5-c430ccd5da51` 保留。真实项目仅用只读 SQLite 备份进行离线复算，调用账本的 SHA-256 前后一致。无需数据库迁移。

R2 的关闭条件仍为真人盲评完成、PAI-EAS 配置修复后原生视频多轮探测通过，以及获批派生素材的 20～30 个镜头三路对比。
