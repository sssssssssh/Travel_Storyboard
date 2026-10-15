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

## R2-B 同 SHA CI 存证

`3a767a58d566fedc0be3e7998cb2f978de562f5d`，Run [38743712708](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38743712708)，7/7 success。Windows 常规两 Python 作业各 719 passed / 0 skipped；Ubuntu 常规各 collected 719 / passed 675 / skipped 44；强制媒体 Windows 656 passed，Ubuntu 612 passed / 44 skipped；前端 38 passed。详见 `validation/v2.1.1/R2B_CI.json`。

## R2-C / TSB-317、TSB-318

TSB-317 问题 → 修复：以 `127.` 开头的外部主机可被误判为 loopback，网络异常及 HTTPX INFO 日志可能包含完整端点，旧授权上下文可绕过后来关闭的开关。改为 IP 地址解析/精确 localhost 判定，拒绝 URL 中嵌入凭据/查询/片段；默认拒绝公网 HTTP，保留的开关仅为开发测试例外并在 UI 标明。每次请求前再检查总开关；TLS 校验失败归类为不可重试的 MODEL_TLS_FAILED，网络/超时/HTTP 正文不公开，HTTPX URL 隐藏。供应商 usage 只接受非负整数 token 字段，请求 ID 使用本地产生的标识，不保存不可信网关回显。

安全接入工具 `backend/tools/verify_secure_model.py` 默认只验证配置、零上传；显式 probe + acknowledge 才发送两张内存生成图片。真实地址和 Key 只填被忽略的 backend/.env。工具不修改云资源；localhost 选项仅记录操作人的加密隧道确认，不能自动证明远端隧道。可执行步骤和指标含义见 [HTTPS_ACCEPTANCE.md](validation/v2.1.1/HTTPS_ACCEPTANCE.md)。本机未提供实际 HTTPS 服务/凭据，T317-02 真实模型验证未执行，生产发布门禁未完成。

TSB-318 问题 → 修复：视觉任务自行递归清理没有 Job 所有权及残留统计，成功登记也未回收。将两种精确叶路径接入 create_owned_staging / cleanup_staging_or_log；校验项目、Job 类型和叶名，仅原子新建可登记，已有目录及链接拒绝。所有结束路径在 finally 清理，并显示 stats.cleanup 的真实移除/残留字节、失败和重试信息；最终统计写入出错也不能跳过清理。成功移除 owner 登记，最多保留 256 条仅能证明目录不存在的幂等回执，回执不能删除后来重建的目录；失败保留登记。重启后不推导/接管未知目录，历史代理及粗剪不动。

红绿证据：最初新增 30 项后端测试在修复前 24 failed / 6 passed；前端 2 项新增均失败。补充统计写入异常的两条测试，修复前均因缺少 cleanup 失败。反向移除 worker 的所有权注册，及恢复宽松 loopback 判定，测试再次失败，已恢复。另验证兄弟任务、重启失去登记、回执被重建目录攻击、登记上限及真实 FFmpeg 中文 >275 字符工作区；合成源 SHA/mtime 和历史粗剪字节不变。证据见 `validation/v2.1.1/r2c-*.txt`、`tsb317-*.txt`、`tsb318-*.txt`。

范围：保留原片、SQLite 旧数据、人工评分/裁剪/角色、确认历史、代理与粗剪；不放宽 skip 守卫，不改 Runner 状态机，不开发 M6、音频语义或原生视频理解。自动化只使用本地模拟服务、注入异常、合成图片/视频；真实 HTTPS、Mac 8～10 段视觉流程和 200GB 性能另列未完成。

R2-C 最终本地全量：collected 781 / passed 737 / skipped 44 / failed 0 / errors 0（macOS arm64，Python 3.12.14，TSB_REQUIRE_FFMPEG=1）；前端四项全部通过，40 tests passed。Windows 专属 skip 原因按既有守卫解释。pnpm 包装器在本机不可用，使用 npm run 执行完全相同的 package.json 四个脚本；CI 保持 pnpm。见 `validation/v2.1.1/R2C_LOCAL.json`。同 SHA CI 推送后核对。

## R2-C 同 SHA CI 存证

`9b6831765aaa0f6a083b8ab7a87b66f1529df9e5`，Run [38746742154](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38746742154)，7/7 success。Windows 常规两 Python 作业各 collected/passed 760，0 skipped；Ubuntu 常规各 collected 760 / passed 716 / skipped 44；强制媒体各 collected 697，Windows 697 passed / 0 skipped，Ubuntu 653 passed / 44 skipped；前端 40 passed。逐 Job 计数和平台见 `validation/v2.1.1/R2C_CI.json`，R2A/B 也补齐逐 Job 平台/精确计数。

## R2-D / TSB-319 发布回归与 Mac 实测

真实接线发现的问题 → 修复：通用 `/jobs/{job_type}` 先于专用视觉路由注册，前端 flat body 中 upload_acknowledged / segment_ids / limit / frame_count 被 JobCreate 忽略，导致已同意的请求仍失败；模拟 provider 单元测试没有覆盖真实 HTTP 匹配。新增 8 项真实 FastAPI 测试修复前 6 failed / 2 passed；将视觉路由放在通用路由前，并兼容原 options 包装，格式矛盾或越界参数在入队前 422。反向恢复旧路由顺序，6 项再次失败；恢复后全部转绿。证据 `tsb319-route-before/after/reverse.txt`，新套件已进入强制媒体清单。保留 TSB-312/313 的逐调用授权与内存图片机制，没有换 Runner 或扩展 M6。

最终本地回归（修正路由后）：**collected 789 / passed 745 / skipped 44 / failures 0 / errors 0**；环境 macOS arm64 / Python 3.12.14，TSB_REQUIRE_FFMPEG=1。前端 typecheck/lint/test/build 全部通过，**40 tests passed**。详见 `validation/v2.1.1/R2D_LOCAL.json`；最终 HEAD 的 7 Job CI 结果推送后核对，不拿上一批绿色替代。

用户提供素材与模型后，验证同一模型服务的有效 HTTPS 接入，明文例外保持 false；凭据仅存被 Git 忽略的 backend/.env（0600），默认模式 off。生成多图 probe 及真实错误 Key 分类通过；用户明确授权人脸/车牌画面外发后，在隔离 SQLite/工作区副本完成跨 6 段素材的 8 镜头语义（8 次请求、13 张 JPEG、9816 tokens）、8 镜头有效缓存（0 新调用）、4 镜头多帧、2 镜头确认/拒绝及重跑历史、裁剪失效、Prompt 版本 1.0→v2→1.0 和真实模型取消。模型重跑的描述实际变化，因此需要重新确认，未把输入相同说成模型证据相同。

本机正式旧 SQLite 的 9 表/269 行旧列数量及逻辑哈希全部保留，迁移前备份与临时副本恢复校验通过；新增 audit_sqlite_upgrade.py 可复用此检查。备份 `backend/data/travel_storyboard.pre-migration-20261015-134731-570082.bak` 仅留本机。6 段原片及验收副本 SHA、原片 mtime/大小，282 个原历史派生文件字节均不变。模型测试只写隔离副本，Asset/Segment ID 保持原值。

范围：未删除/改写原片和历史代理、粗剪、检测边界、正式人工评分/裁剪/角色、UserAnnotation、ReviewRequest、EditPlanShot 或原审阅记录。正式旧库只做有备份的增量 schema 升级，旧表旧列核对相同；验收副本里的显式测试判定/裁剪与正式数据分开。未操作云资源，未提交原图/视频/SQLite/凭据/私有端点。

尚未完成：用户目录实际仅 6 段同类型 4K HEVC 横屏 MP4，少于要求的 8～10 段；H.264/MOV/竖屏/无音轨/HDR、完整 UI 人工审阅与导出流程、真实限流/断网故障注入仍未执行。精确事务屏障、故障清理边界由自动化覆盖，不能冒充真实服务对应注入。失败/取消尝试未报告的 token 不假定为零或免费，不估算供应商价格。60+ 段/200GB 性能未测，不外推；原生视频/音频语义和 AI 导演决策未开发。

完整实测矩阵、逐 Job 和 G1～G8 见 [MAC_REAL_FOOTAGE.md](validation/v2.1.1/MAC_REAL_FOOTAGE.md)。最终同 HEAD CI 存证推送后生成 `validation/v2.1.1/FINAL_HEAD_CI.local.json`（不提交，避免改变被验 SHA），同时在交付回复链接 GitHub Run；CI 的 JUnit/平台 artifacts 为权威原始证据。最终结论：**代码级整改通过，发布门禁未完成**。
