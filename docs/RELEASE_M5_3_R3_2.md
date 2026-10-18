# M5.3-R3.2 回退执行与前端状态闭环（TSB-356～359）

冻结基线 main@8c05afa15e01d5fab185d63310ee3de62980e5fa（2026-10-18，Asia/Shanghai），工作区干净并 fetch 核对一致。程序 VERSION 仍为 2.1.2，不改算法、Prompt 或能力授权。

## A / TSB-356：问题 → 修复 → 验收

问题：父 Job 终态使 useJob 停止请求；后继 pending/running/cancelling 不再更新。终态刷新没有 motion-reviews，且面板仅为原生视频记录渲染比较卡，导致没有原生视频记录时实际多帧结果也不可见。切换项目还会沿用组件内旧 jobId/勾选。

修复：只轮询父端点返回的实时 fallback，保持 1.5 秒周期，普通 Job 仍终态停；后继终态刷新 motion-reviews/coverage/observations，单独展示真实多帧卡，不新增视频记录。以项目 key 隔离状态，卸载时 AbortSignal 与 React Query 停止旧查询。网络失败保留旧读数并提示/可重试。用户可暂停/继续界面刷新，不取消后台工作；查看/取消后继指引使用真实编号。补上可见的本次回退开关，默认关闭，上传同意仍独立且必需，未默许回退或扩大授权。

RED：新增 F01～F10 及卸载/网络/暂停场景最初 11 failed / 4 passed；校准 fake timer 的 React Query 微任务刷新后在基线仍 11 failed / 4 passed。回退显式开关另先 1 failed。GREEN：新增共 17 场景，前端全量 96 passed。逆向恢复只看父状态，F01/F08 两项失败；只断开 motion-reviews 失效，F04 失败，均已恢复。定向运行未选中的用例不计作通过，也不是平台 skip。

| A 验证 | 结果 |
|---|---|
| 后端严格全量（从 backend/） | 1306 collected / 1262 passed / 44 Windows 平台 skipped / 0 failed / 0 errors |
| 前端 typecheck/lint/test/build（npm 等价脚本，CI pnpm） | 四项通过，96 passed |

证据见 validation/m5.3-r3.2/F_*.txt、A_LOCAL.json、A_BACKEND.xml；后端基线既有 B23 直接 worker 的弱断言不算真实 E2E，留 B 批补强。

范围：不改预算 TSB-351～353、暂存/Provider/能力门禁；不写原片、正式 SQLite、人工判定/裁剪/评分，不调用真实模型或使用用户视频，不修改数据模型或安全开关默认值。当前 B/C 未完成，同 HEAD 最终 CI 待完整批次推送；G5=blocked_by_endpoint，G6=not_evaluated，Windows 本机真人验收 not_done；不以 Mock/合成/CI 代替这些门禁。
