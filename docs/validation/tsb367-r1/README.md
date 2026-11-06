# TSB-367-R1 证据目录

基线：`main@eb63003eb71678704d1ddf7865fd294cacfe6b39`

## 可提交的脱敏摘要

| 文件 | 内容 |
|---|---|
| [../../RELEASE_TSB_367_R1.md](../../RELEASE_TSB_367_R1.md) | 完整发布记录 |
| [GAP_ANALYSIS.md](GAP_ANALYSIS.md) | 编码前逐项实测：9 项缺陷全部属实、设计决策、拟改文件、数据库变更、测试计划 |

## 仅本地、不提交（本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-identity.local.txt` | R1-01 修复前：6 项失败 |
| `RED-lifecycle.local.txt` | R1-02 修复前：7 项失败 |
| `RED-validation.local.txt` | R1-03 修复前：2 项执行器校验缺失 |
| `RED-semantics.local.txt` | R1-06/07/08 修复前：11 项失败 |
| `RED-migration.local.txt` | R1-04 修复前：历史库缺表复现 |
| `RED-concurrency.local.txt` | R1-09 修复前：败者停留在 proposed |
| `GREEN-migration.local.txt` | 迁移路径修复后全绿 |
| `backend-full.local.txt` / `.xml` | 后端完整运行日志与 junit |

## 关键结论速查

| 项 | 结果 |
|---|---|
| 9 项缺陷核实 | ✅ 全部属实，未发现与代码矛盾 |
| R1-01 身份一致 | ✅ 提案前分配、持久化、应用复用 |
| R1-02 状态持久化 | ✅ 独立事务 + 条件更新 + 终态约束 |
| R1-03 最终校验 | ✅ 10 项检查，invalid 双重阻断 |
| R1-04 迁移兼容 | ✅ 恢复 0001、新增 0003、双路径结构一致 |
| R1-06/07/08 边界 | ✅ move_shot、all 解锁、章节语义 |
| R1-09 真并发 | ✅ 双线程 + Barrier，5 轮稳定 |
| R1-05 前端入口 | ✅ 六类操作，目标显式 |
| 反向验证 | ✅ 三项均成立 |
| 模型调用 | **0**（本轮不接入 LLM） |
