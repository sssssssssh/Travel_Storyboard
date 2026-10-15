# v2.1.1 / M5-R2 整改记录

审查基线：`main@b984bc25f597ae8f9f50818b5e676506605c523a`。配置中的产品版本保持 2.1.0，本文件标记整改批次。TSB-311～313 不重复开发；ANALYSIS_VERSION 保持 2.0.2。

## R2-A / TSB-314

问题：旧测试只证明历史模型行还留有人工字段；原样重跑后的当前 API/UI 已丢失有效判定。新增表之前调用 create_all 也绕过了旧库一致备份。

修复：新增独立、追加写入的 segment_motion_verdicts。人工判定绑定模型行、有效入出点、源文件有界采样/文件身份、实际 JPEG 指纹、模型/提示词和结论；原样重跑有效，输入或有意义结论变化须重新确认。当前列表、详情、POST 共用三态投影；最近 20 条历史、独立分页历史、人工备注可见。两个确认按 SQLite writer 顺序最后提交生效，历史均保留；旧页面版本冲突返回 409。

迁移：在 create_all/ALTER 前对已有 SQLite 进行含 committed WAL 的 sqlite3.backup；备份失败阻止 schema 变更。旧 human_* 字段按模型行回填一次，保留旧列；旧未知指纹不伪造为有效。重复启动不重复回填或备份。设计及回滚注意见 [TSB314_DESIGN.md](validation/v2.1.1/TSB314_DESIGN.md)。

验收：新增后端 12 项修复前全部失败，前端新增 4 项修复前失败（旧 12 项通过）；修复后新后端及数据库原有测试 16 项通过，原有 motion 用例同步验证。移除 API 判定投影接线后 3 项重跑测试重新失败，生产代码已恢复。证据在 `docs/validation/v2.1.1/tsb314-*.txt`。

| 本地检查 | 结果 |
|---|---|
| 后端全量 / macOS arm64 / Python 3.12.14 / 强制媒体 | 714 collected，670 passed，44 平台专属 skipped，0 failed/errors |
| 前端 typecheck / lint / build | 全部通过 |
| 前端 vitest | 37 passed |

同 SHA 的 CI Run、7 个 Job 和精确计数以推送后的交付记录为准，不能将本地结果当作 CI。新增套件已进入强制媒体清单，未修改 skip 守卫。

范围：不修改原片、检测边界、人工裁剪/收藏/角色、UserAnnotation、ReviewRequest、EditPlanShot；保留模型历史、代理和粗剪。测试只使用隔离临时数据库及生成的小图片/合成视频。

后续批次：TSB-315 缓存、316 提交线性化、317 HTTPS、318 所有权清理、319 Mac 实机验收尚待执行。本轮阶段性结论：R2-A 代码级整改通过，发布门禁未完成。公网生产 HTTPS、Mac 真实素材/真实模型与 200GB 性能不得以 Mock/CI 替代。

## R2-A 同 SHA CI 存证

`4d0fc212df1ccfc493d560d0e1466f7580bde491`，Run [38741693258](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38741693258)，7/7 success。Windows 常规两 Python 作业均 693/693 通过；Ubuntu 常规均 collected 693 / passed 649 / skipped 44；强制媒体 Windows 630 passed / 0 skipped，Ubuntu 586 passed / 44 skipped；前端 37 passed。详见 `validation/v2.1.1/R2A_CI.json`。

## R2-B / TSB-315、TSB-316

设计选择：历史缓存命中采用方案 A（重新分析）；只有当前有效版本完全匹配才 skipped，不采用 reused 激活，reused 恒为 0。保留旧版本唯一约束，同输入强制重跑确实调用 provider，并追加 segment_semantic_attempts 审计（Job、输入/实际 JPEG 指纹、请求标识、时间、模型/Prompt/schema、结果及用量）；当前版本可原位更新，审计不覆盖。新增审计表同样由迁移前 WAL 一致备份守护。历史审计可通过 `/segments/{id}/vision-result/attempts` 分页查询。

TSB-315 问题 → 修复：整批哈希导致新增 C 重付 A/B，历史 v1 误命中保留当前 v2，帧数量假冒实际内容；改为每个镜头的项目/素材/区间/有界源采样/处理参数/模型版本 + 有序实际发送 JPEG 字节指纹。先完成实际渲染再判断缓存，发送使用不可变内存字节，失败/缺帧不冒充缓存。覆盖度区分库内、计划、实际成功、有效缓存和未覆盖；失败/取消的调用和图片数也落盘。原单元夹具虚构 C: 源路径改为隔离的小合成源文件，图片断言改为核对真实 JPEG bytes，未放松原有成功/调用次数断言。

TSB-316 问题 → 修复：模型外部终检与数据库保存间有空窗；语义/多帧都传 job_id，在同一个 BEGIN IMMEDIATE 内检查 Job 存在/项目/任务类型/running、取消状态、Segment/Asset 关联及有效性、人工排除、精确有效区间、源身份及检测签名，然后才降级旧行、保存新行。取消/源变更/失效镜头/Job 无效/唯一冲突分别记录稳定错误码；拒绝时统计不增成功。FFmpeg 和网络均不持写事务。

红绿证据：新增初始 8 个缓存、14 个提交竞态用例修复前均失败（`r2b-before.txt`）；修复后通过，并补充 2 个 writer-first Event 用例、旧未知签名及覆盖度用例，共 26 项。暂停点在 provider 返回后、真实写事务之前；writer-first 用例证明 writer 先持锁则只提交一次，取消随后生效并由原 Runner 收敛。移除语义 commit guard 接线重新失败（`tsb316-reverse-publication.txt`），已恢复。UI 额外 1 项覆盖统计测试修复前失败，修复后前端 38 项通过。

最终本地强制媒体全量 collected 740 / passed 696 / skipped 44 / failed 0 / errors 0（Python 3.12.14，macOS arm64）；前端 typecheck/lint/test/build 均通过，38 tests passed。同 SHA CI 待推送后核对。TSB-317～319 尚待后续批次，真实 HTTPS/Mac 模型及 200GB 门禁未完成。
