# TSB-276 定向复核

范围：固定 f7ab581 基线到本轮 main；依据任务书检查状态、删除、事务发布边界。
本文件为本轮实施者的定向复核记录，不冒称另一个独立审查者或真实素材验收。

- Runner：各 task 分支汇总 intended outcome；task finally 后才进入唯一 finalizer。
  CAS 失败重读，底层显式 expected_statuses 不可绕过单调性，合法终态不可改时间/错误，cancelling 不转回 running；4 次有界失败
  带 STATE_CONVERGENCE_FAILED/Job ID，DB 独占继续存在。自然完成后取消 409。
- Cleanup：形状/原始段先于 resolve；创建登记拒绝已有目录，真实 Job/素材归属；
  祖先与目标身份、所有链接、二次检查和 fd/Windows 句柄边界。跨任务与跨项目不删。
  枚举异常不当空目录，实际残留数/字节和重试状态可见。
- Analyzer：取消与帧提交在同一 writer 线性化；唯一文件名回滚仅回收无引用本轮文件。
  人工字段、ReviewRequest、计划引用通过逐项断言，不重映射 ID。
- Proxy/package：已有 writer 下改用同源状态函数。粗剪审查发现最后轮询窗口，增加
  修复前失败用例，再将发布与路径同事务提交、使用独立文件名保留历史。
- Reporting/retry：progress 不覆盖 cleanup，逐 stage 的成功不隐藏其他失败；认证与
  项目/Job 固定作用域，不接收路径。运行中或重启无创建证据拒绝重试。
- Orphans：维护仅 frames；过期且无引用才删除，当前 stage/近期帧/检测中发布保留。
  项目锁+DB writer 防引用快照竞争；扫描错误和链接拒绝有日志，不靠 ignore_errors。
- 反向验证：撤掉 stage 创建者接线，清理回归失败；恢复 cancelled-only 判断，分析
  取消事务回归失败。实现恢复后全量验证通过。

已知限制：进程内 stage 记录不恢复未知崩溃孤儿；DB 不可用时终态需后续恢复处理。
TSB-277 真实 Windows 旅行素材和 200GB 性能仍未执行。

最终 CI 再次发现 TC-29 旧测试的终态快照/解锁时序误判：改为 Event 控制两个阶段，
worker Future 完成后严格断言 202，期间两次 409 均无新增行；不增加 sleep 或放宽断言。

TSB-274 升级恢复复核：旧版无 items 的单条 cleanup 失败先归一为历史项并脱敏，
未知创建证据不授予删除权；残留大小未知时返回 null/界面待核对，不冒称零字节。
先失败后通过的定向用例与最终本机全量、前端四项验证均通过。

Windows run 38586473593 另发现旧直接任务单测遗留 running Job：下一次 TestClient
启动恢复旧任务，在新编码 mock 下多了一次调用。现按实际成功/部分失败/异常/取消
收敛这些直接调用的 Job 并保留原始异常；不改变生产调度，也不放宽次数或清理断言。
证据见 direct-task-recovery-test.txt，最终本机全量再次通过。
