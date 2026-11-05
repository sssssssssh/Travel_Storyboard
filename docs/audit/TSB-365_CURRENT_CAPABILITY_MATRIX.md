# TSB-365 现有能力矩阵

审计日期：2026-11-05。基线：main@0c57aee13ec298af92e3ea8fe11c88bcfee19ddc，VERSION=2.1.2；git pull --ff-only 返回 Already up to date，初始工作区干净。

本报告为静态审计。CODE/API/DB 表示阅读实现；TEST 表示已有测试源码覆盖，不表示本轮执行通过；E2E 为仓库历史记录，不是本轮复测；DOC 为文档；GAP 为在列明调用链中未发现实现。以下路径相对仓库根目录。后端路由均带 `/api` 前缀。

## 1. 完整链路与可用性

| 能力 | 前端入口 | API / Schema | CODE 服务与 DB | TEST 证据 | E2E 与结论 |
|---|---|---|---|---|---|
| 项目创建、多源目录、隔离输出 | Home.tsx，`/`；Project.tsx，`/project/:id` | api/projects.py create_project；ProjectCreate/ProjectUpdate | Project.source_roots/settings/output_dir；paths.py | test_output_dir.py、test_security.py | 有历史建库记录；代码有入口，不等于大规模验收 |
| 项目编辑、删除 | 本轮未见 Home/Project 中完整编辑/删除操作入口 | PATCH/DELETE `/projects/{id}` | update_project/delete_project；删除 DB 项目并 forget_project_root，不执行原片删除 | test_output_dir.py 为相关边界测试，不能代替完整 CRUD UI 测试 | API 已实现；UI 闭环待补，非原片文件管理器 |
| 扫描、重复/增量扫描、元信息 | Project.tsx 任务按钮与素材表 | POST `/projects/{id}/jobs/scan`，JobRequest | indexer.index_one/scan_task；Asset 指纹、路径唯一约束；Job | test_deep_scan_api.py::test_rescan_reuses_asset_ids_and_is_idempotent、test_added_file_is_picked_up_on_rescan、test_scan_reports_damaged_video_as_a_failure | tsb362/e2e-multiframe.md 有单素材正式 API 扫描；200GB 未验 |
| 多根去重、只读 | 同上 | 同上 | iter_video_files；paths 项目产物边界 | test_two_roots_are_deduplicated、test_scan_does_not_modify_the_source_root（同文件） | 已有自动化守卫；不自动接管移动文件身份 |
| 缺失、损坏、文件变更 | Project 失败信息；Review 状态 | assets、jobs、segments 响应 | indexer 失败记录；analyzer 检查源存在，保护已引用旧 Segment 并置 needs_review | test_review_validation.py::test_analyzed_source_signature_survives_rescan_and_rejects_changed_source；test_deep_scan_api.py::test_missing_root_does_not_look_like_an_empty_success | 缺根/损坏可见；未证明目录遍历能主动识别所有已移走文件；不存在完整移动重绑定流程 |
| 素材分页、名称检索 | Project 素材表；参数接线需 UI 实测 | assets.list_assets：offset/limit/status/q（display_name ilike）；AssetListOut | Asset | Project.test.tsx；test_deep_scan_api.py 为扫描相关证据 | API 名称检索不是全文语义搜索 |
| 镜头筛选 | Review.tsx `/project/:id/review` | segments.list_segments：rating/story_role/boundary_reason/asset_id；SegmentListOut | Segment；列表含质量、标签、缩略图和备注 | Review.test.tsx、test_data_integrity.py | 没有“按自然语言找到人物/极光”的统一查询契约 |
| 切镜 | Project 检测任务 | jobs/detect | detector.detect_segments：HSV 直方图 Bhattacharyya 距离；默认 1fps、阈值 .35、最长 30s、宽160；analyzer 持久化 detected_* 与编辑边界 | test_ffmpeg_integration.py、test_data_integrity.py::test_detected_key_is_immutable_when_trimmed | 单段真实素材有链路证据，人工边界精度未验；不能声称帧级切镜 |
| 代表帧、质量与相似性 | Review 镜头卡/详情 | segments 与 frames/image | frames.select_rep_times：短段中点、起始偏移/.45位置/差异峰；quality.analyze_frame：Laplacian、亮度、过曝/欠曝比例；phash 实为 dHash；Frame/Segment.quality_metrics | test_ffmpeg_integration.py、test_hash_pipeline.py | 是技术信号，不是故事价值；相似图像不等于语义重复 |
| 图像语义与多帧运动 | AIDirector 中 VisionPanel；Review 复核 | api/vision.py 语义/运动 Job、结果与 verdict 路由 | vision/semantic.py、motion.py、schema.py；SegmentSemanticResult、SegmentMotionReview、SegmentMotionVerdict | test_vision_semantics.py、test_vision_motion.py::test_invented_timestamps_are_dropped、test_vision_route_wiring.py | 历史有限真实 E2E，详见 §3；不能推断内容准确率 |
| 失败、部分成功、取消、重试 | Project/useJob/CleanupNotice/VisionPanel | jobs 查询、cancel、cleanup-retry | runner/job_service；Job.failures/stats；语义尝试与缓存版本 | test_cancel_lifecycle.py、test_vision_cleanup_status.py、test_tsb361_job_lifecycle.py | 复用现有任务框架；不在本轮审计中重证所有并发安全 |
| 评分/收藏/排除、备注、用途、裁剪 | Review 详情 | PATCH `/segments/{id}`；AnnotateRequest；reset-trim | _apply_annotation/_save_annotation；Segment+UserAnnotation；检测边界不改 | test_data_integrity.py::test_trim_inside_window_is_accepted_and_flagged、test_reset_trim_restores_detected_bounds、test_concurrent_annotations.py | 可作为导演硬约束/偏好输入；备注须先解释确认，不能直接作为可执行指令 |
| 标签人工编辑、批量、通用撤销 | Review 中未发现完整通用入口 | 当前 AnnotateRequest 应用的是 rating/story_role/in/out/note；无通用批量计划操作 API | content_tags 存储不等于人工标签编辑；UserAnnotation 更新现值 | 未见完整通用撤销测试；运动判定历史测试仅覆盖专用领域 | GAP：通用历史/撤销/原子批量；不要把 reset-trim 当全局撤销 |
| 复核包与请求导入 | AIDirector 导出包/粘贴 JSON、ReviewRequestCard | packages；review-requests/import/disposition/proxies | review_package、story_planner.import_review_requests/proxies_task、review_validation；ReviewPackage/ReviewRequest | test_review_package_privacy.py；test_review_validation.py::test_forged_cross_project_and_mismatched_asset、test_human_replacement_preserves_history_annotations_plan | 已有复核闭环；请求替代 lineage 不是故事板版本 |
| 故事线建议 | AIDirector.tsx AnalysisReport 展示 title/logline | POST `/projects/{id}/story/analyze` | direct_analyze 调 model_client.chat；最多默认12图/24采样镜头，60候选预算；返回 stories+coverage；仅 ReviewRequest 落库 | test_story_direct.py::test_direct_analyze_happy_path（mock）、test_coverage_reports_what_was_not_seen、test_validate_rejects_invented_segment_ids | 存在一次性故事建议，非零能力；GAP：完整方案持久化、目标约束、章节编排、自动落入计划 |
| 手工故事板 | Export.tsx `/project/:id/export` | GET/POST edit-plan；EditPlanBuild | timeline_export.build_edit_plan；EditPlanShot；事务内全量替换，数组顺序权威 | test_timeline_export.py::test_validation_is_all_or_nothing、test_array_order_wins_and_timeline_is_contiguous；Export.test.tsx RT15/RT17 | 可添加/移除/上下重排/裁剪/改时长与用途；无锁定、版本恢复、专门替换操作 |
| JSON/CSV/OTIO | Export 下载链接 | export/json,csv,otio | timeline_export.export_json/export_csv/export_otio；同一已保存计划 | test_json_csv_otio_agree_on_shots_and_duration、test_otio_round_trips_through_opentimelineio | JSON 有双 ID/秒数；CSV 无双 ID/路径；OTIO 引用本地路径与帧率换算；真实 NLE 兼容未验 |
| 粗剪预览 | Export 生成粗剪 | jobs/roughcut；media roughcut | roughcut.roughcut_task；EditPlanShot+Job，低清拼接含音轨补齐 | test_ffmpeg_integration.py；test_review_validation.py::test_stale_request_plan_is_preserved_and_roughcut_rejects_it | tsb362 有7镜头下载证据；只作为可选节奏预览，不发展最终渲染 |
| 对话导演、版本差异 | 当前 App 路由与 AIDirector/Export 无此工作区 | 未找到 conversation/plan-operation/version API | models 无方案版本/消息/操作实体；model_client.chat 是传输调用，不是会话领域 | 未发现对应端到端用例 | GAP：全部为新增能力 |

表内前端文件位于 frontend/src/pages（组件另注）；API 位于 backend/app/api；服务位于 backend/app/services；模型位于 backend/app/models.py；测试位于 backend/tests 或对应前端页面旁。

## 2. 导出字段逐项差距

CODE：timeline_export._shot_dict、export_csv、export_otio；DB：EditPlanShot。

| 字段 | 现状 | 目标差距 |
|---|---|---|
| 镜头次序/双 ID/入出点/目标时长 | DB 与 JSON 有；CSV 仅次序、素材名和时间 | CSV 增双 ID、唯一来源标识，明确实际采用的源区间 |
| 方案标题/主题/风格/总目标时长 | 总时长是镜头求和；Project.settings 为自由字典 | 需有校验的策划简报与版本快照 |
| 开场/铺垫/高潮/结尾 | 单镜头 story_role，UI 选项存在 | 无章节实体、章节叙事目的和整体叙事约束 |
| 选择理由/画面描述/上下文衔接 | ReviewRequest.reason 是复核原因；EditPlanShot 无选择理由字段 | 不可混用；增加理由与来源证据引用 |
| 音乐/音频/调色/转场 | 提示字符串已有 | 可复用；音乐 cue 不是自动配乐或版权许可 |
| 景别/运动/字幕旁白/不确定性 | 计划条目没有完整结构 | 有证据才填写；推断和待确认显式展示 |
| 证据/确认状态 | evidence 字符串32字、confidence、review_status | 非证据图谱；需引用分析版本/时间帧/人工判断，失效需派生检查 |
| 可读指导书 | 无 Markdown/PDF/打印指导书入口 | 新增完整阅读导出；不依赖用户再询问 AI |

## 3. 历史证据与不可外推事项

- E2E：docs/validation/v2.1-m5/WINDOWS_REAL_FOOTAGE.md 记录6段同型4K HEVC，工具驱动视觉与多帧路径；明确未覆盖竖屏/无音轨/HDR与8–10段要求。
- E2E：docs/validation/tsb362/e2e-multiframe.md（2026-11-04）记录单段10.76秒、7镜头、35张JPEG、正式 API→Job→DB，多帧7/7；代理 total=0 不证明实际代理生成。GET 返回结果不等于真人逐卡 UI 验收；内容准确率与镜头粒度未做人工真值复核。
- DOC 冲突：RELEASE_TSB_362.md 和 tsb362/README.md 仍有阶段 C 未执行措辞，而上述独立多帧记录已存在。应按模式与记录分别陈述，不能把“完全没有真实 E2E”或“全部已验收”作为结论。本轮不改历史记录。
- TEST 历史状态：tsb362/full-suite-result.md 记录1379 passed、1 failed、5 skipped，非全绿；tsb361 的旧全绿不覆盖该事实。本轮执行测试数为0，无新增 passed 数字。
- 大规模性能、全套真实素材门禁、目标 NLE 导入、自动导演方案质量均待验证。原生视频 Provider、G5/G6、A/B 冻结，不在本轮重新审查或调用范围。

## 4. 缺失证据的检索范围

检查 App.tsx 的实际路由、AIDirector/Export/Review/Home/Project、lib/api/types、api/projects/assets/segments/story/export/jobs、models/schemas、story_planner/timeline_export/roughcut/indexer/detector/frames/quality/analyzer，并检索 conversation、StoryboardVersion、PlanOperation、chat、story_outline。未发现会话与版本闭环。结论限定于当前基线；文档规划或函数名本身不计为实现。
