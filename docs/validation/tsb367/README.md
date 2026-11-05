# TSB-367 证据目录

基线：`main@bca2433f968c3e62522c4c7b8647aa9452172af3`

## 可提交的脱敏摘要

| 文件 | 内容 |
|---|---|
| [../../RELEASE_TSB_367.md](../../RELEASE_TSB_367.md) | 完整发布记录 |
| [GAP_ANALYSIS.md](GAP_ANALYSIS.md) | 开工差距分析（编码前输出）：阶段 0 三项遗留均仍在、复用项、拟改文件、数据库变更、测试计划 |

## 仅本地、不提交（本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-stage0.local.txt` | 阶段 0 修复前的真实失败输出（10 failed） |
| `backend-full.local.txt` / `backend-full.local.xml` | 后端完整运行日志与 junit |

## 关键结论速查

| 项 | 结果 |
|---|---|
| 阶段 0 三项遗留 | ✅ 全部收口 |
| PlanOperation 表 + 4 触发器 + 1 部分唯一索引 | ✅ |
| 八类结构化操作 | ✅ 全部实现并测试 |
| 锁定矩阵 | ✅ 20 格逐格测试 |
| base_version CAS / 幂等 / 终态保护 | ✅ 并做反向验证 |
| 后端专项 | 148 passed |
| 前端 | 112 passed，四项检查通过 |
| 模型调用 | **0**（本轮不接入 LLM） |
