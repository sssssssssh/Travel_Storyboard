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
