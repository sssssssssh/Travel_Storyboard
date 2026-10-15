# TSB-320 状态契约（实现前固定）

基线 main@f68f95446d8ada734843218fc974b321a905e1b4。视觉 worker 不再提前发布 succeeded/partial_failed；最终用量和所有权清理完成后，Runner 使用原有 finalize_job/CAS 判定终态，不改变合法终态集合。

| 条件 | 终态/健康 |
|---|---|
| 模型发布成功、清理成功 | succeeded / cleanup.ok=true |
| 发布成功、清理失败 | partial_failed / cleanup.ok=false，模型结果保留 |
| 部分模型失败 | partial_failed，镜头失败与清理故障分别记录 |
| 已请求取消 | cancelled 优先，清理故障额外可见 |
| 用量/清理报告持久化失败 | 不制造成功；传播失败，保留异常链 |
| 停止任务安全重试成功 | 终态与 finished_at 不变；cleanup.remediated=true，原故障审计保留；0 新模型调用 |
| 未知所有权/重启/链接/兄弟目录 | 不接管/不删除，残留未知不得显示 0；需要人工处置 |

cleanup 汇总和结构化 cleanup failure 在同一 SQLite 写事务持久化。首次故障事实保留；重试成功仅添加解决时间/方式。旧 JobFailures 快照不能覆盖清理审计。清理报告持久化失败不能只写日志后继续成功。

UI：旧 succeeded + cleanup.ok=false/未知也必须黄色强提示；可重试仅调用原有 cleanup-retry。清理已补救后保留历史终态，并明确显示补救状态。统计里的模型 failed 不混入清理失败镜头数。

红绿、反向验证与最终计数在本批验证完成后追加。本文件未宣称真机故障注入通过。
