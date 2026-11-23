# 工程验证

基线 `2a670e8f2a5beef5ff9e4735d3286c4267fece30`。基线已有 GitHub Actions #163 的 7 项成功证据。 [基线运行](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/45107293231)

| 检查 | 本机结果 |
| --- | --- |
| 分类、证据投影、匿名化、人工反馈 | 聚焦测试 163 通过。基线复现 19 项失败，多轮汇总单独复现 3 项失败 |
| 前端 | 19 个文件、192 个测试通过，TypeScript、ESLint、Build 通过 |
| Ruff | 5 个修改的 Python 文件通过 |
| 真实快照离线复算 | 历史副本和当前副本均保留 61 个候选。静帧事实投影 61 → 60，账本校验和保持一致 |
| 历史验收 | Git Diff 核对历史目录无变化。回执与会话沿用历史 ID |

本机严格完整后端回归与同一 SHA 七项 CI 矩阵的最终状态随现场验证副本交付。仓库入口按 `main` 提交核对。 [CI 运行列表](https://github.com/sssssssssh/Travel_Storyboard/actions?query=branch%3Amain)
