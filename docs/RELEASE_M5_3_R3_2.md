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

## B / TSB-357、358：问题 → 修复 → 验收

问题：旧 B23 直接调用 worker，允许 running 或前提缺失时 skip，未证明结果入库。新增真实 Runner 测试复现：降级成功仍无条件记 failure，父任务成为 partial_failed；配置构造丢失 force；仅凭子 succeeded 把空结果和历史读数报为已分析；调度失败显示可能带私有详情。

修复：成功安排回退只记录可见的降级原因，入队失败才计 FALLBACK_SCHEDULE_FAILED；保留 force 快照。只读解析同时核对成功状态、正 analyzed 计数、同项目/资产/选中镜头/当前精确区间/本次执行时间窗内的有效多帧结果，缺证据返回 FALLBACK_NO_RESULTS。部分失败保留实际数量但 analysed=false；失败/取消/缺失及旧数据兼容，返回稳定错误码。前端调度失败使用安全诊断，展示真实后继编号；新任务替换旧查询并恢复刷新。

真实调用链：正式 POST video_analyze → job_service → 自有 JobRunner.submit/_run 父任务 → 正式创建唯一 motion_review 后继 → 真实 Runner 等父锁释放 → 真实 FFmpeg 抽帧 → 未替换的 VisionProvider.complete/序列化/校验 → 只在 httpx.post 边界拦截的回环 Mock → 原 _store → SegmentMotionReview → 正式 motion-reviews API。没有直接调 worker 冒充 E2E，没有手动把真实链路 Job 写成功；等待有确定上限，拒绝 pending/running 作为最终通过。

RED：初始后端 58 collected / 25 failed / 33 passed / 0 skipped / 0 errors；补充缺帧/空动作各失败，畸形帧/越界帧各失败，坏计数原已拒绝；前端调度失败先 1 failed。GREEN：后端专项 65 passed / 0 skipped（新 Runner 17、状态契约 26、原追踪升级 22）；前端四项通过，98 passed（新增轮询文件 19）。完整全量结果由验证目录的 JUnit 摘要记录。

首轮全量 1349 collected / 1304 passed / 1 failed / 44 平台 skipped，发现旧 V39c 要求保留 JobConflict 类别。互斥和无残留断言未失效；修正为仅保留已知冲突类别，不拼接异常详情。加入兼容执行文件后的专项 84 passed / 0 skipped，原 V39c 转绿；证据 B_FULL_FIRST_RED.* 与 B_COMPAT_TARGET.*。完整全量随后重跑。

| B 最终本地验证 | 实际结果 |
|---|---|
| 后端严格全量（从 backend/，TSB_REQUIRE_FFMPEG=1） | 1349 collected / 1305 passed / 44 Windows 平台 skipped / 0 failed / 0 errors |
| 三文件专项 / 加兼容文件专项 | 65 passed / 84 passed，均 0 skipped |
| 前端 typecheck / lint / test / build | 全部通过，98 passed（12 files） |

严格全量也覆盖 TSB-351～353 预算、TSB-345～346 暂存、Provider 和 Capability Gate。完整计数与逐项平台 skip 见 B_LOCAL.json、B_BACKEND.xml，数字由 JUnit 读取，不将 collected 当 passed。

逆向验证均已恢复：删除实际 _store → B05 无结果失败；将真实 _run 换成直接 task(jid) → B03 无 started_at 失败；只按 succeeded 判 analysed → B10 空入库误报失败；去掉 force 接线 → B06 失败；恢复降级无条件计 failure → B02 父部分失败；移出 mandatory media → 登记守卫失败。见 B_REVERSE_*.txt，不将未选中用例算通过。

安全证据：仅使用隔离的 3 秒合成 H.264 与假凭据，Mock 断言目标 127.0.0.1、至少两张派生 JPEG、无 EXIF 和真实源路径。无上传确认在业务边界拒绝，虽可建立失败任务，但不抽帧、不发送；回退开关与上传同意均独立默认关闭。源 SHA/大小/mtime、检测/裁剪/评分/备注和历史判定保持不变；通过正式 verdict API 添加的人工历史在第二次真实重跑后逐字段不变。成功和取消清理自己的暂存，保留其他任务哨兵文件；排队取消不读媒体，运行取消不提交晚到结果，取消历史父任务不能杀掉子任务。

CI 接线：两个新后端文件加入 Required FFmpeg 显式清单（含依赖真实媒体的 B23/B23b 原追踪文件原已在清单）。不修改 skip guard，也无新增平台豁免。A 提交 c98b643 的 [CI 39089911587](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39089911587) 已核对 7/7 success；最终交付须另外核对最终 SHA，不沿用 A 结果。

## 范围与未完成项

不改预算 TSB-351～353、暂存安全 TSB-345～346、Provider/能力门禁、Prompt 和推断算法；不删改原片、正式 SQLite、历史产物或人工判定/裁剪/评分，不调用真实模型或使用用户视频，不修改历史数据模型、无 DB 迁移，不放宽安全开关默认值。

最终同 HEAD CI 待完整批次推送后核验；G5=blocked_by_endpoint，G6=not_evaluated，Windows 本机真人验收 not_done，8～10 段真实素材及 200GB 实测没有新增完成证据。不以 Mock/合成/CI 代替这些门禁。完整证据索引与最终同 SHA 链接交付方式见 validation/m5.3-r3.2/README.md。
