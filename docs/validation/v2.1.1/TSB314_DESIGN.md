# R2-A / TSB-314 实施前设计

审查基线：main@b984bc25f597ae8f9f50818b5e676506605c523a。

## 数据及有效性

新增 `segment_motion_verdicts`，以递增 revision 决定最后提交的人工判定，UUID 为公开 ID；每次确认追加记录，SQLite BEGIN IMMEDIATE 串行化，历史不可覆盖。判定绑定 project/asset/segment、motion_review_id、input_signature 和 evidence_signature。模型表保持历史版本，并新增 input_signature；旧 human_* 列保留兼容镜像，不作为新系统唯一事实。

输入指纹绑定源文件有界 quick fingerprint、文件身份、检测签名、有效入出点、实际 JPEG 字节与时间戳、处理规范；不以整个视频全量 SHA 作每次判断。证据指纹包含模型/提示词版本、规范化结论、证据时间戳与局限。完全相同证据重跑可延续最后判定；输入或有意义结论变化需重新确认。未指纹化的旧模型证据只能保留历史，不能推断成当前有效。

API 在当前模型行上投影独立判定：confirmed_current、needs_reconfirmation、not_reviewed。只选最后一次人工提交，不回退到更早的相似判定；列表、详情和 POST 共用投影。历史限定最近 20 条，并支持独立分页查询。UI 显示当前有效、证据改变、历史判断及备注；证据改变后仍可重新判定。请求可携带模型行 ID/输入指纹，过期页面返回 409，不把点击绑定到用户未见的新模型版本。

## 迁移与回滚

init_db 在 create_all 或 ALTER 前检查缺失表/列，对已存在 SQLite 通过 sqlite3.backup 保存包含 committed WAL 的一致快照；备份失败则禁止迁移。旧 human_* 按模型行逐条回填，legacy_review_id 唯一防重复；未知旧指纹留空并显示需重新确认。保留所有旧列、模型版本、其他业务表及产物。重复启动不得新增备份或重复回填。迁移仅新增表/列：旧程序可忽略新增结构；需完全回滚时停服务后由用户恢复迁移前一致快照，不能用回滚覆盖升级后新写入的数据。

## 验证计划

先用实际 API 重现原样重跑当前判定消失、编辑/结论改变缺少重确认状态；用旧 schema + committed WAL 测 create_all 前备份、回填幂等与失败关闭；以线程屏障验证并发确认历史完整且仅最后一项有效。前端验证三态、备注、历史与 stale 页面提交错误。自动化与真实素材/真实模型验收分别陈述。
