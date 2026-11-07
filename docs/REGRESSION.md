# 回归测试清单（Regression Checklist）

本清单对应代码审查报告中的 T01–T14 场景。**自动化部分**由 `backend/tests/`
覆盖（`python -m pytest`）；**手动部分**需要 FFmpeg 与真实素材，按下面的步骤执行。

普通单测矩阵不依赖 FFmpeg；独立 Ubuntu/Windows 媒体矩阵**强制安装** FFmpeg 与
FFprobe，并设置 `TSB_REQUIRE_FFMPEG=1`，缺工具、合成失败或测试 skip 均让 Job 失败。
本地未装 FFmpeg 时仍可单独跑普通单测，但不得把媒体 skip 当作验收通过。

涉及真实素材与播放器的主观判断仍保持手动，记录在下方表格里。

---

## 一、自动化（CI 每次执行）

```bash
cd backend
python -m pytest -q
```

| 用例 | 覆盖的审查项 | 断言要点 |
| --- | --- | --- |
| `test_security.py::test_token_endpoint_does_not_exist` | P0-01 | `/api/system/token` 返回 404 |
| `test_security.py::test_api_requires_token` | P0-01 | 错误令牌 → 401 |
| `test_security.py::test_health_and_config_are_reachable_without_token` | P0-01 | 白名单接口可用且不泄露令牌 |
| `test_security.py::test_database_file_is_not_downloadable` | P0-01 | `path=travel_storyboard.db` → 400 |
| `test_security.py::test_token_file_is_not_downloadable` | P0-01 | `path=token.txt` → 400 |
| `test_security.py::test_media_rejects_escapes_and_foreign_projects` | P0-01 | `..` 穿越、别的项目 → 400/404 |
| `test_security.py::test_media_rejects_disallowed_extension` | P0-01 | 缓存目录内的 `.exe` 也拒绝 |
| `test_security.py::test_media_serves_whitelisted_artifact` | P0-01 | 合法缩略图仍返回 200 `image/jpeg` |
| `test_security.py::test_cross_site_origin_is_rejected` | P0-01 | 跨站 Origin → 403 |
| `test_security.py::test_non_loopback_host_is_rejected` | P0-01 | 非回环 Host → 403（防 DNS rebinding） |
| `test_data_integrity.py::test_failed_probe_creates_exactly_one_asset` | P0-02 | 损坏文件只产生 1 行 Asset，重复扫描不新增 |
| `test_data_integrity.py::test_unique_constraint_on_project_and_path` | P0-02 | `(project_id, source_path)` 唯一约束生效 |
| `test_data_integrity.py::test_detected_key_is_immutable_when_trimmed` | P0-03 | `detected_start_sec=0.0` 不回退到可编辑值 |
| `test_data_integrity.py::test_trim_validation_rejects_out_of_window` | P0-03 | 越界/倒置/过短裁剪 → 422 |
| `test_data_integrity.py::test_trim_inside_window_is_accepted_and_flagged` | P0-03 | 合法裁剪写入且 `is_trimmed=true` |
| `test_data_integrity.py::test_reset_trim_restores_detected_bounds` | P0-03 | 重置回到检测边界 |
| `test_jobs.py::test_second_exclusive_job_is_rejected` | P0-04 | 同项目并发重活 → 409 |
| `test_jobs.py::test_cancel_endpoint_marks_job_cancelled` | P0-04 | 取消接口 + `is_cancelled` |
| `test_jobs.py::test_failures_make_the_job_partial` | P1-08 | 有失败项 → `partial_failed` |
| `test_jobs.py::test_raising_task_is_marked_failed` | P0-04 | 任务抛异常 → `failed` + 错误信息 |
| `test_jobs.py::test_cancelled_task_is_marked_cancelled` | P0-04 | `JobCancelled` → `cancelled` |
| `test_jobs.py::test_new_job_rows_start_with_empty_failures` | P1-08 | 新任务 `failures` 初始化为空 |
| `test_output_dir.py::test_output_dir_is_reflected_in_cache_dir` | P1-02 | `cache_dir = output_dir/<项目ID>` |
| `test_output_dir.py::test_default_location_when_output_dir_empty` | P1-02 | 留空时回落到数据目录 |
| `test_output_dir.py::test_artifact_dirs_follow_output_dir` | P1-02 | 新项目的帧/代理/包/粗剪目录位于配置位置 |
| `test_output_dir.py::test_output_dir_change_moves_workspace` | P1-02 | 空工作区变更成功（有数据必须 409，不自动迁移） |
| `test_output_dir.py::test_output_dir_matching_data_dir_is_rejected` | P1-02 | 输出目录 = 数据目录 → 422 |
| `test_output_dir.py::test_output_dir_overlapping_source_root_is_rejected` | P1-02 | 输出在素材内 → 422 |
| `test_output_dir.py::test_output_dir_containing_source_root_is_rejected` | P1-02 | 输出包含素材 → 422 |
| `test_output_dir.py::test_patch_to_overlapping_output_dir_is_rejected` | P1-02 | PATCH 同样校验 |
| `test_output_dir.py::test_paths_overlap_semantics` | P1-02 | 重叠判定（父子/相等/无关） |
| `test_output_dir.py::test_media_route_serves_artifact_from_custom_output_dir` | P1-02 + P0-01 | 自定义目录下的帧仍可授权访问 |
| `test_output_dir.py::test_media_route_still_rejects_outside_artifacts_with_custom_root` | P1-02 + P0-01 | 绝对路径也不能越出白名单 |
| `test_output_dir.py::test_relative_data_paths_still_resolve` | P1-02 | 默认位置仍存相对路径 |
| `test_review_package_privacy.py::test_sanitized_copy_drops_exif` | P1-07 | 重编码后 EXIF/GPS 字节确实消失 |
| `test_review_package_privacy.py::test_tool_label_has_no_directory` | P1-07 | manifest 不再写 ffmpeg 绝对路径 |
| `test_review_package_privacy.py::test_package_contains_no_source_paths_or_exif` | P1-07 | 整包不含源路径与相机元数据 |
| `test_review_package_privacy.py::test_package_only_lists_assets_used_by_selection` | P1-07 | 只收录被选中的素材 |
| `test_review_package_privacy.py::test_budget_never_deletes_files_it_does_not_own` | P1-07 | 预算越界时不得误删外部文件 |
| `test_review_package_privacy.py::test_tiny_budget_truncates_package` | P1-07 | 超预算时标记 truncated |
| `test_story_direct.py::test_selection_spreads_across_assets` | P1-06 | 抽样跨素材分散 |
| `test_story_direct.py::test_coverage_reports_what_was_not_seen` | P1-06 | 明确报告未覆盖镜头数 |
| `test_story_direct.py::test_validate_rejects_invented_segment_ids` | P1-06 | 虚构 segment_id 被拒绝 |
| `test_story_direct.py::test_validate_clamps_out_of_range_times` | P1-06 | 越界时间被夹到片段范围 |
| `test_story_direct.py::test_direct_analyze_retries_on_non_json` | P1-06 | 非 JSON 输出自动重试一次 |
| `test_story_direct.py::test_direct_analyze_happy_path` | P1-06 | 通过的请求落库为 ReviewRequest |
| `test_timeline_export.py::test_cross_asset_reference_is_rejected` | P1-05 | segment 与 asset 不匹配被拒绝 |
| `test_timeline_export.py::test_non_finite_times_are_rejected` | P1-05 | NaN/Inf/非数字被拒绝 |
| `test_timeline_export.py::test_target_longer_than_source_is_rejected` | P1-05 | 目标时长超过源区间被拒绝 |
| `test_timeline_export.py::test_array_order_wins_and_timeline_is_contiguous` | P1-05 | 按数组顺序重编号，时间轴连续 |
| `test_timeline_export.py::test_validation_is_all_or_nothing` | P1-05 | 校验失败不破坏既有计划 |
| `test_timeline_export.py::test_json_csv_otio_agree_on_shots_and_duration` | P1-05 | 三种导出镜头数与时长一致 |
| `test_timeline_export.py::test_otio_round_trips_through_opentimelineio` | P1-05 | OTIO 可被参考实现读回 |
| `test_ffmpeg_integration.py`（需 FFmpeg，独立 CI 必跑） | M1/M2 | 真实解码：探测元数据、建库不重复、抽帧、带音频预览、检测出硬切、分析后重跑保留标注且帧数不增长、粗剪时长收敛 |

### v2.0.4 新增（TSB-241 ~ TSB-244）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `test_long_paths.py::test_long_path_is_identity_on_posix` | TSB-241 | 非 Windows 路径行为不变 |
| `test_long_paths.py::test_long_path_prefixes_drive_paths` | TSB-241 | 盘符路径加 `\\?\` 前缀 |
| `test_long_paths.py::test_long_path_handles_unc_shares` | TSB-241 | UNC → `\\?\UNC\`，不产生双重前缀 |
| `test_long_paths.py::test_long_path_is_idempotent` | TSB-241 | 重复转换不叠加前缀 |
| `test_long_paths.py::test_path_exceeds_limit_threshold` | TSB-241 | 260 字符阈值判定 |
| `test_long_paths.py::test_path_length_warning_only_near_limit` | TSB-241 | 仅接近上限时给出短目录建议 |
| `test_long_paths.py::test_run_raises_output_not_created` | TSB-241 | 退出码 0 但未写文件 → `OUTPUT_NOT_CREATED` |
| `test_long_paths.py::test_run_raises_when_pattern_matches_nothing` | TSB-241 | 抽帧 pattern 无匹配 → 报错而非静默 |
| `test_long_paths.py::test_run_classifies_missing_executable` | TSB-241 | 可执行文件缺失 → `EXECUTABLE_MISSING` |
| `test_long_paths.py::test_run_classifies_command_failure` | TSB-241 | 非零退出 → `COMMAND_FAILED` |
| `test_long_paths.py::test_safe_is_file_propagates_permission_error` | TSB-241 | **被锁文件不得被当成「不是文件」**（仅 Linux 会暴露） |
| `test_long_paths.py::test_safe_stat_propagates_permission_error` | TSB-241 | `safe_stat` 同样不得吞 `PermissionError` |
| `test_long_paths.py::test_safe_is_file_still_true_for_readable_file` | TSB-241 | 正常文件仍为 True |
| `test_long_paths.py::test_safe_is_file_false_for_missing_file` | TSB-241 | 缺失文件为 False |
| `test_long_paths.py::test_safe_is_file_false_for_directory` | TSB-241 | 目录为 False |
| `test_long_paths.py::test_scanner_reports_locked_video_instead_of_skipping` | TSB-241 | 被锁视频必须被扫描发现并报失败，而非静默跳过 |
| `test_long_paths.py::test_detector_raises_when_all_frames_unreadable` | TSB-241 | 抽样帧全不可读 → `MEDIA_UNREADABLE`，**不再**退化成单镜头假成功 |
| `test_long_paths.py::test_detector_warns_but_continues_on_partial_read` | TSB-241 | 部分帧不可读仍可检测且记 warning |
| `test_long_paths.py::test_read_image_handles_cjk_and_spaces` | TSB-241 | 中文+空格路径下检测正常 |
| `test_long_paths.py::test_concat_manifest_uses_single_quotes` | TSB-241 | 清单用单引号、无 BOM |
| `test_long_paths.py::test_concat_manifest_escapes_single_quote` | TSB-241 | 文件名含单引号可正确转义 |
| `test_long_paths.py::test_concat_manifest_uses_forward_slashes` | TSB-241 | 反斜杠不被 ffconcat 当转义吞掉 |
| `test_long_paths.py::test_concat_manifest_does_not_use_extended_prefix` | TSB-241 | 清单内不用 `\\?\` 前缀 |
| `test_long_paths.py::test_deep_workspace_extract_frame` | TSB-241 | ≥270 字符路径下抽帧，Python 可读回 |
| `test_long_paths.py::test_deep_workspace_detector_end_to_end` | TSB-241 | ≥270 字符路径下检测出真实切点，不塌缩为单镜头 |
| `test_long_paths.py::test_deep_workspace_full_index_and_analyze` | TSB-241 | 深路径下 扫描→检测→抽帧→HTTP 预览 全通，且原片字节不变 |
| `test_long_paths.py::test_deep_workspace_roughcut_end_to_end` | TSB-241 | 深路径下粗剪 concat 与发布成功且可被 ffprobe 解码 |
| `test_job_mutex.py::test_invalid_job_type_is_rejected` | TSB-242 | 非法 job_type 被拒 |
| `test_job_mutex.py::test_unknown_project_is_rejected` | TSB-242 | 未知项目被拒 |
| `test_job_mutex.py::test_second_heavy_job_is_rejected_with_details` | TSB-242 | 409 且带冲突任务类型与 ID |
| `test_job_mutex.py::test_any_heavy_type_conflicts_with_a_running_heavy_job` | TSB-242 | 5 种重活两两互斥 |
| `test_job_mutex.py::test_no_job_row_is_created_for_a_rejected_request` | TSB-242 | 被拒请求不留下 Job 行 |
| `test_job_mutex.py::test_proxy_endpoint_blocks_when_a_heavy_job_runs` | TSB-242 | **原缺陷**：代理入口遵守互斥 |
| `test_job_mutex.py::test_proxy_endpoint_blocks_a_second_proxy_job` | TSB-242 | 双击「生成预览」只建一个任务 |
| `test_job_mutex.py::test_proxy_endpoint_accepts_when_idle` | TSB-242 | 空闲时 202 + JobOut 格式不变 |
| `test_job_mutex.py::test_proxy_endpoint_404_for_unknown_project` | TSB-242 | 未知项目 404 |
| `test_job_mutex.py::test_concurrent_creates_yield_exactly_one_job` | TSB-242 | **6 线程并发**恰好 1 个成功（前端禁用挡不住的场景） |
| `test_job_mutex.py::test_sequential_creates_after_completion_are_allowed` | TSB-242 | 任务结束后可再次创建 |
| `test_job_mutex.py::test_different_projects_do_not_block_each_other` | TSB-242 | 互斥是项目级而非全局 |
| `test_job_mutex.py::test_proxy_job_config_snapshot_is_unchanged` | TSB-242 | `max_total_mb` 快照不变 |
| `test_job_mutex.py::test_generic_proxy_job_matches_specialised_endpoint` | TSB-242 | 两个入口 config 与类型一致 |
| `test_job_mutex.py::test_proxy_job_runs_and_completes` | TSB-242 | 走完真实调度并到达终态 |
| `test_proxy_duration.py::test_exact_duration_is_accepted` | TSB-243 | 时长完全一致通过 |
| `test_proxy_duration.py::test_tolerable_drift_is_accepted` | TSB-243 | 取整级漂移、短请求、长请求 10% 内通过 |
| `test_proxy_duration.py::test_material_drift_is_rejected` | TSB-243 | 1.2 倍、近乎空、+50% 被拒 |
| `test_proxy_duration.py::test_invalid_actual_duration_is_rejected` | TSB-243 | 0/负/NaN/Inf 被拒 |
| `test_proxy_duration.py::test_tolerance_is_the_more_generous_of_the_two` | TSB-243 | 短请求由绝对值主导、长请求由相对值主导 |
| `test_proxy_duration.py::test_error_message_names_both_durations` | TSB-243 | 错误信息含实际与请求时长 |
| `test_proxy_duration.py::test_wrong_length_proxy_is_not_published` | TSB-243 | 错误时长不发布，`partial_failed` + `[DURATION_MISMATCH]` |
| `test_proxy_duration.py::test_correct_length_proxy_is_published` | TSB-243 | 正确时长正常 `proxied` |
| `test_proxy_duration.py::test_failed_proxy_preserves_previous_publication` | TSB-243 | 重跑失败不覆盖既有代理 |
| `test_proxy_duration.py::test_version_column_matches_generated_length` | TSB-244 | 列宽 ≥ 69，与生成值匹配 |
| `test_proxy_duration.py::test_generated_version_fits_and_is_unique` | TSB-244 | 两次构建版本不同且都不超列宽 |

> TSB-241 的深路径用例仅在 Windows 上运行（非 Windows 自动 skip）；
> 其余用例在所有平台运行。本机 `LongPathsEnabled=0`，即验收要求的场景。

### v2.0.5 新增（TSB-251 ~ TSB-254）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `test_job_cancel_kills_process.py::test_cancelled_run_kills_child_and_raises` | TSB-253 | 取消回调为真 → 立刻终止子进程并抛 `CANCELLED`，**不等编码跑完** |
| `test_job_cancel_kills_process.py::test_cancelled_run_actually_reaps_the_os_process` | TSB-253 | 子进程在**操作系统层面**确实消失，不是被遗弃 |
| `test_job_cancel_kills_process.py::test_uncancelled_run_with_check_still_returns_normally` | TSB-253 | 回调恒为假不影响正常运行 |
| `test_job_cancel_kills_process.py::test_cancel_check_result_is_read_as_truthy_per_poll` | TSB-253 | 回调被**反复轮询**，迟到的取消同样生效 |
| `test_job_cancel_kills_process.py::test_run_without_cancel_check_is_unchanged` | TSB-253 | 不传回调时保持 `subprocess.run` 原行为 |
| `test_job_cancel_kills_process.py::test_run_without_cancel_check_still_classifies_missing_executable` | TSB-253 | 快路径仍分类 `EXECUTABLE_MISSING` |
| `test_job_cancel_kills_process.py::test_run_without_cancel_check_still_classifies_failure` | TSB-253 | 快路径仍分类 `COMMAND_FAILED` |
| `test_job_cancel_kills_process.py::test_run_without_cancel_check_still_classifies_timeout` | TSB-253 | 快路径仍分类 `TIMEOUT` |
| `test_job_cancel_kills_process.py::test_timeout_also_applies_when_cancel_check_is_present` | TSB-253 | 轮询循环**不吞掉**超时分类 |
| `test_job_cancel_kills_process.py::test_cancel_wins_over_timeout_when_both_are_due` | TSB-253 | 同时到期时取消优先，不误报超时 |
| `test_job_cancel_kills_process.py::test_cancel_check_for_is_none_without_a_job` | TSB-253 | 无任务时返回 `None`，一次性检测保持原路径 |
| `test_job_cancel_kills_process.py::test_cancel_check_for_tracks_the_job_row` | TSB-253 | 回调读**实时**任务状态，不是过期快照 |
| `test_job_cancel_kills_process.py::test_job_cancelled_media_process_is_not_marked_failed` | TSB-253 | **原缺陷**：被杀的媒体进程是取消，不得被写成 `failed` |
| `test_job_cancel_kills_process.py::test_successful_job_with_a_cancel_check_still_succeeds` | TSB-253 | 透传回调不改变成功路径 |
| `test_job_cancel_kills_process.py::test_genuine_media_failure_is_still_reported_as_failed` | TSB-253 | 只有 `CANCELLED` 特殊处理，真实媒体错误仍为 `failed` |
| `test_job_cancel_kills_process.py::test_job_cancelled_from_inside_a_task_keeps_cancelled_state` | TSB-253 | 既有 `JobCancelled` 路径无回归 |
| `test_job_cancel_kills_process.py::test_cancelling_one_run_does_not_kill_a_concurrent_run` | TSB-253 | 终止**只作用于自己启动的进程树**，不误伤并发任务 |
| `test_job_cancel_kills_process.py::test_roughcut_passes_a_cancel_check_to_its_encodes` | TSB-253 | 粗剪确实**传入**回调，且取消不被记为单个失败片段 |
| `test_job_cancel_kills_process.py::test_proxy_task_passes_a_cancel_check_to_its_encodes` | TSB-253 | 代理（最长编码）确实传入回调并中止整个任务 |
| `test_job_cancel_kills_process.py::test_analyze_task_passes_a_cancel_check_to_sampling` | TSB-253 | 检测抽样确实传入回调，取消不被记成单个素材失败 |
| `test_job_cancel_kills_process.py::test_detector_forwards_cancel_check_into_its_sampling_run` | TSB-253 | `detector` 把回调转发进抽样的 `run()` |
| `test_job_cancel_kills_process.py::test_detect_segments_keeps_its_original_signature_contract` | TSB-253 | `cancel_check` 默认 `None`，无任务调用者行为不变 |

> TSB-253 的关键断言是**时延**，不只是终态：旧实现同样会到达 `cancelled`，
> 只是要等编码跑完。因此这些用例同时断言"停得够快"与"子进程真的死了"。
> 本机实测（`tools/probe_cancel_latency.py`）：同一 8 秒媒体命令，
> 旧路径 8.44s 才返回，新路径 0.73s 即中止，回收 7.71s。

### v2.0.6 新增（TSB-261 ~ TSB-264）

`test_cancel_lifecycle.py`（14）—— 取消中状态与互斥状态机

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `::test_cancelling_is_non_terminal_and_exclusive` | TSB-261 | `cancelling` 既非终态、又在互斥集合内；`ACTIVE_STATUSES` 仍只含 `pending/running` |
| `::test_cancelling_is_not_treated_as_runnable` | TSB-261 | `cancelling` **不得**被改回 `running` |
| `::test_cancel_while_running_enters_cancelling` | TSB-261 | **原缺陷**：取消只请求，不直接跳终态；不提前写 `finished_at` |
| `::test_new_exclusive_job_is_rejected_while_cancelling` | TSB-261 | **原缺陷**：旧任务真正退出前，新互斥任务必须 409 且不建行 |
| `::test_exclusive_jobs_allowed_after_the_worker_really_stops` | TSB-261 | worker 退出后恢复正常受理（TC-02） |
| `::test_pending_job_cancelled_before_it_runs_converges` | TSB-261 | 未调度的取消不会永远停在 `cancelling`（TC-03） |
| `::test_repeated_cancel_is_idempotent` | TSB-261 | 重复取消幂等返回 200/cancelling（TC-04） |
| `::test_concurrent_cancels_produce_one_clean_terminal_state` | TSB-261 | 6 线程并发取消只有一个干净终态（TC-04） |
| `::test_cancel_of_a_finished_job_keeps_its_terminal_state` | TSB-261 | 终态不可变，迟到取消 409（任务书 §2.5） |
| `::test_natural_success_is_not_overwritten_by_a_cancel` | TSB-261 | 成功与取消竞争时终态唯一（TC-05） |
| `::test_cancelled_job_does_not_get_a_failure_entry` | TSB-261 | 取消不污染失败清单（TC-06） |
| `::test_recovery_does_not_rerun_a_cancelling_job` | TSB-261 | 重启不重跑已请求取消的任务（TC-10） |
| `::test_recovery_still_resumes_genuinely_interrupted_jobs` | TSB-261 | 无回归：`pending/running` 仍被重新派发 |
| `::test_another_project_is_unaffected_by_a_cancelling_job` | TSB-261 | 互斥是项目级，B 不受 A 取消影响（TC-08） |

`test_cancel_cleanup.py`（21）—— 暂存资源必达清理

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `::test_cleanup_removes_a_task_owned_stage` | TSB-262 | 本任务暂存被真正删除 |
| `::test_cleanup_is_idempotent_on_a_missing_stage` | TSB-262 | 重复清理不误报失败 |
| `::test_cleanup_refuses_the_workspace_root` | TSB-262 | 拒绝项目工作区根 |
| `::test_cleanup_refuses_published_directories` | TSB-262 | 拒绝 `frames/proxies/roughcut/packages`（TC-13） |
| `::test_cleanup_refuses_a_path_outside_the_project` | TSB-262 | 拒绝项目外路径 |
| `::test_cleanup_refuses_the_data_root_and_user_source_dir` | TSB-262 | 拒绝数据目录与素材目录 |
| `::test_cleanup_allows_analyzer_staging_pattern` | TSB-262 | `staging/<asset>/<token>` 合法 |
| `::test_cleanup_allows_proxies_staging_pattern` | TSB-262 | `proxies/staging/<token>` 合法 |
| `::test_cleanup_reports_failure_instead_of_swallowing_it` | TSB-262 | 清理失败可观测、可重试；**不**用 `ignore_errors` 当证据（TC-17） |
| `::test_cleanup_result_carries_traceable_details` | TSB-262 | 结果含残留路径/字节/重试标记 |
| `::test_count_tree_measures_before_deleting` | TSB-262 | 统计不修改文件 |
| `::test_cancelled_roughcut_removes_its_staging` | TSB-262 | **原缺陷**：取消粗剪回收暂存（TC-11/12） |
| `::test_failed_roughcut_removes_its_staging` | TSB-262 | 失败路径同样回收 |
| `::test_roughcut_staging_is_cleaned_on_the_success_path_too` | TSB-262 | 无回归：成功路径照常发布并清理 |
| `::test_stale_roughcut_output_survives_a_cancelled_rerun` | TSB-262 | **不**删除历史正式产物（TC-12） |
| `::test_cancelled_detection_does_not_leak_frame_staging` | TSB-262 | 取消抽帧回收暂存（TC-14） |
| `::test_cleanup_handles_deep_staging_paths` | TSB-262 | ≥270 字符 stage **真的**被删除（TC-18） |
| `::test_cleanup_counts_deep_staging_bytes` | TSB-262 | 深路径字节统计正确 |
| `::test_cleanup_refuses_a_symlink_pointing_outside` | TSB-262 | **严重**：链接逃逸时外部数据绝不被删（TC-20） |
| `::test_cleanup_accounting_matches_what_actually_disappeared` | TSB-262 | 计数反映真实消失量 |
| `::test_staging_is_cleaned_before_the_job_reports_cancelled` | TSB-262 | 清理先于终态与释放互斥 |

`test_media_process_tree.py`（10）—— 进程树定向终止

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `::test_tree_harness_really_creates_a_grandchild` | TSB-263 | 守卫测试本身有效：链路是真实存活的两个进程 |
| `::test_cancel_kills_the_whole_descendant_tree` | TSB-263 | **原缺陷**：取消回收父**与孙**进程（TC-21/22） |
| `::test_cancel_reports_only_after_the_tree_is_stopped` | TSB-263 | 报告取消时树已确实停止 |
| `::test_timeout_also_reclaims_the_tree` | TSB-263 | 超时同样回收（TC-23） |
| `::test_parent_exiting_first_still_reclaims_the_grandchild` | TSB-263 | 父进程先退出仍回收孙进程（区分真进程树回收） |
| `::test_cancelling_one_run_leaves_an_unrelated_run_alone` | TSB-263 | 不误杀并发任务（TC-22） |
| `::test_cancel_between_polls_still_reclaims_late_descendants` | TSB-263 | 迟到的后代也被回收 |
| `::test_terminate_errors_do_not_leave_the_tree_running` | TSB-263 | `terminate()` 抛异常仍不留活进程 |
| `::test_grace_period_is_short_enough_to_be_useful` | TSB-263 | 宽限期上限 |
| `::test_no_global_name_based_kill_is_used` | TSB-263 | **静态守卫**：禁用 `/IM`、`pkill`、`killall` |

`test_cancel_recovery.py`（10）—— 回归矩阵与端到端一致性

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `::test_job_list_reports_cancelling_and_the_blocking_job` | TSB-264 | HTTP 列表暴露 `cancelling` 与阻塞对象（TC-24） |
| `::test_single_job_endpoint_matches_the_list` | TSB-264 | 单任务接口与列表一致（TC-24） |
| `::test_cancelled_run_preserves_ratings_notes_trims_and_plan` | TSB-264 | 评分/备注/裁剪/计划/检测边界全保留（TC-25） |
| `::test_rerun_after_a_cancel_still_works_and_keeps_user_state` | TSB-264 | 取消后可重跑且人工状态不丢（TC-25） |
| `::test_cancelled_scan_preserves_checkpoints` | TSB-264 | 取消保留断点，下次可续跑（TC-25） |
| `::test_multiple_projects_cancel_concurrently_without_deadlock` | TSB-264 | 多项目并发取消不死锁（TC-26） |
| `::test_queued_job_beyond_the_pool_is_cancelled_without_running` | TSB-264 | 超出线程池的排队任务取消后收敛且不执行（TC-03/26） |
| `::test_interleaved_create_and_cancel_across_projects` | TSB-264 | 反复创建/取消不重复派发（TC-26） |
| `::test_artifacts_are_not_cross_deleted_between_projects` | TSB-264 | 跨项目产物不被误删（TC-26） |
| `::test_repeat_request_stays_blocked_until_cleanup_finishes` | TSB-264 | 阻断持续到**清理完成**而非点击瞬间（TC-29） |

`test_ci_skip_guard.py`（3）—— 强制媒体作业的 skip 守卫（TSB-266）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| `::test_platform_skips_are_tolerated` | TSB-266 | Windows 专属套件在 Linux 上 skip **不**得让媒体作业失败 |
| `::test_tool_missing_skips_still_fail_the_job` | TSB-266 | 工具缺失类 skip **仍然必须**失败，守卫不得被放宽到失效 |
| `::test_every_skip_reason_in_the_suite_is_recognised` | TSB-266 | 扫描全部测试源码：每个 `skipif` 原因要么是平台事实、要么显式列为应失败 |

> `test_ci_skip_guard.py` 是**自覆盖**守卫：新增一个带未登记 skip 原因的套件会立即失败。
> 它由一次真实的 Ubuntu CI 失败催生（深路径套件加入媒体作业后，其 Linux 合法 skip
> 被当成意外 skip）。反向验证：移除白名单条目后用例立即点名该原因并失败。

> **测试纪律**：并发用 `Event`/`Barrier` 等可观察条件同步，不用 `sleep` 当竞争验证。
> 状态机、清理、进程树三项均做反向验证：撤去保护后对应用例立即失败（记录见发布文档）。

---

## 二、手动回归（需要 FFmpeg + 真实素材）

准备一个 **2–5 个短片**的小集合，不要一上手就用 200GB 全量库。

| 编号 | 场景 | 步骤 | 期望结果 |
| --- | --- | --- | --- |
| T01 | 首次扫描 | 新建项目 → 添加素材根目录 → 扫描 | 每个文件一行 Asset；`scan_status` 正确 |
| T02 | 损坏/空文件 | 放一个 0 字节或截断的 `.mp4`，扫描 | 恰好一行 Asset，`scan_status=failed`，`error_code=probe_failed`；再次扫描不新增行 |
| T03 | 裁剪后重复检测与复核 | 标注镜头（收窄入出点 + 收藏）→ 强制重新检测 ≥2 次；M5 confirm/reject 加备注后原样/改区间重跑 | 镜头 id、收藏、备注、裁剪区间保留；`detected_*` 不变；相同有效证据保留判定，区间/模型证据变化须重新确认且历史可查（TSB-314/319） |
| T04 | 增量检测 | 不改素材再点一次「检测」（不勾选强制） | 任务 `skipped ≥ 1`，磁盘帧数不增长 |
| T05 | 帧缓存回收 | 连续强制检测 3 次 | `<项目工作区>/frames/` 文件数稳定，不随次数增长 |
| T06 | 中途杀进程 | 检测进行中直接结束后端进程 → 重启 | 任务被重新派发（`resume_interrupted_jobs`），不产生重复镜头 |
| T07 | 并发保护 | 检测运行中再点扫描/打包 | 第二次请求返回 409 并给出正在运行的任务编号 |
| T08 | 取消 | 打包/代理运行中点取消 | 任务状态 `cancelled`，已产出的临时文件不污染正式目录；**进程应立即停止**（见 T20） |
| T09 | 部分失败可见 | 让某个素材不可读后跑打包 | 任务 `partial_failed`，前端列出失败条目，成功部分仍可用 |
| T10 | 授权边界 | 直接请求 `/api/system/token`、`/api/projects/<pid>/media?path=token.txt`；M5 已有 Key 下关闭总开关/不勾本次同意，检查新旧入口 | 404 / 400，绝不返回内容；M5 零出站、错误可见；生产模型 HTTPS 且证书有效，明文开发例外关闭（TSB-317/319） |
| T11 | 跨站防护 | 用非回环 `Host`/`Origin` 发请求 | 403 |
| T12 | 分析包隐私 | 生成分析包并解压检查所有 JSON | 不出现 `source_path`、绝对路径、令牌 |
| T13 | 复核闭环 | 导入复核请求（含合法与非法各若干）→ 生成代理 → 播放 | 合法项 `proxied` 可播放；非法项被跳过并给出原因 |
| T14 | 导出与粗剪 | 保存剪辑计划 → 导出 CSV/JSON/OTIO → 生成粗剪 | 导出与时间线一致；缺镜头时粗剪标记为不完整 |
| T15 | 自定义输出目录 | 新建项目时把输出工作区设为**素材之外的另一个盘/目录** → 扫描 → 检测 → 打包 | 分镜图/代理/包写在输出目录下；`<数据目录>/projects/<id>` 为空；原片 SHA256 不变 |
| T16 | 输出目录重叠保护 | 把输出目录设为素材目录本身、其子目录、或其父目录 | 三种情况均返回 422 并说明原因，不创建项目 |
| T17 | 粗剪带声音 | 保存剪辑计划 → 生成粗剪 → 用播放器打开 | 粗剪含 AAC 立体声（48kHz）音轨；`resume_token` 中 planned 与 actual 时长一致 |
| T18 | 剪辑计划校验 | 提交跨素材引用、倒置区间、目标时长超过源区间、NaN | 四种均返回 `created=0` 并给出逐条原因；已有计划不被破坏 |
| T19 | OTIO 可导入 | 导出 OTIO → 在 DaVinci Resolve / Premiere（如支持）中导入 | 镜头数与时长与 CSV/JSON 一致；若 NLE 不支持请记录版本 |
| T20 | 取消**及时**生效 | 对一段较长素材生成代理（或跑粗剪），在编码进行中点「取消」，同时用任务管理器观察 `ffmpeg.exe` | `ffmpeg.exe` 在数秒内退出（不再跑完整个片段）；任务为 `cancelled` 而非 `failed`；项目锁被释放，可立即提交新任务 |
| T21 | 并发取消不误伤 | 分别在两个项目各起一个长任务，只取消其中一个 | 只有被取消的那个进程退出；另一个任务继续正常完成 |
| T22 | 取消窗口与互斥 | 长任务编码中点「取消」，**立刻**再点同一重活 | 取消后到进程真正退出前，新任务返回 **409** 且带阻塞任务类型与 ID；旧任务终态为 `cancelled` 后才返回 202；不能只把新任务排队 |
| T23 | 取消后暂存与历史产物 | 取消粗剪/代理/检测及 M5 视觉/多帧后检查工作区 | 本任务已登记 `staging-*`、`staging/<asset>/<token>`、`proxies/staging/<token>`、`vision/staging/<job>`、`vision/motion/<job>` 清空或明确报告残留；历史产物与人工编辑保留；兄弟/重启后未知 stage 不接管（TSB-318/319） |
| T24 | 深路径暂存回收 | 在工作区路径 ≥270/320 字符处跑一次任务并取消 | 文件与子目录**实际**删除（不是仅 API 返回成功）；界面/日志可见残留字节时能定位 |
| T25 | 链接逃逸防护 | 在工作区内放一个指向工作区**外**的目录链接（junction/symlink），置于待清理的暂存目录下，再触发清理 | 链接目标内容**绝不**被删除；结果给出可追踪的拒绝或残留记录 |

> T06/T08 结束后请确认 `<项目工作区>/staging/` 下没有遗留目录。
> T22–T25 是 v2.0.6 新增的手动项，对应 TSB-261/262/263；自动化已用合成命令覆盖等价场景，
> 但真实素材上的**实际时延**与用户体感仍需实机确认。

---

## 三、产物落盘位置

| 内容 | 位置 |
| --- | --- |
| SQLite、`token.txt` | `<数据目录>`（`TRAVELSB_DATA_DIR`，默认 `backend/data`），**不对外提供** |
| 分镜图 / 代理 / 分析包 / 粗剪 | `<项目工作区>/<项目ID>/...`，其中项目工作区 = `output_dir` 或数据目录下的 `projects/` |
| 原片 | 用户素材目录，**始终只读**，从不写入 |

项目工作区不能与素材目录重叠（含父子关系），否则创建/修改项目返回 422 —— 这是为了避免
产物覆盖原片。API 的 `cache_dir` 字段会返回实际落盘位置，前端在项目页与项目卡片上展示。

---

## 四、数据库迁移

`init_db()` 在启动时执行**幂等**的增量迁移：

* 自动为旧库补齐 `assets.analyzed_*`、`segments.detected_*`/`is_trimmed`、`jobs.failures`；
* 迁移前通过 SQLite Backup API（包含 WAL）生成快照，保存为 `<name>.pre-migration-<UTC时间戳>.bak`；
* 仅新加入的 `detected_*` 列取当时的 `start_sec`/`end_sec`；已有零秒检测边界保持。

**已知限制**：早于边界拆分版本且没有 detected_* 列的旧库镜头已裁剪时，原始检测边界无法还原，首次补列回填后等于裁剪后的
区间。这类镜头若再做一次检测，会被视为新镜头。升级后新产生的裁剪不受影响。


## v2.0.1 回归用例映射

结果及未覆盖项见 [RELEASE_V2_0_1.md](RELEASE_V2_0_1.md)。

| 任务书 RT | 自动化证据 | 实测边界 |
| --- | --- | --- |
| RT01～03 | test_story_direct.py：评分、长片挤占、60 素材 12 图片、确定性及缺图 | 模型调用使用 stub，图片绑定和覆盖率为真实 DB/文件检查 |
| RT04～05 | test_output_dir.py：默认/自定义完整及不完整 MP4 API、越界/白名单 | Windows 自定义路径由媒体 CI 覆盖，用户 Windows 实片待验收 |
| RT06～08 | test_stability_jobs.py：队列取消、最后项取消、终态 409、串行恢复 | 实际线程门控与 DB 条件更新 |
| RT09～10 | test_output_dir.py：有数据拒绝且旧媒体可读、空目录允许、停止线程保护 | DB 与路径缓存保持一致 |
| RT11～14 | test_checkpoints.py：同大小时间中部改动、增删、参数快照、失败去重恢复 | 快速采样碰撞和 full_hash 补充测试明确分开 |
| RT15、17 | frontend Export.test.tsx：恢复 10 个镜头、dirty 不覆盖、保存失败保留 | React Query + Router + 实际页面组件 |
| RT16 | test_timeline_export.py：越片段出点、非有限目标时长整批拒绝 | 旧计划行完整保留 |
| RT18～20 | test_ffmpeg_integration.py / test_cache_recovery.py | 真抽帧、重复分析、发布/DB/清理故障、孤儿宽限期、外部文件保护 |
| RT21 | test_ffmpeg_integration.py：6 秒三场景、切镜、代理音轨、粗剪精度 | 实际 FFmpeg；另含 3 fps 4K HEVC、横竖屏、无音轨 |
| RT22 | Ubuntu/Windows 媒体 CI 的中文路径生成素材替代验证 | 真实 Windows 旅行素材尚未获得，不能写通过 |
| RT23 | 综合链路报告记录 3 个生成视频 SHA-256 前后 | 真实原片三段哈希仍待用户素材验收 |
| RT24 | 自定义目录扫描→检测→包→复核代理→计划→粗剪、保存计划保持 | 3 个生成视频替代；5～10 段旅行实片和真实模型待验收 |

完整本地自动化命令：

```bash
cd backend
python -m pytest -q
# 设置 FFmpeg/FFprobe 路径或放入 PATH 后，强制媒体模式：
TSB_REQUIRE_FFMPEG=1 python -m pytest -v tests/test_ffmpeg_integration.py tests/test_cache_recovery.py
cd ../frontend
pnpm test
pnpm typecheck
pnpm lint
pnpm build
```

Windows PowerShell：先 `$env:TSB_REQUIRE_FFMPEG="1"`，再执行媒体 pytest 命令。

### Windows 实片待执行记录

选择 5～10 段视频，记录 CPU/GPU、RAM、磁盘、Windows/FFmpeg 版本、总时长及大小。
覆盖几十秒与 10 分钟以上、4K、HEVC/H.264、横竖屏、中文路径、音轨有/无、VFR、HDR；
没有的类别明确填“未覆盖”。至少三段使用 `Get-FileHash -Algorithm SHA256` 分析前后对比。
分别验证默认及自定义工作区；操作收藏/裁剪，强制检测、重启、计划刷新、取消最后一项；
检查 ID/标记/媒体引用/计划不丢，粗剪音画及时间正确。记录任务管理器峰值内存和缓存大小，
故意加入一个损坏文件观察其余任务正常完成。所有未执行项不得打勾。

## v2.0.2（TSB-221～226）补充回归

新测试均使用稳定 ID/路径；并发交错由 Event 控制，不通过 sleep 碰执行顺序。
`test_concurrent_annotations.py` 与 `test_hash_pipeline.py` 是无 FFmpeg 的生成 JPEG/文件
测试，四组普通后端 CI 均执行；独立 Windows/Ubuntu 媒体 Job 也执行这些测试。

| 本轮 RT | 对应证据 | 预期与覆盖边界 |
| --- | --- | --- |
| RT01～03 | concurrent_annotations：rating/note/trim/role | 真线程抽帧暂停 → 用户 API 写入 → 后台提交，四类最新字段保留 |
| RT04 | 同文件的 protected 参数化与 plan_created_during_extraction | 计划/复核/注释/收藏/角色/裁剪/确认状态保护旧 ID 和旧帧；新增计划引用也保护 |
| RT05～06 | 并发故障参数化 + cache_recovery | 抽帧/文件发布/提交/取消后，已保存最新人工字段和旧 JPEG 完整 |
| RT07 | stability_jobs：creation_does_not_enumerate、real_background_hash | 创建接口不调用 manifest、不读视频，阻塞后台首块仍 202；实测耗时写 JUnit |
| RT08 | checkpoints：hash_cancellation | 首块后取消，未继续读完整文件，进度与检查点保留 |
| RT09 | hash_pipeline：crash_mid_hash | 模拟进程中断，已完成文件不重读，未完成文件从头重读，累计字节真实 |
| RT10 | checkpoints/hash_pipeline：source_changed | 哈希/FFprobe/抽帧中变更失败；旧元数据/分析引用回滚保留 |
| RT11～12 | force_scan_reprobes、real_ffprobe_force_and_resume_call_counts | 不强制跳过，强制新任务实际 FFprobe，恢复不重复已完成项，ID 稳定 |
| RT13 | FFmpeg rerun + concurrent_annotations | 真实强制抽帧重跑及并发人工状态保持 |
| RT14 | force_scan_reprobes + force 检查点恢复 | 区分新强制作业与相同 Job 断点恢复 |
| RT15 | data_integrity/ffmpeg：重复索引、失败重试 | 同一 Asset 行，不重复建档 |
| RT16 | generated_ten_minute_chinese_path_with_audio | 601 秒中文/空格路径、带 AAC 的生成视频，检测/尾帧/代理/API 通过；真实 Windows 原片仍未覆盖 |

补充不变量：在 SQLite BEGIN IMMEDIATE 写事务内验证/插入计划和复核引用，避免
“读取旧 ID → 分析删除 → 保存悬空引用”的竞争；直连模型的结果持久化时重新核对当前镜头；已有 pending 复核请求若引用失效镜头，不生成新代理，保留原记录/已有缓存并告警。
后台算法只写 detected_*、reason、version、quality_metrics、frame_refs；人工 trim 仅以
提交时最新值夹取，无法相交时保留原选择与审计、标记 needs_review。

校验模式：quick / full，默认全量只读一遍；strict_full_hash 明确增加处理后完整复核。
按 1 MiB 块协作取消，进度至多约 8 MiB/250 ms 一次及每阶段起止持久化。
恢复未完成哈希无法恢复 hashlib 内存状态，会重读该文件；`bytes_read` 不回退且含重复读取。
源错误的 `error_code` 为 source_changed/source_unavailable，其他 I/O 和编码故障显示明确
错误；修复权限、磁盘空间、源文件后新建任务或恢复未完成任务，旧分析版本无需删除。

Windows 实片待执行样本、记录表与最终结论见 [RELEASE_V2_0_2.md](RELEASE_V2_0_2.md)。
不把生成视频、低分辨率十分钟视频或 CI 绿灯当成真实 200GB 验收通过。

## v2.0.3 回归矩阵（此节 RT 编号属于 v2.0.3）

固定基线为 `ebcd25da6d5eafcd60339e776169a6a523a5f0d5`。新自动化覆盖 RT-01～17、20；RT-18/19 保留 Windows 真实旅行素材人工门槛，自动生成的视频不能代替实机验收。

| 用例 | 自动化位置 / 关键断言 |
|---|---|
| RT-01/02 | `test_review_validation.py::test_package_excludes_favorite_stale_in_all_filters`：默认与收藏 ZIP 排除 stale ID，并核对 Manifest |
| RT-03 | 同上及 `test_package_missing_frame_statistics_do_not_enumerate_excluded_asset`、`test_package_frames_are_limited_to_current_trim`：JSON/帧/联系表与 Asset 选择一致 |
| RT-04 | `test_package_all_invalid_fails_actionably`：全 stale、缺帧、源缺失都报“无当前有效镜头” |
| RT-05 | `test_forged_cross_project_and_mismatched_asset`：伪造/跨项目/错误 Asset，定位错误码 |
| RT-06 | `test_batch_strict_shared_validation`、`test_nonfinite_json_and_nonobject_rows_do_not_abort_valid_batch`：NaN/Infinity/负值/逆序不入库，正常行仍可保存 |
| RT-07/08 | `test_review_proxies.py::test_before_encoding_rechecks_latest_state`：收紧裁剪后零 FFmpeg 调用，保留旧代理 |
| RT-09 | `test_during_encoding_rechecks_commit_without_writer_lock`：修改裁剪/状态期间写入可以完成，最终拒绝旧结果 |
| RT-10 | `test_ffmpeg_integration.py::test_real_short_review_proxy_is_exact_readonly_and_project_scoped`：实际 FFprobe 约 0.6 秒、低于 0.8 秒、实际解码成功 |
| RT-11/12 | `test_cancel_during_encode_does_not_publish` / 编码期间 source 替换参数：无新正式文件，DB 旧引用保留 |
| RT-13 | `test_repeat_import_normalizes_and_reports_duplicate`，模型重复请求测试：三位毫秒规范化去重与原请求 ID 回传 |
| RT-14 | `test_stale_request_plan_is_preserved_and_roughcut_rejects_it` 与既有 stale plan 测试：计划保留，旧镜头不能生成新粗剪 |
| RT-15 | `test_human_replacement_preserves_history_annotations_plan`、`test_explicit_replacement_during_encoding_keeps_new_request_pending`：显式新 ID，历史链完整 |
| RT-16 | `test_defer_dismiss_and_failed_replacement_are_non_destructive`：暂缓/放弃不清除人工标记，旧 stale 不入包 |
| RT-17 | 实际 0.6 秒媒体测试的 default/custom 参数：HTTP Range、实际解码、跨项目访问拒绝、原片 SHA 不变 |
| RT-20 | `test_publish_faults_preserve_old_file_and_database`：磁盘不足、FFmpeg、不可读文件、DB 提交失败均保留旧产物 |
| 补充 | `test_package_retry_does_not_reuse_uncommitted_directory_or_stale_zip_members`、`test_replayed_job_keeps_published_byte_budget_and_duration_counters`：恢复隔离和发布预算 |
| 迁移 | `test_database_backup.py`：WAL 完整备份、新历史字段幂等迁移、旧 full 签名安全恢复 |
| 前端 | `ReviewRequestCard.test.tsx` 5 项，Review/Project 原测试增强：历史信息、计划阻断、手动选择、并发拒绝、暂缓/放弃、轮询不覆盖表单、默认 quick 与成本说明 |

后端完整本地运行使用已安装的 FFmpeg/FFprobe，并设置 `TSB_REQUIRE_FFMPEG=1`，不允许媒体 skip。CI 强制媒体清单新增 `test_review_validation.py`、`test_review_proxies.py`、`test_review_package_privacy.py`、`test_database_backup.py`；普通后端 Ubuntu/Windows × Python 3.11/3.13 继续运行，前端执行 typecheck/lint/Vitest/build。具体固定 SHA、数量和日志见发布记录。

手工验收必须按 `validation/v2.0.3/WINDOWS_REAL_FOOTAGE.md` 执行：8～10 段真实素材，长短混合、横竖屏、有无音轨，逐个核对时间映射、可播放与失败重试。**未执行不能写成通过。** 不要把完整 SHA 当作一次性费用：完整扫描 + 完整检测是两个任务，200GB 可能额外读约 400GB；严格复核及文件重读另计，不等于 FFmpeg 解码总 I/O。

## v2.0.7（TSB-271～277）确定性取消与删除安全

以下 28 条均可定位到自动化用例。短名的文件在 `backend/tests/`；Windows 特定场景
仅按平台事实跳过，强制媒体作业缺工具仍失败。六个新套件共 **57 个参数化用例**。

| 场景 | 任务 | 证据用例（文件::函数，参数见源码） |
|---|---|---|
| RT271-01 | TSB-271 | test_terminal_races.py::test_cancel_wins_between_outcome_check_and_terminal_cas[succeeded] |
| RT271-02 | TSB-271 | test_terminal_races.py::test_committed_terminal_is_not_overwritten_by_cancel[succeeded] |
| RT271-03 | TSB-271 | 上述两项 [partial_failed] |
| RT271-04 | TSB-271 | 上述两项 [failed]，保留真实失败；2,000 次受控 CAS 用例 |
| RT271-05 | TSB-271 | test_cancel_wins_between_outcome_check_and_terminal_cas：409 不新增行，解锁后 202；test_task_finally_finishes_under_project_lock_before_terminal |
| RT271-06 | TSB-271 | test_cancel_lifecycle.py::test_concurrent_cancels_produce_one_clean_terminal_state / test_pending_job_cancelled_before_it_runs_converges / test_recovery_does_not_rerun_a_cancelling_job；test_cancel_recovery.py::test_repeat_request_stays_blocked_until_cleanup_finishes |
| RT272-01 | TSB-272 | test_staging_ownership.py::test_exact_owned_shapes_delete_only_their_files（三种形状） |
| RT272-02 | TSB-272 | test_staging_ownership.py::test_non_leaf_shapes_are_rejected_without_changing_bytes（9 个反例） |
| RT272-03 | TSB-272 | 上项 frames/staging-evil、packages/staging-evil；raw_path_segments 检查 . / .. / 空段 |
| RT272-04 | TSB-272 | test_staging_ownership.py::test_internal_stage_alias_cannot_delete_another_stage |
| RT272-05 | TSB-272 | test_staging_ownership.py::test_middle_parent_link_is_refused_without_following（内部/外部，Windows 真 junction） |
| RT272-06 | TSB-272 | test_staging_ownership.py::test_different_jobs_cannot_adopt_or_delete_each_others_stage / test_cross_project_owner_and_asset_are_rejected |
| RT272-07 | TSB-272 | test_staging_ownership.py::test_link_swap_after_preflight_is_blocked_by_identity_recheck |
| RT272-08 | TSB-272 | test_staging_ownership.py::test_owned_chinese_stage_beyond_max_path_is_really_removed[270/320]；旧 test_cancel_cleanup 深路径回归 |
| RT272-09 | TSB-272 | test_staging_ownership.py::test_windows_open_file_reports_real_residual_bytes（真实 Win32 占用句柄）；test_unreadable_scan_is_failure_not_empty_success |
| RT273-01 | TSB-273 | test_analysis_cancel_commit.py::test_cancel_lands_after_last_poll_before_write_transaction |
| RT273-02 | TSB-273 | test_analysis_cancel_commit.py::test_analysis_writer_commits_before_queued_cancel_is_accepted（Event + SQLite writer 排队） |
| RT273-03 | TSB-273 | test_analysis_cancel_commit.py::test_transaction_cancellation_preserves_all_manual_history[cancelling/cancelled] |
| RT273-04 | TSB-273 | test_analysis_cancel_commit.py::test_cancel_after_new_jpeg_publish_reclaims_only_uncommitted_files；test_publish_cancel.py 两项粗剪扩展 |
| RT273-05 | TSB-273 | test_analysis_cancel_commit.py::test_cancel_and_source_change_keep_error_classification_visible；test_job_cancel_kills_process.py 媒体取消分类回归 |
| RT274-01 | TSB-274 | CleanupNotice.test.tsx 三种清理表达；Project.test.tsx 真实页面接线/停止任务重试/取消中不重试 |
| RT274-02 | TSB-274 | test_cleanup_reporting.py::test_cleanup_failure_survives_progress_and_other_stage_success |
| RT274-03 | TSB-274 | test_cleanup_reporting.py::test_terminal_cleanup_retry_is_authenticated_scoped_and_idempotent（认证/跨项目/仍运行拒绝） |
| RT274-04 | TSB-274 | 上项占用解除后成功、零残留、重复幂等；test_retry_cannot_adopt_old_or_invented_cleanup_paths |
| RT275-01 | TSB-275 | test_orphan_frame_safety.py::test_deep_chinese_old_orphan_removed_but_referenced_young_and_stage_kept[270/320] |
| RT275-02 | TSB-275 | 上项 Frame + Segment.frame_refs 引用保留 |
| RT275-03 | TSB-275 | 上项近期 JPEG/旧 stage 不删除；test_active_detection_defers_old_orphan_deletion |
| RT275-04 | TSB-275 | test_orphan_frame_safety.py::test_frame_root_and_nested_directory_links_are_never_followed[root/nested] / test_orphan_listing_error_is_observable_and_retryable |

额外：test_terminal_races.py 的 DB I/O、4 次重试耗尽用例断言结构化错误和未写
finished_at。两次反向接线验证记录于 `docs/validation/v2.0.7/reverse-*-wiring.txt`。
全量及最终 CI 发现的旧测试调度缺口改为显式调度/锁同步，不删除、skip 或放宽断言。
TC-29 进一步用 Event 控制清理阶段，等待 worker Future 返回后才断言 202；两个占用
阶段均断言 409 无新增行/finished_at 未写，不以终态可见冒充项目锁已释放。

手工门禁 **TSB-277 未执行**：Windows 真机 8～10 段旅行视频，端到端与取消时间线、
原片哈希/大小/mtime 一致、历史人工字段/复核/计划保持。另行执行 60+ 段/200GB
性能验证。清单见 `docs/validation/v2.0.7/WINDOWS_REAL_FOOTAGE.md`。

RT274-02 升级恢复补充：test_cleanup_reporting.py::test_legacy_single_cleanup_failure_survives_resumed_stage_success，旧版单条失败统计不被新 stage 成功覆盖；CleanupNotice.test.tsx 验证未知大小不会冒称 0 字节。

---

## v2.1 (M5) 视觉理解与动态复核用例映射

结果、范围声明与未完成项见 [RELEASE_V2_1_M5.md](RELEASE_V2_1_M5.md)。
用例 → 任务号 → 守护的行为：

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_vision_provider.py::test_status_classification[*] | TSB-302 | 401/403/404/408/429/5xx/400 各自映射到稳定错误码 |
| test_vision_provider.py::test_auth_failure_is_not_retried | TSB-302 | 错 Key 立即失败，不空耗预算 |
| test_vision_provider.py::test_bad_model_id_reported_as_400_is_classified_as_not_found | TSB-302 | 网关用 400 + "does not exist" 表示错模型时仍归类为 NOT_FOUND |
| test_vision_provider.py::test_retry_is_bounded / test_retry_after_is_honoured | TSB-302 | 重试有界且真正等待 Retry-After（断言时延） |
| test_vision_provider.py::test_malformed_success_bodies_are_rejected[*] | TSB-302/C6 | 200 但无可用内容 = 失败，不是空成功 |
| test_vision_provider.py::test_reasoning_only_answer_names_the_real_cause | TSB-302 | 推理型模型 content=null 时给出可定位原因（真实端点实测） |
| test_vision_provider.py::test_cancel_before_send_makes_no_request / test_cancel_during_backoff_stops_retrying | TSB-302 | 取消后不再发新请求，且不排队第二次 |
| test_vision_provider.py::test_declared_no_image_support_is_refused_before_any_request | TSB-302 | 能力错配在花钱之前被拒绝 |
| test_vision_provider.py::test_error_message_redacts_the_api_key | TSB-302/C5 | 供应商回显密钥也不得泄露到异常 |
| test_vision_provider.py::test_cleartext_non_loopback_is_refused_by_default | TSB-302 | 非回环明文端点默认拒绝，需显式 opt-in |
| test_vision_schema.py::test_fabricated_segment_id_is_rejected | TSB-303/C3 | 编造 ID 不解析为行，不静默重映射 |
| test_vision_schema.py::test_model_supplied_asset_id_is_ignored | TSB-303/C3 | 模型不能把镜头改指到别的素材 |
| test_vision_schema.py::test_unknown_frame_ids_are_dropped_not_trusted | TSB-303 | 编造 frame_id 被丢弃而非采信 |
| test_vision_schema.py::test_identity_and_place_claims_are_dropped[*] | TSB-303 | 像素无法证明的人名/地点断言不落库 |
| test_vision_schema.py::test_ordinary_visible_descriptions_survive | TSB-303 | 过滤器不过度：真实可见描述保留 |
| test_vision_schema.py::test_explicit_unknown_state_is_preserved | TSB-303 | "看不到" 是合法答案，不被归一掉 |
| test_vision_schema.py::test_single_frame_shot_is_forced_into_motion_review | TSB-303/307 | 单帧不能证明运动，强制标记并记录原因 |
| test_vision_semantics.py::test_job_reports_coverage_not_a_total_claim | TSB-304 | 分析子集必须报告为子集（not_covered 可见） |
| test_vision_semantics.py::test_rerun_skips_unchanged_shots | TSB-304 | 相同输入不重复调用模型 |
| test_vision_semantics.py::test_force_reruns_in_place_without_duplicating | TSB-304/C6 | 强制重跑真的重跑且不留重复行（曾静默失败） |
| test_vision_semantics.py::test_history_is_kept_when_the_input_signature_changes | TSB-304 | 输入签名变化时旧结论保留可审计 |
| test_vision_semantics.py::test_retrim_makes_the_result_stale_and_reanalyzed | TSB-304/C2 | 人工裁剪使缓存结论失效并重分析 |
| test_vision_semantics.py::test_provider_failure_yields_partial_failed | TSB-304/C6 | 单项失败不得呈现为成功 |
| test_vision_semantics.py::test_invalid_model_output_is_a_visible_failure | TSB-304/A13 | 畸形 JSON 逐项可见，不中断整批 |
| test_vision_semantics.py::test_job_refuses_without_upload_acknowledgement | TSB-304/A14 | 未同意时一张图都不发 |
| test_vision_semantics.py::test_uploaded_bytes_carry_no_local_path | TSB-304/C5 | 提示词含标签不含本机绝对路径 |
| test_vision_semantics.py::test_job_never_modifies_human_fields | TSB-304/C4 | AI 不写评分/用途/裁剪 |
| test_vision_candidates.py::test_rule_reason_never_asserts_a_shake | TSB-306 | 帧差只作"需核验"提示，不声称抖动/甩镜 |
| test_vision_candidates.py::test_single_frame_shot_is_flagged_not_dropped | TSB-306/A10 | 单帧标记运动不可判定而非排除 |
| test_vision_candidates.py::test_static_shot_is_not_a_candidate | TSB-306 | 无变化不是弱信号，静止镜头不入队 |
| test_vision_candidates.py::test_non_current_semantic_rows_are_ignored | TSB-306 | 已失效的语义结论不驱动新复核 |
| test_vision_candidates.py::test_works_without_a_configured_model | TSB-306/A01 | 规则候选不需要模型 |
| test_vision_motion.py::test_sample_times_are_inside_the_interval | TSB-307 | 采样点严格落在有效区间内，不取到相邻镜头 |
| test_vision_motion.py::test_sampled_timestamps_lie_inside_the_valid_interval | TSB-307/C2 | 真实抽帧的时间戳同样受区间约束 |
| test_vision_motion.py::test_invented_timestamps_are_dropped | TSB-307 | 模型编造的证据时间戳不被采信 |
| test_vision_motion.py::test_unknown_motion_always_requests_human_review | TSB-307/A11 | unknown 不留成看似确定的空白 |
| test_vision_motion.py::test_prompt_states_it_is_not_video / test_prompt_forbids_audio_claims | TSB-307 | 提示词声明非视频、不得评论音频 |
| test_vision_motion.py::test_source_longer_than_reported_duration_cannot_seek_past_the_end | TSB-307 | 过期时长不会让采样越过文件末尾 |
| test_vision_motion.py::test_short_interval_that_cannot_yield_two_frames_is_a_visible_failure | TSB-307/C6 | 抽不到两帧是可见失败，不算成功 |
| test_vision_motion.py::test_frames_are_removed_after_the_job | TSB-307/C7 | 只清理本次任务自己的暂存帧 |
| test_vision_motion.py::test_human_verdict_is_stored_and_survives_a_rerun | TSB-307/C4 | 模型可重跑，人工判定不被抹掉 |
| test_vision_motion.py::test_audio_is_never_claimed_to_be_analyzed | TSB-307 | 音频未分析必须写明 |
| VisionPanel.test.tsx（11 项，含 motion 4 项） | TSB-305/307 | 无模型提示、mode=off 拒传、同意门禁、上传预估、来源标注、部分失败、多帧非视频标注、人工判定替换按钮 |

手工门禁 **TSB-308 的真机部分**：本轮在 Windows 上用 6 段 DJI 4K HEVC 跑通
M5 全链路（见 `docs/validation/v2.1-m5/`），但**未覆盖竖屏/无音轨/HDR**，
段数也少于 8～10 段要求，因此真实素材发布门禁仍然未完成。
原生视频理解、音频语义、60+ 段/200GB 性能均未测。

---

## v2.1 (M5.1) TSB-312 / TSB-313 用例映射

结果、范围声明与未完成项见 [RELEASE_V2_1_M5_1.md](RELEASE_V2_1_M5_1.md)。
用例 → 任务号 → 守护的行为：

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_vision_authorization.py::test_mode_off_denies_even_with_consent | TSB-312/C5 | 全局开关默认 off，即使点了同意也不上传 |
| test_vision_authorization.py::test_mode_on_without_consent_denies | TSB-312/C5 | 本次调用未同意即拒绝，不是进程级"已授权" |
| test_vision_authorization.py::test_missing_project_scope_denies | TSB-312 | 无项目范围无法确认上传对象，拒绝 |
| test_vision_authorization.py::test_contexts_are_per_call_not_process_global | TSB-312 | 授权是逐次令牌，不跨请求泄漏 |
| test_vision_authorization.py::test_legacy_story_analyze_without_body_is_refused | TSB-312/C5 | **原缺陷**：旧 `/story/analyze` 仅凭"配了 Key"就上传，现无请求体即 403 |
| test_vision_authorization.py::test_legacy_story_analyze_denied_when_mode_off | TSB-312/C5 | 旧入口同样受 mode=off 约束 |
| test_vision_authorization.py::test_legacy_route_shares_the_boundary_with_the_new_one | TSB-312 | 两个入口共用同一判定，不各自推导权限 |
| test_vision_authorization.py::test_new_job_makes_zero_calls_when_* | TSB-312 | 被拒时**零**外部调用（不是"调用后丢弃"） |
| test_vision_compat.py::test_chat_leaves_no_frame_copy_in_the_temp_directory | TSB-313/C5 | **原缺陷**：data URI 落临时文件且从不删除；现计数断言临时目录不新增 |
| test_vision_compat.py::test_the_image_survives_as_in_memory_bytes | TSB-313/C6 | 不落盘不等于丢图：字节仍以 `ImagePart.data` 送达 provider |
| test_vision_compat.py::test_an_error_path_also_leaves_nothing_behind | TSB-313/C5 | 失败路径同样不留帧副本（旧实现失败也泄漏） |
| test_vision_compat.py::test_a_malformed_data_uri_is_reported_not_silently_dropped | TSB-313/C6 | 坏 data URI 报 `MODEL_BAD_REQUEST`，不静默丢图 |
| test_vision_compat.py::test_an_oversized_data_uri_is_refused_before_decoding | TSB-313 | 上限在解码前按字符串校验，避免内存放大 |
| test_vision_compat.py::test_a_plain_path_is_still_accepted_as_a_path | TSB-313 | 只有 inline data URI 改了传输方式，路径透传不变 |
| test_vision_compat.py::test_image_part_refuses_a_part_with_no_payload | TSB-313 | 无载荷的 ImagePart 构造即失败 |
| test_vision_compat.py::test_image_part_refuses_both_carriers_at_once | TSB-313 | `path` 与 `data` 互斥，不产生歧义来源 |
| test_vision_compat.py::test_image_part_keeps_the_old_positional_constructor_working | TSB-313 | `ImagePart(path, label)` 旧写法不被破坏（M5 渲染器在用） |
| test_vision_compat.py::test_a_forged_mime_is_refused_by_content | TSB-313 | 声称 PNG 实为 JPEG 的内容不符被拒 |
| test_vision_compat.py::test_a_non_image_payload_is_refused | TSB-313 | 非图片载荷按 magic bytes 拒绝 |
| test_vision_compat.py::test_an_oversized_inline_payload_is_refused | TSB-313 | 单图超过内联上限被拒 |
| test_vision_compat.py::test_the_compat_entry_point_cannot_be_used_without_a_context | TSB-312/C5 | 直接调 `chat()` 不能绕过业务入口 |
| test_vision_compat.py::test_the_flattened_request_carries_the_authorization | TSB-312 | 授权令牌确实附在出站请求上 |
| test_vision_compat.py::test_direct_analyze_passes_the_token_through_to_chat | TSB-312 | **接线**：首调与 JSON 重试都转发令牌（TSB-253 教训） |
| test_vision_compat.py::test_the_legacy_route_builds_the_context_once_and_forwards_it | TSB-312 | 路由先授权再调用，且不在路由内自查配置 |
| test_vision_compat.py::test_the_route_reports_a_stable_code_and_a_migration_hint | TSB-312/C6 | 拒绝给稳定错误码 + 迁移提示，不是含糊失败 |
| test_vision_compat.py::test_no_production_call_site_reads_the_removed_temp_helper | TSB-313 | 旧 `_materialize_data_uri` 已删除而非留用 |

**反向验证**：把实现回退到修复前修订（`306b1fd`）后，
`test_vision_compat.py` 26 项中 **22 项失败**（另 4 项为 pre-fix 才需要的导入容错），
失败原因包括 `chat() got an unexpected keyword argument 'authorization'`、
`ImagePart` 无 `data`、`sniff_mime` 不存在等；恢复修复后全部转绿。
修复前的实测泄漏证据见 `docs/validation/v2.1-m5-1/tsb313-before.txt`
（每次 `_flatten` 产生 1 个 203 字节 `tsb-vision-*.jpg`，返回后仍存在）。


## v2.1.1 / M5-R2

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_motion_verdict_continuity::test_same_evidence_rerun_keeps_current_api_verdict (3) | T314-01/02/07 | confirm/reject/unclear 重跑后当前列表/详情一致，备注和历史保留 |
| ::test_changed_evidence_requires_reconfirmation_and_retains_history (5) | T314-03/04 | 区间、源、JPEG、结论、检测签名改变需重新确认，历史不丢 |
| ::test_stale_page_verdict_is_409_without_rebinding_to_new_result | T314-07 | 页面证据过期不能绑定到新结果 |
| ::test_concurrent_confirmations_append_history_last_commit_is_effective | T314-06 | 并发确认历史完整，最后提交唯一有效，历史分页一致 |
| ::test_old_database_is_backed_up_before_create_all_and_backfill_is_once | T314-05 | 新表之前 WAL 一致备份、旧人工字段回填一次、未知指纹不伪造 |
| ::test_backup_failure_blocks_schema_creation | T314-05 | 备份失败不修改旧 schema |
| VisionPanel human-state (3) / verdict version-note-conflict (1) | T314-07 | 三态/历史/备注可见，提交绑定用户看到的证据，错误可见 |

手动门禁：在 Mac 真实素材和真实 HTTPS 模型条件下复查人工判定原样重跑、改裁剪重跑、历史及升级前后数据；本轮尚未执行，不以以上自动化替代。

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_vision_cache_versions::test_adding_c_only_calls_c / test_only_trimmed_a_invalidates | T315-01/02 | 单镜头键，新增/精细裁剪仅重算受影响镜头 |
| ::test_prompt_round_trip_activates_requested_version | T315-03 | v1→v2→v1 确实调用并正确切换 current |
| ::test_actual_frame_change_cannot_hit_cache (2) / missing_source | T315-04 | JPEG 替换/缺失、源失效不得误命中 |
| ::test_force_retains_request_audit_and_actual_wire_fingerprint | T315-05 | force 调用与独立请求审计/实际帧指纹 |
| ::test_parallel_identical_publish_has_one_current_row | T315-06 | 双线程两个有意调用，写锁串行，唯一当前版本且两个审计 |
| ::test_legacy_unknown_signature_is_retained_but_not_treated_as_valid | T315-07 | 旧签名保留为历史，不假定有效 |
| ::test_coverage_counts_failures_and_cache_separately / VisionPanel coverage | TSB-315 | 计划、成功、缓存、失败与未覆盖分别计数/展示 |
| test_vision_commit_races::test_after_model_before_writer_never_publishes_stale (14) | T316-01～05/07/08 | 两条链路在 provider 返回后使用 Event 注入取消/精细裁剪/排除/删除/源替换/检测变更/错项目 Job，无过期写入，取消期间 409 |
| ::test_writer_first_commits_once_then_cancel_converges (2) | T316-06 | writer 先拿锁，取消确实等待，提交保留，终态 cancelled，无重复结果 |

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_vision_transport_security::test_loopback_looking_host_is_not_a_loopback / malformed_or_credential_bearing_endpoint (7) | T317-01 | 伪本机域名、凭据/查询/非法 URL 预发送拒绝 |
| ::test_tls_verification_failure_is_permanent_classified_and_private | T317-03 | TLS 错误不可重试、不泄露服务端信息 |
| ::test_untrusted_errors_cannot_echo_endpoint_image_path_or_key (3) / test_httpx_logging_does_not_disclose_configured_endpoint | T317-04/06 | 网络/超时/服务错误及 HTTPX 日志不含 URL、Key、base64、路径 |
| ::test_mode_off_refuses_provider_even_with_an_old_authorized_context | T317-05 | 关闭总开关后旧授权上下文也零出站；新旧 API 另由 authorization/compat 套件守护 |
| ::test_user_migration_checker_never_uploads_without_explicit_ack (2) / test_user_checker_does_not_accept_unsafe_production_configuration (2) | T317-02 | 用户工具默认零上传、生产配置拒绝开发明文和未确认隧道；不替代真实 HTTPS |
| test_vision_staging_ownership::test_workers_finally_report_real_cleanup (10) | T318-01/02/06 | 两条链路成功/模型错误/取消/占用/统计异常仍执行清理，残留字节及重试可见 |
| ::test_exact_visual_stage_owns_bytes_and_releases_registry (2) / bounded_registry / completed_receipt | T318-01/06 | 回收 owner，有界回执不删除后来重建的目录，失败可重试 |
| ::test_sibling_and_restarted_visual_stages_are_not_deleted (2) / test_foreign_job_wrong_kind_existing_and_restart_stages_are_never_adopted | T318-03/07 | 不删兄弟 stage、不接管已有目录或重启后的孤儿 |
| ::test_swapped_visual_stage_link_does_not_touch_outside / test_visual_shape_expansion_does_not_authorize_parents (5) | T318-04/05 | symlink/junction 外部字节不变；父目录/错误形状拒绝 |
| ::test_real_motion_in_deep_unicode_workspace_keeps_source_and_history | T318-08 | 真实 FFmpeg 中文深路径抽帧和拥有的 stage 回收，原合成源及历史产物不变；进入强制媒体清单 |
| VisionPanel insecure-development-warning / cleanup residual | T317/318 | 明文例外必须显式可见，清理失败不能显示无残留 |

手动补充 T01–T25 门禁：本轮实际 HTTPS 多图接入与 6 段素材/8 镜头的 Mac API 验收另见 `validation/v2.1.1/HTTPS_REAL_PROBE.json` 和 Mac 报告；8～10 段、多类型矩阵与完整 UI 人工验收仍未完成。CI 和上述合成媒体不能替代。

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_vision_route_wiring::test_real_route_persists_all_visual_options (4) | TSB-319/312 接线 | 真实 FastAPI 匹配专用路由；前端 flat 和旧 options 包装都保留同意、选段、limit、force/frame_count |
| ::test_visual_options_use_dedicated_validation (2) / test_ambiguous_consent_is_rejected_before_enqueue (2) | TSB-319/317 | 非法 limit 和矛盾的两种同意格式在入队前 422，不静默丢弃/覆盖 |

## v2.1.2 / M5 收尾

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_vision_cleanup_status::test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free (2) | C320-01/02/06 | 两条真实 worker/Runner/API：模型成功但清理失败为 partial_failed；原结果保留；安全重试 0 新调用、终态/finished_at 不变、审计保留且标解决 |
| ::test_worker_never_claims_success_before_runner_finalization (2) | C320-03/09 | worker 不提前写成功/部分失败，正常由 Runner 收敛成功 |
| ::test_cancel_during_failed_cleanup_wins_and_terminal_time_is_immutable (2) | C320-05/09 | Event 清理屏障、取消优先、项目 409、finished_at 不回写；进程树/及时取消仍由原媒体套件守护 |
| ::test_stats_failure_and_cleanup_failure_both_remain_visible (2) / test_cleanup_report_storage_failure_cannot_become_success (2) | C320-08 | 报表/清理同时故障均可见，报告失败不能伪装成功 |
| ::test_stale_business_failure_snapshot_cannot_erase_cleanup_audit (2) | C320-04/08 | 旧失败对象发布新业务故障时不抹掉 durable cleanup audit；模型部分失败由原 semantics/motion 套件守护 |
| ::test_unsafe_stage_is_visible_without_deleting_foreign_content (6) | C320-07 | 丢 owner/链接/兄弟任务：不删外部字节，API 清理异常可见，未知字节不当作 0 |
| VisionPanel legacy-success cleanup/retry / CleanupNotice (新增 2) / useJob cancelling (新增 1) | C320-10 / U323-08/09/10 | 不保留纯绿色成功、重试只调 cleanup API、补救可见、未知数诚实、取消持续轮询 |

T23 手动扩展：视觉业务成功但清理失败必须醒目警示；仅安全重试后保留终态、显示补救且模型调用数不增加。真实浏览器/真人工证据待 C 批，不以组件测试代替。

| 用例 | 任务 | 守护行为 |
|---|---|---|
| test_release_version (5) / App release-version UI (1) | C321-01～04 | VERSION 权威；API/配置/启动器/前端/README/发布包 2.1.2；漂移拒绝，算法和模型缓存键不因产品版本变化 |
| test_media_format_matrix::test_format_runs_real_scan_detect_proxy_and_export (10) | M322-01～07 | H.264/HEVC/MOV/竖屏/Display Matrix 旋转/无音轨/PQ/HLG/60fps/VFR，正式 API+Runner 全媒体路径、可读/时长/比例/音轨和原合成文件保全 |
| ::test_invalid_container_is_visible_without_changing_bytes (3) / test_missing_source_after_scan_is_a_visible_detect_failure | M322-08 | 损坏/空/零时长/缺失可见且稳定错误码；detect API failure 与 checkpoint code 一致 |
| ::test_mixed_project_retains_good_formats_and_isolates_corruption / test_matrix_is_explicitly_forced_in_the_media_job | M322-10 | 四种有效类型+坏文件隔离；必须登记强制媒体 CI，不允许仅常规 backend |
| 既有 test_deep_media_http/deep_path_cleanup/deep_scan_api/vision_staging_ownership | M322-09 | Windows 默认长路径策略 + 真媒体 + 任务所有权安全回收；本地 Mac 平台 skip 不能代替 Windows CI |

T01/T04 手动增加：应用/帮助/API/发布包版本一致；旧库启动不因仅产品版本变化全量重新检测。T15/T17/T19 增加混合格式、竖屏/旋转、无音轨静音策略、实际可播放产物与时长核验。HDR 色彩准确性和 L2 额外实片未验收，不能由上述 L1 自动化替代。


| 用例 | 任务 | 守护行为 |
|---|---|---|
| Project::refreshes project and assets when scan finishes (2) | TSB-323 | 真实 UI 扫描完成后刷新统计/素材列表、启用检测；覆盖 running→succeeded 及首次轮询已终态，反向断开失效 |
| VisionPanel::refreshes published evidence on final poll (2) | U323-03/05 | 语义/动态任务完成后刷新语义、候选、预估和多帧结果；快任务不依赖运行中定时器，反向断开失效 |
| v2.1.2 MAC_UI_E2E / UI_JOB_AUDIT / UI_CLEANUP_RETRY / UI_TRIM_CONFLICT / UI_EXPORT_PROBE | TSB-323/324 | 实际产品界面扫描/检测/HTTPS 模型/缓存/取消/清理故障与补救/旧页 409/代理/导出；API/SQLite/ffprobe 仅核验产物，不冒充界面操作 |

T01/T04/T15/T17/T19/T23/T25 本轮补充记录见 `validation/v2.1.2/MAC_UI_E2E.md` 与 `RELEASE_GATE.md`：本轮 UI 使用现有 6 段中的 2 段、17 镜头；8 个镜头不等于 8 个视频。确认/拒绝自动化点选不冒充真人审阅。真人播放/判定尚待用户完成，浏览器安全策略拒绝鉴权媒体导航；ffprobe/完整解码不能替代实际播放。新增独立多类型实片、HDR 人工色彩对比及 200GB 性能未完成。

## v2.1.3 / M5.3 批次 A（TSB-325 / TSB-326）

结果、范围声明与未完成项见 [RELEASE_V2_1_3.md](RELEASE_V2_1_3.md)。
批次 A～D 均已完成；真实视频联调仍未执行（端点拒绝视频）。

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_native_video_capability.py::test_default_state_is_unverified_not_permission | TSB-325 | 默认 `unverified` 不是"可以试试"的许可 |
| ::test_only_verified_native_video_is_sendable | TSB-325 | 四态中只有 `verified_native_video` 可发送 |
| ::test_acceptance_without_temporal_evidence_is_not_sendable | TSB-325/V04 | 接受但无时间证据 ≠ 已验证，不得花用户素材 |
| ::test_limits_default_to_unknown_not_to_a_plausible_number | TSB-325 | 未测量的上限保持 unknown，不填猜测值 |
| ::test_to_dict_names_the_unknown_fields_explicitly | TSB-325 | 消费方无需读文档即可区分 null 与 0 |
| ::test_a_measured_zero_is_not_reported_as_unknown | TSB-325 | `False` 是知识，只有 `None` 才是未知 |
| ::test_verified_requires_a_protocol_and_a_scope | TSB-325 | "调用成功了"不构成验证证据 |
| ::test_record_verification_stamps_time_and_keeps_unknowns_unknown | TSB-325 | 已验证不等于上限已测量 |
| ::test_a_capability_object_has_no_credential_fields | TSB-325/C5 | 能力对象不含 key/url/path 字段 |
| ::test_protocol_selection_is_by_registration_not_by_model_name | TSB-325 | 协议按注册选择，不嗅探模型名（任务书点名的反模式） |
| ::test_a_duplicate_protocol_name_is_refused / _without_mime_types_is_refused | TSB-325 | 注册表 fail closed |
| ::test_an_unregistered_protocol_raises_rather_than_guessing | TSB-325 | 未注册协议报错而不是猜 |
| ::test_the_builtin_protocol_is_registered_and_carries_mp4 | TSB-325 | 内联协议不触发服务端取 URL |
| test_native_video_probe.py::test_a_2xx_without_temporal_content_is_sampling_unknown_not_verified | TSB-325/V04 | 200 但无时间内容 ≠ 已验证（核心规则） |
| ::test_a_dead_endpoint_cannot_be_reported_as_unsupported | TSB-325 | **实测场景**：网关全 503 不得被伪造为"不支持视频" |
| ::test_a_control_500_also_poisons_the_video_verdict | TSB-325 | 连视频形状的 415 在端点全挂时也无意义 |
| ::test_a_healthy_control_lets_a_video_rejection_stand | TSB-325 | 对照正常时才采信视频被拒 |
| ::test_a_dry_run_report_is_unverified_even_with_a_good_observation | TSB-325 | dry-run 不能凭空产生验证 |
| ::test_a_synthetic_clip_is_not_labelled_as_operator_footage | TSB-325 | **回归**：曾把合成片误标为运行者素材 |
| ::test_the_plan_states_when_mode_on_means_a_real_send | TSB-325 | mode=on 时明确告知请求会离开本机 |
| ::test_the_plan_flags_cleartext_non_loopback | TSB-325/V06 | 非回环明文端点风险可见 |
| ::test_the_report_carries_no_credentials | TSB-325/C5 | 报告不含 Bearer/api_key/sk- |
| ::test_dry_run_exits_zero_and_sends_nothing | TSB-325 | 默认 dry-run 零外发 |
| ::test_execute_without_explicit_consent_is_refused | TSB-325/C5 | 一个 flag 不足以打开真实上传 |
| ::test_a_missing_clip_is_refused_rather_than_silently_substituted | TSB-325 | 指定素材缺失时报错而非静默换用合成片 |
| test_native_video_provider.py::test_missing_authorization_sends_nothing | TSB-326/C5 | 无授权上下文零出站 |
| ::test_an_unregistered_protocol_sends_nothing / _empty_protocol_sends_nothing | TSB-326/V05 | 协议未定不发送，不自动猜协议重试 |
| ::test_a_non_video_payload_sends_nothing | TSB-326 | 声明 video/mp4 的 HTML 不得上线 |
| ::test_an_oversize_payload_sends_nothing | TSB-326/V07 | 超限在上线前拒绝 |
| ::test_a_fetching_protocol_is_refused_as_an_ssrf_surface | TSB-326 | 需服务端取 URL 的协议被拒（无 SSRF 面） |
| ::test_video_part_requires_a_server_issued_clip_id | TSB-326/C3 | clip 必须由服务端签发 |
| ::test_video_part_refuses_an_audio_bearing_clip | TSB-326/V07 | 派生片默认静音，带音轨需显式决定 |
| ::test_video_part_maps_source_coordinates | TSB-326 | 源坐标映射（为 TSB-328 绝对秒做准备） |
| ::test_capability_gate_refuses_none_and_unverified / _sampling_unknown / _allows_verified | TSB-326/V03 | 能力门禁：不可用时不启视频，图片模式不受影响 |
| ::test_a_successful_call_sends_a_video_url_data_uri | TSB-326 | 实际发出 `video_url` + data URI |
| ::test_the_uploaded_body_carries_no_local_path | TSB-326/C5 | 出站体不含本机路径 |
| ::test_an_empty_content_response_is_a_failure_not_a_success | TSB-326/C6 | 200 空 content = 失败，不是空成功 |
| ::test_error_statuses_are_classified[401/404/413] | TSB-326 | 稳定错误码分类 |
| ::test_the_api_key_is_never_in_the_error_message | TSB-326/C5 | 供应商回显密钥也不泄露 |
| ::test_a_cancel_before_send_makes_no_request | TSB-326/C7 | 取消后零出站 |
| ::test_sampling_disclosure_is_only_reported_when_the_server_says_so | TSB-325/326 | 抽帧机制未披露不得反推为已知 |
| ::test_retry_after_is_honoured_without_sleeping_too_long | TSB-326/V22 | 有界重试并尊重 Retry-After |

**反向验证**：`tools/reverse_verify_native_video.py` 逐个削弱守卫并确认测试转红——
协议注册闸门 → `test_a_fetching_protocol_is_refused_as_an_ssrf_surface` 失败；
载荷校验 → `test_a_non_video_payload_sends_nothing` 失败；
授权 chokepoint → `test_missing_authorization_sends_nothing` 失败；
`capability_gate` → `test_capability_gate_refuses_sampling_unknown` 失败。
`VERDICT: PASS`，脚本在每次变异后恢复原文件。

**手动门禁结论**：端点**当前不接受视频**，`video_native` 通道保持禁用。

实测（端点健康：纯文本 200、图片 200 且回答正确）——视频请求返回 HTTP 400，
vLLM 原始报错为
`At most 0 video(s) may be provided in one prompt. Set --limit-mm-per-prompt to increase this limit.`。
即 vLLM **认得** `video_url` 类型，但该部署的 per-prompt 视频上限为 0：
**服务端配置问题**，不是协议写错，也**不能**据此断言模型不支持视频。
要解除需服务端加 `--limit-mm-per-prompt video=1` 并重启部署，再重跑探测。

新增用例 `test_native_video_probe.py::test_a_zero_video_limit_names_the_serving_flag`
守护这条结论的表述：报错必须被翻译成可执行的**服务端**修复建议，而不是笼统的"HTTP 400"；
`::test_an_unrecognised_content_part_is_described_as_such` 区分"类型不存在"与"数量上限 0"；
`::test_an_unparsed_rejection_still_reports_the_status` 保证读不懂的报错不瞎猜原因。

证据见 `docs/validation/v2.1.3/CAPABILITY_REPORT.json` 与 `PROTOCOL_EXAMPLES.md`。
本次 `--execute` 确实外发了 48,829 字节**合成**短片（无用户素材），
不应描述为"仅本地测试"。
**视频被接受后能否给出有用时间信息仍完全未知**；拿到真实正例前不得进入
真实视频链路，也不得宣传视频理解能力。

---

## v2.1.3 / M5.3 批次 B + C + D（TSB-327 ～ TSB-332）

结果、范围声明与未完成项见 [RELEASE_V2_1_3.md](RELEASE_V2_1_3.md)。
用例 → 任务号 → 守护的行为：

### TSB-327 镜头短片派生与时间映射（38 项）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_video_clip.py::test_a_window_maps_to_absolute_source_seconds | TSB-327/V11 | 绝对秒 = 短片起点 + 相对秒，不重新设定原点 |
| ::test_mapping_uses_the_clip_offset_not_the_segment_start | TSB-327 | 预算裁剪后短片起点晚于片段起点时，以短片偏移为准 |
| ::test_a_window_crossing_the_edited_boundary_is_rejected_not_clamped | TSB-327/C2 | **越界拒绝而非夹取** —— 夹取会悄悄挪动人工编辑边界 |
| ::test_negative_times_are_refused_for_their_own_reason | TSB-327 | 隔离负值守卫：宽区间下只有它能拦住 |
| ::test_non_finite_times_are_rejected[nan/inf/-inf] | TSB-327/V12 | NaN/Inf 不得入库为候选窗口 |
| ::test_an_inverted_window_is_rejected | TSB-327/V12 | start ≥ end 不得成为"空事件" |
| ::test_float_noise_within_tolerance_is_accepted | TSB-327 | 容差不为难模型的三位小数 |
| ::test_a_long_shot_is_trimmed_to_the_budget_and_says_so | TSB-327 | 超预算取镜头**开头**并记录说明，不取中间 |
| ::test_verify_source_detects_a_size_change / _an_mtime_change | TSB-327/V14 | 源被替换即 `SOURCE_CHANGED`，不静默重分析 |
| ::test_the_source_file_is_never_modified | TSB-327/C1 | 派生前后原片字节与 mtime 不变（真实编码） |
| ::test_personal_container_metadata_does_not_survive_derivation | TSB-327/V07/C5 | creation_time/make/model/location 确实消失（先验证曾植入） |
| ::test_a_custom_handler_name_does_not_travel_into_the_clip | TSB-327/C5 | 相机写入的自定义 handler_name 不被带出 |
| ::test_the_derived_clip_has_only_a_video_stream | TSB-327/V07 | 无音频/字幕/数据流 |
| ::test_probe_clip_refuses_an_audio_bearing_artifact | TSB-327 | 无声假设在产物上强制，不只是请求 |
| ::test_probe_clip_reports_the_measured_duration_not_the_requested_one | TSB-327 | 以 ffprobe 实测为准（AGENTS.md 5.2） |
| ::test_an_hdr_transfer_characteristic_is_flagged_color_unverified | TSB-327/V10 | PQ/HLG 必须标 COLOR_UNVERIFIED，不冒称色彩准确 |
| ::test_an_sdr_clip_is_not_flagged_color_unverified | TSB-327 | 标记不是恒真，否则失去意义 |
| ::test_a_cancelled_derivation_stops_before_encoding | TSB-327/V17/C7 | 取消被真正执行且不发布产物 |

**反向验证**：`tools/reverse_verify_video_clip.py` 削弱 5 个守卫并确认转红。
**首轮发现 2 个守卫未被覆盖**（负值检查、无声强制），补测后 `VERDICT: PASS`。

### TSB-328 时间事件 Schema 与版本化结果（62 项）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_video_schema.py::test_a_response_about_another_clip_is_rejected | TSB-328/C3 | 描述别的 clip 即拒绝，不自动重映射 |
| ::test_a_response_about_another_segment_is_rejected | TSB-328/C3 | 同上，按 segment 维度 |
| ::test_a_response_with_nothing_usable_is_rejected / _non_list_events_field_is_rejected | TSB-328/C6 | 信封不可用即报错，不落空结果 |
| ::test_a_response_where_every_event_is_invalid_is_rejected_not_emptied | TSB-328/V12/C6 | 全废不得变成"空成功" |
| ::test_an_out_of_range_event_is_dropped_and_reported | TSB-328/V12 | 单项畸形丢弃并留因，整批继续 |
| ::test_audio_analysis_is_forced_false_even_if_the_model_claims_it | TSB-328/V21 | 无声片段下任何"听到音频"都是假，且该声明被记录 |
| ::test_confidence_is_clamped_into_range / _non_numeric_becomes_zero | TSB-328 | 置信度是模型自报值，不是准确率 |
| ::test_the_stored_dict_labels_confidence_as_uncalibrated | TSB-328 | 输出恒标 `confidence_is_calibrated=false` |
| ::test_an_identity_claim_in_an_event_is_dropped | TSB-328 | 像素无法证明身份/地点 |
| ::test_the_prompt_forbids_audio_claims_and_identity / ::test_the_prompt_permits_an_empty_answer | TSB-328 | 提示词声明非视频、禁音频、空答案合法 |
| test_video_observation.py::test_an_identical_rerun_replaces_in_place_without_duplicating | TSB-328/V15 | 同输入重跑原地替换，唯一 current |
| ::test_a_changed_input_appends_a_new_version_and_demotes_the_old | TSB-328/V15 | 变输入追加并降级旧行，历史保留 |
| ::test_a_changed_trim_makes_a_different_version | TSB-328/C2 | 重新裁剪使缓存结论失效 |
| ::test_a_stale_job_cannot_publish | TSB-328/V13 | 取消的 Job 不得提交迟到答案，且不留行 |
| ::test_a_trim_that_changed_during_the_model_call_blocks_publication | TSB-328/V14/C2 | 事务内复查，人工编辑优先 |
| ::test_a_failed_attempt_does_not_demote_the_last_good_reading | TSB-328/C6 | 失败重跑不摧毁上次好结果 |
| ::test_a_failure_does_not_make_already_done_true | TSB-328 | 失败不算"已完成"，否则重试空转却报成功 |
| ::test_already_done_is_false_for_a_different_clip_sha | TSB-328 | 不同派生字节是不同证据 |
| ::test_storing_a_video_reading_does_not_touch_semantic_rows / _motion_rows / _human_columns | TSB-328/V20/C4 | 三路隔离，人工判定与编辑边界不被改写 |
| ::test_coverage_reports_a_subset_as_a_subset | TSB-328 | 子集必须报成子集 |
| ::test_coverage_does_not_count_failures_as_covered | TSB-328/C6 | 失败不计入覆盖率 |

### TSB-329 video_analyze 任务（53 项：job 31 + api 22）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_video_job.py::test_unverified_video_is_refused_when_fallback_is_not_offered | TSB-329/V03 | 未验证即拒绝，不静默降级 |
| ::test_fallback_records_its_reason_and_the_effective_mode | TSB-329/V18 | 回退必须可归因 |
| ::test_a_fallback_never_claims_video | TSB-329/V18 | **核心**：回退后不得声称是视频 |
| ::test_too_many_segments_is_refused / _seconds / _bytes | TSB-329 | 三类预算越限均 `MODEL_BUDGET_EXCEEDED` |
| ::test_the_budget_defaults_are_local_guard_rails_not_endpoint_claims | TSB-329 | 本地护栏不得写成端点限制 |
| ::test_preflight_refuses_without_consent / ::test_preflight_refuses_unverified_capability_even_with_consent | TSB-329/V01/V02 | 派生任何媒体之前即拒绝 |
| ::test_a_job_without_consent_sends_nothing / ::test_a_job_without_verified_capability_sends_nothing | TSB-329/V01/V03 | **零出站**（最强形式的"没上传"） |
| ::test_excluded_and_invalid_shots_are_not_candidates | TSB-329 | 排除/失效镜头不入队也不计入预算 |
| ::test_a_later_settings_change_cannot_alter_a_running_plan | TSB-329 | 配置快照是权威，授权不可被事后放宽 |
| ::test_a_fallback_does_not_produce_video_rows | TSB-329/V18 | 回退不产生任何 `video_native` 行 |
| ::test_video_analyze_is_registered_in_all_three_places | TSB-329 | 可派发（VALID_JOB_TYPES + TASK_REGISTRY + EXCLUSIVE_JOB_TYPES 三者同步） |
| test_video_api.py::test_an_unverified_video_channel_is_refused_before_enqueueing | TSB-329/V03 | **请求即拒绝**，且不留可排队 Job 行 |
| ::test_a_running_job_is_reported_as_a_conflict_before_any_capability_verdict | TSB-329 | 互斥优先：答 422 会掩盖项目正忙 |
| ::test_a_fallback_request_is_accepted_when_video_is_unavailable | TSB-329/V18 | 显式回退可通过 |
| ::test_a_multiframe_request_is_never_blocked_by_video_capability | TSB-329/V03 | 图片/多帧通路不受影响 |
| ::test_the_response_always_carries_the_honesty_flags | TSB-331/328 | 消费方无需从缺失字段推断"未分析音频" |
| ::test_history_returns_superseded_versions / ::test_include_history_exposes_every_version | TSB-328/V15 | 历史可审计 |
| ::test_a_failed_attempt_is_not_counted_as_coverage | TSB-328/C6 | 失败不是覆盖 |
| ::test_video_endpoints_require_the_access_token / ::test_a_non_loopback_host_is_refused | TSB-329/C5 | 新端点同样受鉴权与 Host 守卫约束 |

### TSB-330 A/B 评价（31 项）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| test_video_ab.py::test_without_labels_every_arm_reports_null_metrics | TSB-330/V19 | **核心**：无人工标注不出任何准确率 |
| ::test_without_labels_adoption_is_not_decided | TSB-330/V19 | 无数据不判定采纳 |
| ::test_a_report_without_labels_is_marked_not_annotated | TSB-330 | 状态如实 |
| ::test_a_blocked_run_reports_blocked_by_endpoint | TSB-330 | arm C 缺失标 `blocked_by_endpoint` |
| ::test_arm_c_is_never_filled_from_arm_b | TSB-330 | **绝不**用多帧读数冒充原生视频证据 |
| ::test_mock_results_are_not_accepted_as_arm_c_evidence | TSB-330 | 数字齐全也不能让阻塞运行变"已评价" |
| ::test_an_arm_that_ran_but_found_nothing_scores_zero_not_null | TSB-330 | `0.0`=跑了没检出；`null`=没跑（两者不可混淆） |
| ::test_an_arm_with_no_reading_at_all_is_null_not_zero | TSB-330 | 镜像情形 |
| ::test_a_label_is_never_matched_twice | TSB-330 | 重复使用标注会抬高召回 |
| ::test_events_without_absolute_times_are_ignored_not_guessed | TSB-330 | 无绝对秒不猜 |
| ::test_the_report_states_how_many_distinct_videos_the_segments_came_from | TSB-330 | 20 镜头≠20 个独立样本 |
| ::test_the_report_refuses_to_compare_token_costs_across_shapes | TSB-330 | 不同计费形态不可比 |
| ::test_the_adoption_thresholds_are_pre_registered | TSB-330 | 阈值执行前固定，防事后挑选 |
| ::test_the_written_report_contains_no_local_paths / ::test_writing_refuses_a_report_that_leaked_a_path | TSB-330/C5 | 证据不含本机路径 |
| ::test_a_malformed_label_file_yields_no_labels_rather_than_zero_scores | TSB-330 | 坏标注文件不得变成"评了 0 分" |

### TSB-331 前端面板（17 项）

| 用例 | 任务 | 守护的行为 |
| --- | --- | --- |
| VideoUnderstandingPanel.test.tsx::all three input modes are labelled with what they actually are | TSB-331 | 三路并列且各自标注真实输入模式 |
| ::a fallback is shown with both requested and effective modes | TSB-331/V18 | 回退必须写明"不是视频分析" |
| ::a capability refusal is explained with state and remedy | TSB-331 | 拒绝给出能力状态与可执行处理方式 |
| ::a generic failure is not dressed up as a capability problem | TSB-331 | 普通失败不被伪装成能力问题 |
| ::an event shows both clip-relative and absolute times | TSB-331 | 两套坐标各自标注，混用会放错事件位置 |
| ::confidence is labelled as uncalibrated | TSB-331 | 出现"未校准"，不出现"准确率" |
| ::events without absolute times omit the source line rather than showing NaN | TSB-331 | 无绝对秒不显示 NaN |
| ::the honesty badges are rendered | TSB-331 | 音频未分析 / quality 可见 |
| ::an HDR source shows the colour-unverified badge | TSB-331/V10 | 色彩未验证可见 |
| ::dropped events are surfaced | TSB-331 | 被丢弃事件可见而非静默 |
| ::a zero-event reading says so rather than looking empty | TSB-331 | 零事件≠空成功 |
| ::coverage is reported as a subset, not a total | TSB-331 | "这不是已分析全库" |
| ::the run button is disabled until per-call consent is given | TSB-331/V02 | 逐次同意门禁 |
| ::a missing reading shows its reason instead of disappearing | TSB-331 | 缺失列显示原因 |

### TSB-332 证据边界

| 产物 | 守护的行为 |
| --- | --- |
| `tools/protection_report.py` → `PROTECTION_REPORT.json` | 原片（6 段 4.76 GB）与生产库的只读快照，作为事后比对基线；**明确声明**采样摘要≠全文件摘要 |
| `ci.yml` 强制媒体清单 | 9 个新套件登记；实测 457 项 **0 skip** |
| `RELEASE_V2_1_3.md` | 结论措辞、范围声明、未完成项 |

**手动门禁（未完成）**：真实视频联调、A/B 人工盲评、8～10 段多维度素材验收、
60+ 段/200GB 性能 —— 均未执行，不得由上述自动化替代。
A/B 报告 `AB_RESULTS.json` 的 `status` 为 `not_annotated`，**所有指标为 null**。



---

## M5.3-R1 整改（R1-A / R1-B）

### TSB-333 CI 跨平台失败：测试级开启模型开关

| 产物 | 守护的行为 |
| --- | --- |
| `test_native_video_provider.py`（模块 autouse fixture） | 15 项协议/嗅探/上限/状态分类/重试用例够得着授权边界之后的分支；**生产默认 off 不变** |
| `test_video_model_switch_scope.py::R02` | fixture 开启后**确实**发生 1 次真实 loopback 请求（不是 0 次） |
| `::R02b` | 该开启不依赖进程环境变量 |
| `::R01` | 开关关着时 0 外发且 `MODEL_UPLOAD_DENIED` |
| `::R01b` | `.env.example` 默认值仍为 `off` |
| `::R01c` | `ci.yml` 不得出现 `TRAVELSB_MODEL_MODE`（防"全局打开"式假绿） |

### TSB-334 视频 Provider 工厂与真实调用链

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_provider_wiring.py::R07` | 收到 `complete_video` 的类是 `NativeVideoVisionProvider` |
| `::R07b` | 恰好 1 个请求；含 `data:video/mp4;base64,`、不含 `image_url`；载荷 `ftyp` 完好 |
| `::R09` | 非回环明文端点 0 外发且不发布 observation |
| `::R10` | 事件 rel 0.5s → 源片绝对 1.5s；`is_current` 唯一；`audio_analyzed` 强制 False |
| 反向验证 R08 | 调用点改回 `get_provider()` → 5 项全红 |
| `errors.MODEL_PROVIDER_FAULT` | 适配器逃逸异常与 `MODEL_INVALID_OUTPUT` 区分 |

### TSB-335 端点能力记录闭环

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_capability_store.py::a1` | 全新安装无记录 → `unverified`、0 外发 |
| `::a2` | 实测 `unsupported` 仍拒绝 |
| `::a3` | 本机正例绑定当前端点后允许 |
| `::a4/a4b/a4c` | 换 URL / 模型 / 协议 / 部署 / 过期 → 重新拒绝 |
| `::a5/a5c/a5b` | 伪造记录、缺 scope、损坏 JSON 均不解锁 |
| `::a5d` | 仅 200 无时间信息 = `video_accepted_sampling_unknown`，仍不可发送 |
| `::a6/a6b/a6c` | 重启重读；记录不在仓库内；不含 Key/Authorization/绝对路径 |
| `::w1/w3/w4` | `current_capabilities()` / `plan_video_job()` 确实读了记录 |
| `::w2` | 存储故障降级为"拒绝"，不是"允许"、也不崩 |
| `::w5` | 排队后切换端点在执行时被拦下 |
| 反向验证 | 改回硬编码 `unverified` → W1、W3 转红 |
| `probe_video_capability.py --record-capability` | dry-run 拒绝写入（无证据） |

### TSB-336 暂存回收精确叶子与清理健康统一

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_provider_wiring.py::R16` | 成功运行后规范审计 `cleanup.ok=True`、`items` 非空、`removed_files>=1`（未被 `merge_job_stats` 覆盖） |
| `::R17` | 派生失败仍把叶子交给清理；不产生伪失败 |
| `::R19` | 原片 SHA-256 与源目录列表不变（C1/C7） |
| 反向验证 | 恢复 `parent.parent` 且取消派生前登记 → R16、R17 双红 |

### TSB-341 能力探测可移植性与证据强度

| 产物 | 守护的行为 |
| --- | --- |
| `test_probe_clip_portability.py::test_the_probe_clip_builds_without_drawtext` | 探测短片在**无 freetype** 的 FFmpeg 上也能生成（修复前崩溃） |
| `::test_the_probe_clip_carries_a_checkable_colour_sequence` | 逐帧采样验证三段颜色确实不同且顺序为红/绿/蓝 |
| `::test_the_probe_asks_for_the_observed_order` | prompt 必须点名颜色并要求顺序 |
| `::test_a_correct_order_is_recognised_and_a_wrong_one_is_not` | 顺序说反必须判否 |
| `::test_the_old_keyword_heuristic_could_not_fail_this_way` | 回归守卫：流利但顺序错误不得通过 |
| 反向验证 | 放回 `drawtext` → 前两项转红 |
| `test_video_clip.py` 测试源 | 改用核心滤镜 `hue`；本机 38/38（原 12 failed） |

---

## M5.3-R2 补充整改（TSB-342 / 343 / 344 / 337 / 338 / 339 / 340）

发布记录：[docs/RELEASE_M5_3_R2.md](RELEASE_M5_3_R2.md)
证据目录：[docs/validation/m5.3-r2/](validation/m5.3-r2/)

### TSB-342 探测证据不可伪造

| 产物 | 守护的行为 |
| --- | --- |
| `test_probe_temporal_evidence.py::V01/V01b/V01c` | 提示词不含任何颜色顺序、无填充示例；每轮顺序随机且互不相同 |
| `::V02/V05/V06/V06b` | 严格 JSON 解析；顺序必须与本轮隐藏真值一致（每轮独立判定） |
| `::V03/V03b` | 纯提示词对照答对即判否（含回抄 prompt） |
| `::V04` | 仅含 motion/moving/changing 的文本**不得**作为时间证据（修复前误判通过） |
| `::V07/V07b` | HTTP 200 但无有效顺序 → `video_accepted_sampling_unknown`；单帧对照答对亦不得升级 |
| `::V08/V08b` | 401/403/413/429/500/503 与超时永不 `verified` |
| `::A3/A3b/A3c/A3d/A3e` | 需 ≥3 轮全对、顺序互异、对照确实执行且答错 |
| `::W1–W4` | **接线**：驱动确实传入每轮顺序与 `probe_id` 并确实跑了两项对照 |
| 反向验证 | 写回示例 → V01b 红；恢复 motion 回退 → V04/V03/V06 三红 |

### TSB-343 CI 虚拟模型环境与 Windows CLI 编码

| 产物 | 守护的行为 |
| --- | --- |
| `test_ci_environment_isolation.py::V11/V12/V13` | Mock 契约完整（`model_mode`+URL+Key）；缺 URL/Key 时确为 `MODEL_NOT_CONFIGURED`；teardown 复原，不泄漏全局开关 |
| `::V14/V14b/V14c` | cp1252/cp936/ascii/latin-1 下 dry-run 均 exit 0，`--ascii` 输出纯 ASCII，JSON 报告仍写出 |
| `::V15/V15b/V16` | dry-run 零外发；无 URL/Key 也能出计划；`--execute` 单独不发送 |
| `::V16b/V16c` | 生产默认仍 `off`；`ci.yml` 不导出全局 `TRAVELSB_MODEL_MODE` |
| 反向验证 | 移走 `backend/.env` 复现 17 项失败（改前），改后 0 |

### TSB-344 暂存叶子替换与链接拒绝

| 产物 | 守护的行为 |
| --- | --- |
| `test_staging_leaf_substitution.py::V17/V17b` | 悬空链接必须失败关闭（不得静默跳过）；指向项目外的链接绝不跟随 |
| `::V18/V18b/V18c/V18d` | 同路径换成别的目录、目录链接、伪造 inode、伪造祖先链身份 → 一律拒绝且文件存活 |
| `::V19/V19b/V19c` | 父目录、更深子路径、他人 job_id 均无删除权 |
| `::V20/V20b` | 正常叶子仍能清理、二次清理幂等（防过度拒绝） |
| `::V21` | 被拒绝时项目外受害目录与邻居目录均不受影响 |
| 反向验证 | `_is_link` 恒 False → 6 红；删除身份比对 → V18d 红且错误删除了文件 |

### TSB-337 回退多帧不得空跑成功

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_fallback_execution.py::V22/V22b/V22c/V22d` | 未同意回退即拒绝；同意则明确命名模式与原因；可发送态不回退；`multiframe` 不被改道 |
| `::V23/V23b` | 回退必须真的分析、或给出稳定错误码——**不得** `phase=complete` + `total=0`（修复前正是此形态） |
| `::V23c` | 反向：未发送则不得声称已分析 |
| `::V24/V24b` | 必须指明工作落到哪个 job 类型；且不得伪造 video 行 |
| 反向验证 | 恢复空跑成功 → V23/V23b/V24 三红 |

### TSB-338 预算逐次生效与审计台账

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_budget_ledger.py::V25/V25b` | `max_calls` 真正生效（修复前是死配置）；重试计为一次尝试 |
| `::V26/V26b` | 每次尝试**前**校验字节，含首次；被拒尝试不计数 |
| `::V27/V27b` | 台账可审计、可序列化、不含路径/端点/凭据 |
| `::V28/V28b/V28c/V28d` | 计划级上限不回退；**实际派生**时长复核 |
| `::W5/W6/W7` | **接线**：`note_attempt` 出现在 `_analyze_one` 且早于 `complete_video`；计费含 Base64 膨胀 |
| 反向验证 | 删除发送点 charge → W5 红 |

### TSB-339 前端结构化错误与证据匿名化

| 产物 | 守护的行为 |
| --- | --- |
| `api-error-structure.test.ts::V29/V29c/V29f` | `ApiError.code` 保留稳定错误码；不可重试的错误如实标注；嵌套 detail 被展开而非字符串化 |
| `::V29b/V29e` | `retryable`/`retry_after`/`status` 保留；无法解析的 body 不退化为解析错误 |
| `::V29d` | 服务器未给 code 时**不臆造**（`undefined`） |
| `test_evidence_anonymisation.py::V30b/V30c/V30d` | `--redact` 清除 prose 中的端点主机、本机绝对路径、Bearer 令牌 |
| `::V30e/V30f` | 幂等、保留结构、不修改入参 |
| `::V30g` | 公开摘要不含顺序/端点/路径 |
| 反向验证 | 改回单字段 redactor → 3 红 |

### TSB-340 正式 API 端到端

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_e2e_formal_api.py::V31` | 全链路：API 受理 → Mock 收到含 video 的请求 → Observation 落库 → 清理无失败 → **原片 sha256 与 inode 未变（C1）** |
| `::V32` | 取消不得落库、不得记成完成 |
| `::V33/V33b` | 项目互斥在 API 层生效；6 并发最多 1 个被接受，拒绝均为 409/422 而非 500 |
| `::V34/V34b` | 重跑不删历史观测（降级 `is_current=False`）、不覆盖 `user_rating`/`content_tags`（C2/C4）；至多一行 current |
| `::V35/V35b` | 未验证端点 422 拒绝且请求**未到达模型** |
| `::V36` | 覆盖率接口返回真实计数 |
| 由本测试暴露的真实缺陷 | `CLIP_FAILED` 未定义（NameError）；预登记与 `derive_clip` 二次创建冲突（FileExistsError）；预登记只记路径未建目录 |

### R2 手动确认项（建议纳入 T01–T25）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| T-R2-01 | 未验证能力的端点上点击「原生视频分析」 | 按钮禁用；若强行请求返回 422 且带稳定错误码，且**无任何外发** |
| T-R2-02 | 提交一份可提交的证据（`--redact`） | 文件中搜不到端点主机、本机路径、令牌；协议/字节数等审计字段保留 |
| T-R2-03 | 对同一项目并发触发两次视频分析 | 至多一个受理，另一个 409 且提示正在运行的任务 |
| T-R2-04 | 重跑已有分析的项目 | 历史观测仍在（被降级而非删除），人工评分与备注不变 |

---

## M5.3-R3 稳定性与安全收尾（TSB-345 / 346 / 347 / 348 / 349 / 350）

发布记录：[docs/RELEASE_M5_3_R3.md](RELEASE_M5_3_R3.md)
证据目录：[docs/validation/m5.3-r3/](validation/m5.3-r3/)

### TSB-345 代际身份与防接管

| 产物 | 守护的行为 |
| --- | --- |
| `test_staging_leaf_substitution.py::V21` | **inode 复用伪造**：相同 (dev, ino) 但不同创建事件 → 拒绝且不删除（Ubuntu CI 真实失败场景） |
| `::V21b` | 重建目录无法复现代际 nonce；伪造标记文件被拒 |
| `::V21c` | 标记缺失/不可读 → 失败关闭，不退回路径或 inode 匹配 |
| `::V21d` | 标记本身是链接 → 不作为所有权证据，项目外目标不被删 |
| `::V21e` | 代际 nonce 不泄露到任何公开输出（结果/message/path/回执） |
| `::V21f` | **正常路径不退化**：合法清理成功、removed_files==1、重复清理幂等 |
| `::V22` | 路径上已有的未登记目录 → 不得接管、不得获得删除权 |
| `::V22b/V22c/V22d/V22e` | 本任务叶子被认可；他人 job 叶子不复用；代际被破坏不认可；已清理叶子不重获删除权 |
| 反向验证 | 身份强制相等 → 修复前删除文件、修复后拒绝 |

### TSB-346 悬空链接不得静默跳过

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_fallback_execution.py::V37` | 悬空链接占用路径 → 不得记为 "stage was never created"（`Path.exists()` 跟随链接的坑） |
| `::V37b` | 真正不存在的路径仍记为从未创建（不破坏审计） |
| `::V37c` | 正常目录必须交给清理器（防过度跳过） |
| `::V37d` | **接线**：清理点必须用 link-aware 判断，`Path.exists()` 形式必须已移除 |
| 反向验证 | 恢复 `Path.exists()` → V37d 红 |

### TSB-347 每次 HTTP 尝试前预留预算

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_budget_ledger.py::V38` | 三次真实重试计 3 次（修复前只记 1 次） |
| `::V38b` | 超出 `max_calls` 的尝试在**发出前**被拒；被拒不计费 |
| `::V38c` | 每次重试的字节逐次计入 |
| `::V38d` | **接线**：guard 必须早于 `httpx.post`（请求后计费等于没计费） |
| `::V38e` | **接线**：Job 调用点必须传入 `attempt_guard` |
| `::V38f` | 派生时长逐镜头累计（十个 5 秒镜头 = 50 秒，修复前是 5 秒） |
| `::V38g` | 累计超 `max_total_sec` → `MODEL_BUDGET_EXCEEDED` |
| `::V38h` | 台账可回答「还剩多少秒」 |
| 反向验证 | 恢复覆盖式派生秒数 → V38f/V38g 红；移除 guard 调用 → V38d 红 |

### TSB-348 回退必须真正调度

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_fallback_execution.py::V39` | 回退必须真的排出 1 个 `motion_review` 任务（修复前 0 个） |
| `::V39b` | 排出的任务沿用计划选中的 segment_ids/limit |
| `::V39c` | **其他活跃互斥任务仍必须冲突**，且不留半排队残留（互斥语义未被削弱） |
| `::V39d` | 豁免精确到单个 job id，不得按 job_type 放宽 |
| `::V39e` | 无法排队的回退必须报告原因，不静默失败 |
| `::V39f` | **接线**：worker 必须把自己的 job id 传进去，否则自锁冲突永远排不出来 |
| 反向验证 | 改回「记录即返回」→ V39f 红 |

### R3 手动确认项（建议纳入 T01–T25）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| T-R3-01 | 同一路径下把暂存叶子换成另一个目录后再触发清理 | 拒绝清理、报 `STAGING_NOT_OWNED`、被替换目录内容**完好** |
| T-R3-02 | 暂存路径存在悬空链接时运行视频任务 | 该链接被交给清理器并失败关闭，**不得**显示 "stage was never created" |
| T-R3-03 | 让上游反复返回可重试错误（触发重试） | 每次重试都计入台账；达到上限后**在发出前**拒绝，`budget_exceeded=true` |
| T-R3-04 | 多镜头任务观察台账 | `derived_seconds` 为**逐镜头累计**值，`remaining_seconds` 随之递减 |
| T-R3-05 | 在未验证能力的端点上以 `allow_fallback=true` 提交 | 明确拒或**真正排出** `motion_review`；不得只记 deferred 而无任务 |
| T-R3-06 | 回退排队时项目内有其他活跃任务 | 仍返回 409，且不留下半排队任务 |

---

## M5.3-R3.1 模型预算与回退闭环补丁（TSB-351 / 352 / 353 / 354 / 355）

发布记录：[docs/RELEASE_M5_3_R3_1.md](RELEASE_M5_3_R3_1.md)
证据目录：[docs/validation/m5.3-r3.1/](validation/m5.3-r3.1/)

### TSB-351 单次 HTTP 尝试只扣一次

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_budget_http_contract.py::B01` | 配额 1、模型 200 ⇒ **POST=1、calls=1**（修复前 POST=0：预扣耗尽额度） |
| `::B01b` | 计费回调被调用且**仅** 1 次 —— 一次 POST 不得扣两次 |
| `::B02` | 配额 1、503 ⇒ 只有 1 次 POST，重试被预算拒绝 |
| `::B03` | 配额 2、503→200 ⇒ **2 次** POST、calls=2（不得提前耗尽） |
| `::B04/B05` | 授权拒绝 / 非法视频部件 ⇒ **0 POST、0 计费** |
| 反向验证 | 恢复有副作用预扣 ⇒ **10 RED** |

### TSB-352 按真实序列化 Body 计费

| 产物 | 守护的行为 |
| --- | --- |
| `::B07/B11` | 预留字节 **== Mock 服务实际收到的 body 长度**（实测 1295 == 1295） |
| `::B08` | 精确 Body 为 B、配额 B-1 ⇒ **0 POST** 且报稳定预算错误 |
| `::B08b` | 配额恰为 B ⇒ 允许（**不得过度拒绝**） |
| `::B09` | 中文 prompt 按 UTF-8 精确计量，而非固定 `+512` 估算 |
| `::B09b` | 计费路径不得再出现独立估算函数（消灭两套算法） |

### TSB-353 任务级重试/超时确实生效且不串扰

| 产物 | 守护的行为 |
| --- | --- |
| `::B13` | 快照 `max_retries=0`、持续 503、Provider 自身值 99 ⇒ **只发 1 个 POST** |
| `::B14` | 快照 `max_retries=2`、Provider 自身值 0 ⇒ **发 3 个 POST** |
| `::B15` | A(3s)/B(11s) 的 `httpx.post(timeout=)` 分别为 3.0 / 11.0 |
| `::B15b` | **次序互换**结果仍为各自的值 —— 无共享可变状态污染 |
| `::B15c` | 请求 99 次重试 / 9999 秒 ⇒ 被服务端上限钳制（不得放大费用） |
| `::B15d` | 不得在调用中改写共享 Provider 实例的 `self.max_retries`/`timeout_sec` |
| `::B15e` | **接线**：调用点必须传 `CallOptions`，且取自 `plan.budget` |
| 反向验证 | Provider 忽略按调用选项 ⇒ B13/B15/B15b **3 RED** |

### TSB-354 回退父子追踪与终态

| 产物 | 守护的行为 |
| --- | --- |
| `test_fallback_job_tracking.py::B16/B16b` | 返回**真实后继 Job ID**，且指向**唯一那条**后继行 |
| `::B16c` | 排不出来 ⇒ `state=failed`、无 ID、有可读原因 |
| `::B16d` | 保持向后兼容：`scheduled, error = ...` 仍可解包 |
| `::B17` | **断言 worker 源码**确实把后继身份写入父 Job stats |
| `::B17b` | 写入的身份必须能被解析回真实存在的同一行 |
| `::B18` | 六种后继状态逐一钉住：只有 `succeeded` 且本次选中镜头的有效多帧结果可核验时 `analysed` 才为真（R3.2 加强） |
| `::B19` | 没有后继 ⇒ 永远不可能是"已分析" |
| `::B20` | 后继行消失 ⇒ 报 `missing`，**不得假设成功** |
| `::B21` | 成功的回退是预期降级，**不得**记入 failures（父 Job 不得假报 partial_failed） |
| `::B22` | 不算失败 ≠ 隐藏：`phase`/`fallback_state`/`deferred_reason` 仍须可见 |
| `::B23` | 走**真实 Runner 注册入口**执行后继，并断言进入终态 |
| `::B23b` | 读取父 Job 时解析后继**当前**状态，而非回放排队那一刻的 `scheduled` |
| `::B24–B24d` | API 详情与列表均带 fallback 块；无回退不伪造 |
| 反向验证 | 删 `fallback_job_id` ⇒ B17 RED；恢复无条件 `_record_failure` ⇒ B21 RED |

### TSB-354 前端回退追踪

| 产物 | 守护的行为 |
| --- | --- |
| `VideoUnderstandingPanel.test.tsx` | 已排队 ⇒ 显示"已排队，尚未开始"，**不得**出现"已入库" |
| 同上 | 运行中 ⇒ "正在分析"，仍**不得**显示已分析 |
| 同上 | 只有 `succeeded` 且后端确认 `analysed` ⇒ 显示"已完成 / 已入库" |
| 同上 | 失败 / 取消 ⇒ 明确显示终态，**不得**读成已分析 |
| 同上 | 后继消失 ⇒ 显示"无法确认"，不得假设成功 |
| 同上 | 排队失败 ⇒ 显示原因（`JobConflict` 等） |
| 同上 | 无回退的普通任务 ⇒ **不渲染**追踪块 |
| 反向验证 | 移除 `<FallbackTracking>` ⇒ **7 项前端 RED** |

### R3.1 手动确认项（建议纳入 T01–T25）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| T-R31-01 | 设 `max_calls=1` 并让模型立即成功 | 恰好 1 次真实请求，台账 calls=1、bytes=该请求 Body 长度 |
| T-R31-02 | 让模型连续 503 且配额为 1 | 只有 1 次请求发出，第 2 次**在发出前**被拒（`MODEL_BUDGET_EXCEEDED`） |
| T-R31-03 | 提交 `max_retries=0` 的任务 | 实际只发 1 次；快照记录与实际一致 |
| T-R31-04 | 同时运行两个 `timeout_sec` 不同的任务 | 各自按自己的超时执行，互不影响 |
| T-R31-05 | 允许回退后查看原视频任务 | 能看到**后继 Job ID 与其实时状态**；后继未成功前**不显示**"已完成" |
| T-R31-06 | 回退已排队但后继失败 | 原任务明确显示回退失败，**不得**显示已分析 |
| T-R31-07 | 项目内有其他互斥任务时请求回退 | 回退排队被拒并**显示原因**，不留半排队残留 |


## M5.3-R3.2 / TSB-356～359

| 用例 | 任务 | 守护行为 |
|---|---|---|
| VideoFallbackPolling F01/F02/F03/F08 | TSB-356 | 父终态后继续观察活跃后继；快任务首响应终态也不丢；子终态停止请求 |
| F04/F05/F06/F10 | TSB-356 | 完成刷新并显示实际多帧结果；部分失败/失败/取消/缺失诚实呈现，不伪造原生视频记录 |
| F07/F09 + network/pause + permission/new-job（前端本文件共 19 场景） | TSB-356 | 普通 Job 不额外轮询、卸载/切项目清除旧读数、新任务替换旧查询并恢复刷新、错误恢复、界面暂停不取消任务，回退独立默认关闭 |
| test_video_fallback_runner_e2e B01 | TSB-357 | 无上传确认在业务授权边界拒绝：不抽帧、不请求模型；API 允许建立任务不等于上传获准 |
| B02/B03 | TSB-357 | 正式 API → 真实 submit/_run，父成功、唯一子 pending→running→终态，真实开始/结束时间与锁互斥 |
| B04/B05 | TSB-357 | 真实 FFmpeg、Provider 序列化 ≥2 张 JPEG、仅回环 Mock、去 EXIF、选中镜头实际入库且公开 API 可查 |
| B06 | TSB-357 | segment_ids/limit/force/consent 传入子任务，不处理未选镜头 |
| B07 | TSB-357/358 | 源 SHA/大小/mtime、评分/备注/检测与编辑边界、历史人工镜像及正式 verdict API 追加的判定历史在重跑后不变 |
| B08（成功/排队取消/运行取消） | TSB-357/358 | 自有 stage 清理、锁释放；排队取消不抽帧/上传；取消历史父任务不取消子任务 |
| B09（schedule/model/invalid/ffmpeg） | TSB-357/358 | 入队拒绝、Mock 400、无效 200、媒体失败均稳定诊断，不显示成功分析 |
| B10 + 6 项逆向验证 | TSB-357/358/359 | 删除真实入库、绕过 Runner、仅凭状态判成功、丢 force、恢复父误报失败、移出强制媒体清单分别 RED |
| B10 有效命令校验 + CI_FOLD_RED/REVERSE | TSB-359 | 解析 YAML 折叠后的实际 shell 参数，清单中每个套件均须进入 argv；内嵌 # 不得截断后续预算/回退用例，不能只查文件里是否有名字 |
| test_fallback_result_contract S01/S02 | TSB-358 | 活跃子未完成；成功需同项目/资产/镜头/精确区间/本次时间窗/有效帧证据和正计数 |
| S03（14 种空/失效/畸形证据） | TSB-358 | succeeded 空结果、零计数、历史/别的镜头、失效区间、缺帧或坏帧一律 FALLBACK_NO_RESULTS |
| S04 + Runner S04 | TSB-358 | 真正部分成功仅报告实际入库子集和错误码，analysed=false |
| S05/S06 + 前端 S06 | TSB-358 | 失败/取消/缺失/跨项目/旧 Job/调度失败兼容，不泄露原始错误或无限轮询 |
| S07 | TSB-358 | 详情/列表读数一致、只读解析不更改人工状态和镜头边界 |

以上逐一覆盖任务书 F01～F10、B01～B10、S01～S07 共 27 个验收编号；参数化场景数与编号数分开计。后端专项三个文件合计 65 passed / 0 skipped，其中新 Runner 17、状态契约 26、原追踪升级 22。两份新后端文件均加入 Required FFmpeg 清单；既有 skip guard 未放宽。

T25 手动步骤补充：显式选择“允许本次回退”并单独确认上传；父任务结束后核对后继编号和 pending→running→终态；确认多帧卡出现且原生视频数量不增加；缺失/失败不可当成功。暂停只停界面刷新，取消应在项目页选择真实后继编号。真人与真实模型验收未执行，G5/G6 不变。


## TSB-360 / TSB-361 测试基建（Windows 全量稳定性）

TSB-360 修的是「跑不起来」（裸 `ffmpeg` 找不到、无符号链接权限、全局注册表跨用例
累积）；TSB-361 修的是「跑起来但互相干扰」。两个偶发失败**同源**：一个用例留下的
可执行工作，跑到另一个用例的体内。有两条泄漏路径，缺口都在 `backend/tests/conftest.py`
的 autouse 屏障 `_no_background_jobs_left_behind`：

1. **已提交、未等待** —— 直接调任务函数的用例把回退子任务提交到模块级 `runner` 后
   就返回，晚到的 `motion.extract_frame` 落进下一个用例 monkeypatch 的 tracker；
2. **未派发、被恢复** —— 同一类用例把行留在 `pending`，下一个用例的 `client` 启动
   lifespan 时 `resume_interrupted_jobs()` 把它当"上次崩溃遗留"重新派发，
   在**那个用例体内**执行。

屏障因此是两步：`drain_background_jobs()`（等 `runner._futures` 全部完成，超时即
fail）+ `settle_unfinished_jobs()`（把 `pending/running/cancelling` 行落成终态，
使恢复扫描不再选中）。断言强度未降低：`test_vision_cleanup_status.py` 的既有用例
一行未改，仍按全表快照比较。

| 用例 | 任务 | 守护行为 |
|---|---|---|
| `test_tsb361_job_lifecycle.py::test_a01_a/_b` | TSB-361 | 用例留下仍在跑的后台 Job 即返回；下一用例看到 `succeeded`（真跑完），不是被兜底取消 |
| `::test_a02_a/_b` | TSB-361 | 测试体之后才发的抽帧事件只落进发起它的用例，后一个用例 tracker 一条都没有 |
| `::test_a03_two_concurrent_jobs_keep_their_own_trackers` | TSB-361 | 两个 Job 真并发（`threading.Barrier` 保证同时在跑），各自 tracker 无交集 |
| `::test_a04_a/_b` | TSB-361 | 运行中取消：等 worker 自己退出后行才落 `cancelled`，`started_at/finished_at` 齐全 |
| `::test_a05_cleanup_runs_even_when_the_body_fails` | TSB-361 | 主体断言失败时 teardown 仍把 Job 收干净（直接驱动屏障生成器复刻 pytest 行为） |
| `::test_a06_a/_b` | TSB-361 | `future.add_done_callback` 的晚到回调不写进后来者的 tracker |
| `::test_a07_a/_b` | TSB-361 | 后台任务抛异常 ⇒ `failed` + 错误原文可见，不假装成功 |
| `::test_a08_repeating_the_same_scenario_yields_the_same_outcome` | TSB-361 | 同一场景连跑 3 轮，终态/统计/收敛性完全一致 |
| `::test_a09_a_pending_row_is_never_resumed_into_a_later_test` | TSB-361 | `pending` 行落成终态后 `resume_interrupted_jobs()` 不再选中它（路径②） |
| `test_vision_cleanup_status.py::test_b01_...` | TSB-361 | 模型成功 + 清理失败：请求次数、入库行数、staging 残留、`partial_failed`、`analyzed=1` 五项各自可见 |
| `::test_b02_...` | TSB-361 | 连续两次 cleanup-retry 都不重新请求模型、不重复写结果 |
| `::test_b03_...` | TSB-361 | 两个 Job 并发清理只动自己的暂存，`removed_bytes` 各自准确 |
| `::test_b04_...` | TSB-361 | 注册表里他人遗留的登记不影响本 Job 结论，其文件与 stats 均不被改动 |
| `::test_b05_...` | TSB-361 | receipt 丢失后同路径出现新目录 ⇒ `STAGING_NOT_OWNED`，一个字节都不删 |
| `::test_b06_...` | TSB-361 | 删到一半抛 `OSError` ⇒ `CLEANUP_FAILED` + `retryable` + 残留字节 + 审计条目 |
| `::test_b07_...` | TSB-361 | 同一目录反复清理幂等（`removed_files` 1→0→0），邻接目录不受影响 |
| `::test_b08_...` | TSB-361 | motion 与 semantic 在同一条清理状态契约上逐字段相等，且与期望契约相等 |

A 组 14 项 + B 组 10 项（B01/B02 各 2 个参数、其余各 1 个），`collected` 由 1349
升到 1383（+2 进程树守卫、+8 注解器）。B 组保留 `motion` 参数化（任务书 §5.3 明确要求），未删除任何既有用例、
未新增 skip/xfail。

此外补了两组与本轮 CI 直接相关的守卫：

| 用例 | 任务 | 守护行为 |
|---|---|---|
| `test_media_process_tree.py::test_a_half_written_pid_file_is_not_treated_as_observable` | TSB-361 | 空 pid 文件 `exists()` 为真但 `_pids_ready()` 为假——取消触发不得用 `exists()` |
| `::test_a_missing_pid_file_is_not_an_error` | TSB-361 | 触发函数在 `run()` 的轮询里对缺失文件返回假而不是抛异常 |
| `test_ci_annotate.py`（8 项） | TSB-361 | 失败→`::error`（nodeid + 首行断言）、跳过→`::warning`（带原因）、缺 XML 不算失败、summary 落盘、截断到注解上限、两个 CI 作业都必须调用且读自己的 junit |

`test_media_process_tree.py` 的 5 个取消触发点由 `pid_file.exists()` 统一换成
`_pids_ready()`：文件刚被创建、内容还没落盘时 `exists()` 已为真，取消会落在那个窗口里，
把整棵树杀掉后文件永远停在空/半个 pid，于是"the tree was never observable"——
CI run #89 就是这么红的（见 [RELEASE_TSB_361.md](RELEASE_TSB_361.md) §八·附）。

**验证口径**：修复前后用同一组"前序 + 目标"命令对照（红→绿），再叠加 A→B、B→A
两种顺序、相关后台 Job 用例混跑、连续 3 轮、Windows 严格全量；证据在
[docs/validation/tsb361/](validation/tsb361/)。单例通过不算关闭。

## TSB-362 真实端点验证（阶段 A 只读核对 + R3 帧对照接线）

发布记录：[docs/RELEASE_TSB_362.md](RELEASE_TSB_362.md)
证据目录：[docs/validation/tsb362/](validation/tsb362/)

### TSB-362 / R3 单帧对照必须真的只发一帧

**原缺陷**：`tools/probe_video_capability.py::run_probe` 声明了 `frame_only` 参数却
**从未读取**（AST 实测函数体内 0 次引用），视频部件只由 `include_video` 决定。
于是 `run_multi_round_probe` 的"单帧负对照"实际发送的是**完整 MP4**，等价于第 4 个
视频轮次。它几乎必然答对，`classify_probe_outcome` 因此落到
`video_accepted_sampling_unknown`，把一个**真能读时序**的端点判成未证实——
而 `SENDABLE_STATES` 只认 `verified_native_video`，视频通道就被假证据永久锁死。

**为什么 TSB-342 的 W1–W4 没抓到**：那组用例用 `_fake_probe_factory` 整体替换了
`run_probe`，假探测**自己**遵守 `frame_only`（`_fake_probe_factory` 的 behaviour
签名里就有该参数），所以真实实现的丢弃行为从未被覆盖；`test_w1` 对帧对照只断言
`frame_control_attempted is True`，不检查 `frame_only` 是否真的生效。属
AGENTS.md §3.2「接线要单独测」同一类缺陷。

| 用例 | 任务 | 守护行为 |
|---|---|---|
| `test_probe_temporal_evidence.py::test_w05_the_still_frame_control_sends_no_video` | TSB-362 | 拦截 `httpx.post` 截获**真实** `run_probe` 构造的 payload：`frame_only=True` 时必须**没有** `video_url` 部件、**恰有 1 个** `image_url` 部件，且为 `data:image/jpeg;base64,` |
| `::test_w06_a_normal_round_still_sends_the_video` | TSB-362 | 反向守卫：修复不得让正常轮次退化成静帧——`include_video=True, frame_only=False` 时恰有 1 个 `video_url`、0 个 `image_url` |
| 反向验证 | TSB-362 | 把 `if frame_only:` 改回 `if False:` → W05 立刻红（`parts were ['text','video_url']`），W06 仍绿；恢复后 W05/W06 双双转绿 |

**未发任何网络请求**：W05/W06 用 monkeypatch 拦截 `httpx.post`，只用 FFmpeg 生成
本地合成短片与 1 帧 JPEG，属纯本地回环测试；真实端点调用仍受 `--execute` +
`--i-authorize-real-upload` 双重授权保护。

**修复副作用（已知且有意）**：单帧对照现在是真正的阴性对照，向后兼容地**多一次**本地
FFmpeg 抽帧，并把 1 帧 JPEG（而非 MP4）作为 `image_url` 部件发出。抽帧失败时
`run_probe` 返回 `FRAME_CONTROL_UNAVAILABLE` 且**不发送**，遵循 C6「不静默跳过」。

## TSB-365 纯审计索引（2026-11-05）

本轮未新增或执行测试，不改变既有用例、T01–T25 或发布门禁。
现有用例→能力证据映射见 [能力矩阵](audit/TSB-365_CURRENT_CAPABILITY_MATRIX.md)。
新增能力的待编写用例、任务号及可观察验收标准见
[目标架构 §6](audit/TSB-365_TARGET_ARCHITECTURE.md#6-分阶段任务拆分与验收)。
这些是后续任务要求，不计入当前通过数。审计交付记录见
[TSB-365](RELEASE_V2_1_2_TSB365_AUDIT.md)。



## TSB-366 方案版本与兼容迁移

发布记录：[docs/RELEASE_TSB_366.md](RELEASE_TSB_366.md)
证据目录：[docs/validation/tsb366/](validation/tsb366/)

**原缺陷**：`EditPlanShot` 每次保存先 `DELETE` 全部行再重插，没有历史、没有身份、
没有并发保护；任何调用方都能无版本保护地全量替换计划。

### 模型与约束（`test_tsb366_storyboard_model.py`，14 项）

| 用例 | 守护行为 |
|---|---|
| `test_models_exist_with_the_required_columns` | 三张表与 §2 规定的字段齐全 |
| `test_one_master_storyboard_per_project_is_enforced_by_the_database` | 每项目最多一个主方案，由 `UNIQUE(project_id)` 保证（第二个 INSERT 抛 IntegrityError） |
| `test_version_no_is_unique_per_board` | 同 board `version_no` 唯一 |
| `test_two_boards_may_each_have_a_version_one` | 唯一性只在 board 内作用，不误伤其他项目 |
| `test_parent_version_must_belong_to_the_same_board` | 跨 board 父版本被触发器拒绝 |
| `test_current_version_must_belong_to_the_same_board` | 当前指针不得指向别的 board |
| `test_snapshot_requires_a_schema_version` / `_rejects_unknown_fields` | 快照必须带 schema_version；未知字段拒绝 |
| `test_snapshot_preserves_second_precision` | 秒数不提前取整 |
| `test_snapshot_requires_stable_shot_ids` / `_round_trips_through_json` | 每个镜头有稳定 id；JSON 往返一致 |
| `test_source_ref_is_unique_per_version_and_shot` | 派生索引唯一 |
| `test_deleting_a_storyboard_never_deletes_assets_or_segments` | 删除方案**不删素材**（历史证据不丢） |
| `test_reading_a_version_does_not_change_the_current_pointer` | 读历史不改变 current |

### 迁移（`test_tsb366_migration.py`，10 项，对应任务书 §7.2 的 10 个数据集）

| 用例 | 守护行为 |
|---|---|
| `test_a_normal_legacy_plan_imports_losslessly_as_version_1` | 全部字段 + 数组顺序无损；旧表行仍在 |
| `test_an_empty_legacy_plan_yields_an_empty_version_with_no_invented_shots` | 空计划不虚构镜头 |
| `test_the_same_file_name_under_different_asset_ids_stays_distinct` | 同名文件按 Asset ID 区分，不按文件名猜 |
| `test_an_orphan_segment_is_reported_not_silently_repaired` | 孤儿 segment → `needs_review`，快照仍可读 |
| `test_an_asset_segment_mismatch_is_reported` | asset/segment 不匹配 → 标记 |
| `test_missing_source_signature_is_marked_legacy_not_fabricated` | 无签名 → `legacy`，不伪造哈希 |
| `test_running_the_migration_twice_creates_nothing_new` | 幂等 |
| `test_a_project_that_already_has_a_storyboard_is_skipped` | 已迁移项目跳过 |
| `test_a_failure_mid_project_leaves_no_half_written_board` | 中途失败 → 无半迁移，后续重跑可成功 |
| `test_the_migration_report_counts_are_consistent` | 报告六项计数覆盖全部项目 |

### 版本读写与并发（`test_tsb366_versions.py`，19 项）

| 用例 | 守护行为 |
|---|---|
| `test_first_read_returns_version_1` / `_read_response_carries_every_contract_field` | 首次读取返回 v1；§4.1 全部字段存在 |
| `test_versions_are_listed_newest_first` | 历史稳定排序 |
| `test_save_creates_version_2_and_leaves_version_1_byte_identical` | 保存产生 v2，**v1 逐字段不变** |
| `test_two_clients_on_the_same_base_only_one_wins` | 同基版本并发 → 一个成功、一个 409，当前版本不动 |
| `test_retrying_the_same_request_does_not_create_a_duplicate_version` | 幂等键重试不产生 v+2 |
| `test_an_invalid_snapshot_writes_nothing` | 非法快照不部分写入 |
| `test_restoring_v1_creates_v3_instead_of_moving_the_pointer_back` | 恢复是**前向**的：生成 v3，v1/v2 可读 |
| `test_restore_records_the_target_and_validation_state` | 记录恢复目标与校验状态 |
| `test_a_version_from_another_project_is_refused` / `_unknown_version_id_is_a_uniform_not_found` | 跨项目不可探测；不存在统一 404 |
| `test_the_http_contract_for_read_save_conflict` | 缺 base → **428**；过期 base → **409** |
| `test_restore_over_http` | HTTP 恢复链路 |
| `test_all_exports_come_from_the_same_version_and_report_it` | JSON/CSV/OTIO 同 version_id，响应头记录 board/version |
| `test_a_historical_version_can_be_exported_without_touching_current` | 导出历史不改当前指针 |
| `test_the_old_read_endpoint_still_answers_with_the_legacy_shape` | 旧 GET 响应结构保持 |
| `test_the_roughcut_uses_the_current_storyboard_version` | 粗剪读版本，不读旧表 |
| `test_no_legacy_write_path_bypasses_the_version_gate` | 适配器必须带基版本且不得直写旧表 |

### 运维层（`test_tsb366_schema_ops.py`，3 项）

| 用例 | 守护行为 |
|---|---|
| `test_the_alembic_revision_declares_the_same_triggers_as_the_app` | Alembic 与 `init_db` 的触发器集合**不得漂移** |
| `test_the_revision_downgrade_never_touches_the_legacy_plan_table` | downgrade 源码不得出现 `edit_plan_shots` |
| `test_upgrade_downgrade_round_trip_preserves_legacy_rows` | 真实隔离库 round-trip：降级删表但旧计划 1 行保留，升级后触发器恢复 |

### 被迁移的既有用例（断言强度不降）

旧写路径 `build_edit_plan` 变为带基版本的薄适配器，以下文件的用例随之迁移，
**每条原有校验仍然被断言**，只是从"返回 errors 数组"改为"抛 SnapshotError"或"标记 invalid 版本"：

| 文件 | 变化 |
|---|---|
| `test_timeline_export.py` | 新增 `_save`/`_save_error` 辅助；`order_adjusted` 不再存在（数组位置即顺序）；越界区间改为标记 `invalid` 并**额外断言旧版本未变**（比原来更严） |
| `test_concurrent_annotations.py` | 3 项改为经版本服务；写锁探针装到 `validate_references` |
| `test_cancel_cleanup.py` / `test_publish_cancel.py` | `_add_shot` 改写版本 |
| `test_review_validation.py` | `plan_shot_ids` 来自版本快照；`_assert_plan_points_at` 校验历史引用不被重绑 |
| `test_media_format_matrix.py` / `test_long_paths.py` / `test_ffmpeg_integration.py` | edit-plan 调用补 `base_version_id` |

### 反向验证

| 破坏项 | 结果 |
|---|---|
| 去掉基版本冲突检查 | 2 红（并发 + HTTP 契约） |
| 去掉迁移幂等 | 2 红 |
| 去掉不可变触发器 | 快照可被改写（`IMMUTABILITY NOT ENFORCED`）；恢复后 `IntegrityError` |

### 前端（`Export.test.tsx`，新增 3 项）

| 用例 | 守护行为 |
|---|---|
| `shows the current storyboard version and sends base_version_id on save` | 显示 `vN`；保存提交 `base_version_id` |
| `a 409 conflict keeps the draft and shows the conflict banner` | 冲突时**草稿保留**并提示 |
| `the history panel lists versions and offers restore for non-current ones` | 历史面板列出并标记当前版本 |


## TSB-366-R1 版本化底座质量收口

发布记录：[docs/RELEASE_TSB_366_R1.md](RELEASE_TSB_366_R1.md)
证据目录：[docs/validation/tsb366-r1/](validation/tsb366-r1/)

**原缺陷**（5 项，均在 `8f2d4eb` 实测复现）：

| 编号 | 问题 | 案例后果 |
|---|---|---|
| P0-01 | 历史版本读取时 `export_json` 未收到 `version_id` | 请求 v1(3 镜头) 返回 v1 标签 + v2 的 8 个镜头 |
| P0-02 | 前端 `DraftShot` 无 `shot_id` | 只改一个时长，全部镜头身份重建 |
| P1-03 | 恢复历史版本无确认且直接 `dirty=false` | 未保存草稿被静默丢弃 |
| P1-04 | 迁移失败被吞 + 无 board 时自动建空版本 | 空方案取代旧方案，迁移永久跳过 |
| P1-05 | 对账以 `current_version_id` 为基准 | 编辑后对账对象变成 v2/v3 |

### 专项用例（`backend/tests/test_tsb366r1_regressions.py`，24 项）

| 用例 | 守护行为 |
|---|---|
| `test_p0_01_get_edit_plan_returns_the_requested_historical_version` | 请求 v1 必须得到 v1 的 3 个镜头与 v1 的总时长 |
| `test_p0_01_json_export_of_a_historical_version_is_self_consistent` | JSON 镜头数/segment/入点全属 v1 |
| `test_p0_01_csv_export_of_a_historical_version_is_self_consistent` | CSV 行数属 v1 |
| `test_p0_01_otio_export_of_a_historical_version_is_self_consistent` | OTIO clip 数属 v1 |
| `test_p0_01_reading_a_historical_version_does_not_move_the_pointer` | 四种读取入口均不改 `current_version_id` |
| `test_p0_01_the_default_read_still_returns_the_current_version` | 不带 version_id 的兼容行为不变 |
| `test_p0_02_an_edit_round_trip_preserves_every_shot_id` | 改时长不改变任何身份 |
| `test_p0_02_adding_a_shot_only_creates_one_new_identity` | 新增只产生 1 个新 id |
| `test_p0_02_removing_a_shot_leaves_the_others_untouched` | 删除不影响其他身份 |
| `test_p0_02_reordering_keeps_identity_tied_to_the_shot_content` | 排序后 `shot_id→segment_id` 不变，时间线按数组顺序 |
| `test_p0_02_the_legacy_endpoint_preserves_shot_ids_too` | HTTP 写路径同样保留身份 |
| `test_p1_04_a_project_with_no_legacy_plan_may_initialize_an_empty_board` | 新项目正常初始化空 v1 |
| `test_p1_04_a_migrated_legacy_project_reads_its_v1` | 迁移后正常读 v1 |
| `test_p1_04_a_failed_migration_must_not_yield_an_empty_initial_version` | 失败后拒绝造空版本且不写数据 |
| `test_p1_04_a_failed_migration_records_a_queryable_error_then_retries` | 失败可查、重试成功 |
| `test_p1_04_a_retry_is_idempotent_and_leaves_migrated_projects_alone` | 重试不重复建版本 |
| `test_p1_04_retry_does_not_disturb_a_project_that_has_been_edited` | 重试不改当前指针 |
| `test_p1_04_the_legacy_rows_are_never_modified_by_migration_or_retry` | 旧行逐字段不变 |
| `test_p1_05_reconcile_still_targets_v1_after_later_edits` | 编辑后对账仍以 v1 为基准 |
| `test_p1_05_reconcile_detects_real_drift_against_v1` | 真实差异能被发现 |
| `test_p1_05_reconcile_reports_a_missing_migration_version` | 缺迁移版本被报告 |
| `test_p1_05_reconcile_never_modifies_any_version` | 对账只读 |
| `test_p1_cross_project_asset_and_segment_are_never_treated_as_valid` | 跨项目引用永不视为有效 |
| `test_p1_a_new_version_may_not_save_a_shot_whose_asset_is_absent` | 显式拒绝模式返回可识别错误 |

### 前端专项（`Export.test.tsx`，新增 8 项）

| 用例 | 守护行为 |
|---|---|
| `P0-02 keeps every shot_id through load, edit and save` | 保存 payload 中身份完整 |
| `P0-02 an added shot receives a new id while the others keep theirs` | 新镜头交由服务端分配 id |
| `P1-03 restoring a clean editor needs no confirmation` | 无草稿时不打扰用户 |
| `P1-03 cancelling the confirmation sends no restore request and keeps the draft` | 取消 → **0 请求**、草稿不变 |
| `P1-03 confirming sends the restore request` | 确认 → 恰好 1 次请求 |

### 已知遗留

`tests/test_vision_provider.py::test_status_classification[408-MODEL_TIMEOUT]` 在 `8f2d4eb`
与本轮 HEAD 上**均失败**（已用 `git stash` 在纯净基线双向取证），属基线既有问题，
本轮**未修复也未跳过**，不计入本轮缺陷。


## TSB-367 结构化操作协议与引用校验

发布记录：[docs/RELEASE_TSB_367.md](RELEASE_TSB_367.md)
差距分析：[docs/validation/tsb367/GAP_ANALYSIS.md](validation/tsb367/GAP_ANALYSIS.md)

**原缺陷**：任何 Storyboard 修改都只能整份覆盖提交（`POST /edit-plan` 全量替换），
没有操作级校验、没有锁定规则、没有操作回执，也无法为未来 AI 调用提供安全中间层。

### 阶段 0 遗留收口（`test_tsb367_stage0.py`，13 项）

| 用例 | 守护行为 |
|---|---|
| `test_leftover_1_pending_migration_has_a_stable_error_code` | 迁移未完成 → **409 LEGACY_MIGRATION_PENDING**，含可操作 `action`，**不含**堆栈/SQL/路径 |
| `test_leftover_1_a_pending_migration_is_never_answered_with_an_empty_plan` | 绝不用空方案回应待迁移项目 |
| `test_leftover_1_an_unknown_project_is_still_a_plain_404` | 未知项目仍为 404 |
| `test_leftover_2_migration_status_is_queryable_over_http` | 状态可查询 |
| `test_leftover_2_retry_migrates_a_pending_project` | 授权用户可重试 |
| `test_leftover_2_retry_is_idempotent_and_does_not_touch_migrated_projects` | 重试幂等，不建第二个 v1 |
| `test_leftover_2_retry_leaves_a_projects_current_pointer_alone` | 重试不改已编辑项目的指针 |
| `test_leftover_2_retry_then_read_works_after_a_failure` | 失败后重试可恢复读取 |
| `test_leftover_2_retry_reports_a_still_failing_migration` | 持续失败时**不谎报成功** |
| `test_leftover_3_a_revision_mentions_legacy_plan_migrations` | Alembic 与运行时建表一致 |
| `test_leftover_3_downgrade_never_drops_the_legacy_plan_table` | downgrade 不删旧计划 |
| `test_leftover_3_init_db_remains_the_runtime_entry_and_alembic_matches` | 两条路径表集合一致 |

### 模型与状态机（`test_tsb367_model.py`，13 项）

| 用例 | 守护行为 |
|---|---|
| `test_plan_operation_table_has_the_contract_columns` | 契约字段齐全 |
| `test_status_values_are_a_closed_set` | 非法状态被数据库拒绝 |
| `test_idempotency_key_is_unique_per_board` / `test_the_same_key_may_be_used_on_a_different_board` | 幂等键作用域正确 |
| `test_base_version_must_belong_to_the_board` | base 必须属同 board |
| `test_applied_requires_a_result_version_in_the_same_board` | applied 必须有同 board 结果版本 |
| `test_applied_with_a_valid_result_version_is_accepted` | 合法 applied 可写入 |
| `test_a_terminal_operation_cannot_be_rewritten` / `test_a_rejected_operation_is_also_terminal` | 终态不可改写 |
| `test_only_one_applied_operation_per_result_version` | 一个结果版本只被一个操作认领 |
| `test_lock_dimensions_validate_and_normalise` / `test_all_expands_to_every_dimension` / `test_legacy_boolean_lock_still_reads_back` | 锁定维度校验、`all` 展开、旧布尔兼容 |

### 操作协议 Schema（`test_tsb367_schema.py`，32 项）

信封必填字段、未知 `schema_version`、未知字段、**伪造服务端字段**（status/diff/result_version_id）、
空/超限操作数、超长 reason、未知操作类型、每类操作的必填与互斥规则、NaN/Infinity/负数/字符串数字、
倒置区间、未知锁维度、`replace` 不得由客户端指定新身份、未知约束字段、序列化稳定、
**payload digest 与 key 无关而与内容相关**。

### 纯函数执行器（`test_tsb367_executor.py`，31 项）

输入快照**不被修改**；同输入同输出；八类操作的正向/负向/边界；锁定拒绝与放行；
插入/替换的**服务端造身份**；跨项目与 asset-segment 不匹配拒绝；越界拒绝；
删除锁定镜头拒绝；批量全有或全无；操作已删镜头报错清晰；约束冲突被报告；
结果时间线连续。

### 锁定矩阵（`test_tsb367_lock_matrix.py`，25 项）

4 维度 × 5 操作 = **20 格逐格**断言；元测试比对代码矩阵与公布表；
组合维度取并集；解锁后恢复可编辑；移动不丢锁。

### 事务、并发、幂等（`test_tsb367_transactions.py`，16 项）

propose 产生预览 diff 不建版本；apply 恰好建一个版本；**基版本逐字节不变**；
SourceRef 同事务写入；失败批次**零副作用**；未知操作 404；
过期 base 冲突；**同 base 两客户端只一个成功**；同 key 同 payload 返回原操作；
同 key 异 payload 拒绝；applied 重放不建新版本；reject 不建版本且幂等；
rejected 不可 apply；applied 不可 reject；跨项目操作不可见；跨 board base 拒绝。

### API 契约（`test_tsb367_api.py`，19 项）

propose→apply 闭环；读状态与 diff；reject 不建版本；未知类型 400；畸形信封 400；
**伪造字段 422**（三种）；缺失镜头 404；锁定冲突 409；越界 422；过期 base 409；
同 key 返回同一操作；同 key 异 payload 409；**跨项目 board/operation 均 404**；
未知项目 404；版本 diff 与首版空 diff。

### 前端（`OperationPanel.test.tsx`，7 项）

展示**服务端 diff**（本地不计算）；确认恰好 apply 一次；拒绝走 reject 且无版本；
**409 保留待提交操作**并提示；apply 失败显示错误而非成功；无待提交操作时按钮禁用。

### 反向验证

| 破坏项 | 结果 |
|---|---|
| 去掉 apply 时 base_version CAS | 并发用例变红 |
| 去掉 `_check_lock` | 锁定矩阵多格变红 |
| 绕过幂等键检查 | 幂等两项变红 |


## TSB-367-R1 结构化操作协议质量收口

发布记录：[docs/RELEASE_TSB_367_R1.md](RELEASE_TSB_367_R1.md)
差距分析：[docs/validation/tsb367-r1/GAP_ANALYSIS.md](validation/tsb367-r1/GAP_ANALYSIS.md)

**原缺陷**：TSB-367 的 9 处实现缺陷——身份重生成、状态被回滚吞掉、
最终校验不生效、历史迁移分叉、move/lock/section 边界错误、无真并发测试、
前端只有两个写死第一个镜头的按钮。

### R1-01 身份一致性（`test_tsb367r1_identity.py`，9 项）

| 用例 | 守护行为 |
|---|---|
| `test_r1_01_insert_preview_id_equals_final_version_id` | insert 预览 ID = 最终版本 ID |
| `test_r1_01_insert_identity_is_persisted_on_the_proposal` | 身份随提案持久化 |
| `test_r1_01_multiple_inserts_each_get_a_distinct_stable_id` | 多 insert 各自稳定 |
| `test_r1_01_replace_preview_id_equals_final_version_id` | replace 预览 ID = 最终 ID |
| `test_r1_01_replace_mapping_is_recorded_per_operation` | replace 映射按操作记录 |
| `test_r1_01_multiple_replaces_map_correctly` | 多 replace 映射不串号 |
| `test_r1_01_confirming_twice_does_not_mint_a_second_identity` | 重复确认不铸造新身份 |
| `test_r1_01_a_client_cannot_forge_the_internal_identity` | 客户端伪造内部身份被拒 |
| `test_r1_01_applying_a_proposal_without_identity_fails_instead_of_guessing` | 缺身份拒绝猜测 |

### R1-02 状态持久化（`test_tsb367r1_lifecycle.py`，12 项）

| 用例 | 守护行为 |
|---|---|
| `test_r1_02_a_stale_proposal_is_committed_as_stale` | 过期提案真实落库为 stale |
| `test_r1_02_a_stale_operation_cannot_be_applied_later` | stale 终态不可重试 |
| `test_r1_02_a_deterministic_failure_is_committed_as_failed` | 执行失败真实落库为 failed |
| `test_r1_02_a_failed_apply_creates_no_version` | 失败不留半成品版本 |
| `test_r1_02_applied_and_result_version_commit_together` | applied 与结果版本同事务 |
| `test_r1_02_terminal_states_cannot_move`（4 参数） | 四种终态均不可改写 |
| `test_r1_02_an_illegal_status_is_rejected_on_insert` | INSERT 非法状态被拒 |
| `test_r1_02_applied_without_result_version_is_rejected_on_insert` | INSERT applied 缺结果被拒 |
| `test_r1_02_a_status_write_never_overwrites_a_committed_terminal_state` | 状态写入必须条件化 |

### R1-03 最终校验（`test_tsb367r1_validation.py`，12 项）

`test_r1_03_a_healthy_plan_validates_ok` · `..._an_asset_that_does_not_exist_is_invalid` ·
`..._a_segment_that_does_not_exist_is_invalid` · `..._an_asset_segment_mismatch_is_invalid` ·
`..._a_cross_project_reference_is_invalid` · `..._a_range_outside_the_segment_is_flagged` ·
`..._a_duplicate_shot_id_is_invalid` · `..._an_invalid_plan_cannot_become_a_proposal` ·
`..._an_invalid_final_snapshot_never_becomes_the_current_version` ·
`..._needs_review_is_saved_and_recorded_on_the_version` ·
`..._a_historical_version_with_broken_references_stays_readable` ·
`..._nothing_is_auto_repaired_or_dropped`

### R1-04 迁移路径（`test_tsb367r1_migration_paths.py`，8 项）

`..._a_fresh_alembic_upgrade_creates_every_table` ·
`..._a_historical_database_reaches_the_same_schema`（**本轮关键回归**） ·
`..._upgrading_a_historical_database_preserves_its_data` · `..._upgrading_twice_is_a_noop` ·
`..._init_db_and_alembic_produce_the_same_structure`（列/类型/外键/触发器） ·
`..._the_shared_indexes_exist_on_both_paths` · `..._downgrade_to_base_leaves_no_storyboard_tables` ·
`..._downgrade_never_drops_the_legacy_plan_table`

### R1-06/07/08 语义（`test_tsb367r1_semantics.py`，17 项）

跨章节移动写入 section、无 section 保持原章节、不存在章节拒绝、无悬挂引用、
position 锁拒绝、`after_self`/`before_self` 无操作、自身锚点不抛异常、
自身锚点仍可换章节；`all` 部分解锁留 reference+position、trim 允许而 move/replace 拒绝、
逐维度解锁到底、存储规范化；章节移动不改镜头顺序、时间线按镜头数组、合同在代码中、交错章节被标记。

### R1-09 真并发（`test_tsb367r1_concurrency.py`，6 项）

`..._two_simultaneous_applies_produce_exactly_one_version` ·
`..._the_loser_is_persisted_as_stale`（精确断言 `{"applied","stale"}`） ·
`..._no_duplicate_version_numbers` · `..._no_half_written_version_survives` ·
`..._the_race_is_stable_across_repeats`（**5 轮**） · `..._three_simultaneous_applies_still_yield_one_version`

### R1-05 前端（`OperationPanel.r1.test.tsx`，11 项）

六类操作各自可提议并展示服务端 diff（6 参数化）、目标显式可见、确认恰好一次、
拒绝无版本、409 保留待提交操作、失败不显示成功、无待提交时禁用。

### 反向验证

| 破坏项 | 结果 |
|---|---|
| 去掉 stale/failed 状态持久化 | 5 项变红 |
| 去掉身份复用 | 6 项变红 |
| 关闭 invalid 阻断 gate | 2 项变红 |


## TSB-366-R2 Vision Provider 408 稳定性

发布记录：[docs/RELEASE_TSB_366_R2.md](RELEASE_TSB_366_R2.md)
根因分析：[docs/validation/tsb366-r2/ROOT_CAUSE.md](validation/tsb366-r2/ROOT_CAUSE.md)

**原缺陷**：本机 `test_status_classification[408-MODEL_TIMEOUT]` 连续多个轮次失败，
CI 却通过，形成"本机红、CI 绿"的长期噪音。

**根因**：Windows 系统代理（注册表 `ProxyEnable=1`, `ProxyServer=127.0.0.1:<proxy-port>`）
被 httpx 的 `get_environment_proxies()` 读取，而 Python **不解析 WinINET 的
`ProxyOverride` 绕过列表**（`127.*`、`localhost`），于是 loopback 请求被送往代理；
代理转发后破坏了 408 响应，客户端抛 `RemoteProtocolError`，Provider 归类为
`MODEL_NETWORK_ERROR`。分类函数本身**一直正确**。

**隐蔽点**：两种情况下 mock server **都收到了请求**，所以"请求已到达"的断言一直是过的。

### 修复

生产代码新增单一传输出口 `_post()`：**仅 loopback** 使用 `trust_env=False`，
远程端点行为不变（生产代理支持未被关闭）。

### 回归测试（新增 76 项）

| 文件 | 项数 | 守护行为 |
|---|---:|---|
| `test_vision_provider.py`（改造） | 43 | 408 增加请求次数/path/实际返回状态断言；mock 状态改为实例级；teardown join 线程 |
| `test_tsb366r2_matrix.py` | 29 | 纯函数分类表、传输层超时、连接失败、loopback 判定、端到端 408、mock 隔离、无残留线程 |
| `test_tsb366r2_loopback_proxy.py` | 4 | 代理会破坏 loopback 408 的**机制**、直连对照、辅助客户端必须 `trust_env=False`、Provider 策略存在 |

### 关键用例

| 用例 | 守护行为 |
|---|---|
| `test_classify_status_table`（10 参数） | 纯函数映射表 |
| `test_http_408_is_not_a_network_error` | 408 不得被判为网络错误 |
| `test_every_transport_timeout_maps_to_model_timeout`（4 参数） | 四种超时类 |
| `test_connection_failures_are_network_errors`（3 参数） | 三类连接失败 |
| `test_loopback_detection`（6 参数） | 127.0.0.1 / localhost / [::1] / 公网 / 内网 / 非法 |
| `test_the_post_choke_point_disables_env_for_loopback` | loopback 必须禁用 env 代理 |
| `test_a_real_408_through_the_adapter_is_model_timeout` | 端到端 408 |
| `test_mock_servers_do_not_share_state` | mock 隔离 |
| `test_the_provider_fixture_leaves_no_thread_behind` | 无残留线程 |
| `test_a_proxy_gets_between_the_client_and_loopback` | 复现机制（有代理时 408 被破坏） |
| `test_loopback_without_env_reaches_the_server_and_returns_408` | 直连对照 |
| `test_the_mock_server_client_is_direct` | 辅助客户端 `trust_env is False` |
| `test_the_provider_uses_a_client_that_ignores_the_environment` | `_is_loopback_url` 存在且正确 |

### 稳定性

| 要求 | 结果 |
|---|---|
| 目标 408 用例连续 20 次 | 20/20 |
| Provider 文件连续 5 次 | 5/5 |

### 反向验证

去掉 loopback 绕过 → 两项变红，错误正是原始 `RemoteProtocolError`；恢复后全绿。

### 已知边界

连接被拒在本机表现为 `ConnectTimeout` 而非 `ConnectError`（测试断言契约而非异常类）；
局域网地址仍走系统代理（当前未处理）。


## TSB-366-R3 分析包代表帧缺失（长路径）

发布记录：[docs/RELEASE_TSB_366_R3.md](RELEASE_TSB_366_R3.md)
根因与证据：[docs/validation/tsb366-r3/ROOT_CAUSE.md](validation/tsb366-r3/ROOT_CAUSE.md)

**原缺陷**：`test_complete_custom_workspace_pipeline_and_source_hashes` 在本机间歇失败、
CI 一直通过。失败信息 `代表帧缺失或预算不足` 把两种原因合并，且没提真正的重编码失败。

**根因**：`_safe_save()` 的长路径回退分支 `image.save(BytesIO, **kwargs)` 缺少 `format`。
目标路径超过 `MAX_PATH`(260) 时第一次 `image.save(path)` 失败，回退必然抛
`ValueError: unknown file extension`，被调用方当作"重编码失败"吞掉 → **所有代表帧静默丢弃**。

**逐层排除证据**：失败时刻合格镜头 5、有帧行镜头 5、帧行 5、范围内 5、越界 0、
路径解析失败 0、文件缺失 0 —— 四层全部健康，问题在保存层。

**触发阈值**：tmp_path 111 字符 → 帧路径 267 → 失败；tmp_path 87 → 帧路径 243 → 通过。

### 三处同类缺陷

| # | 位置 | 机制 |
|---|---|---|
| 1 | `_safe_save()` 回退分支 | `BytesIO` 无扩展名且无 `format` |
| 2 | contact sheet 保存 | 与 #1 同源，随 #1 修复 |
| 3 | **zip 写入** `zf.write(p, …)` | `zipfile` 内部 `os.stat()` 看不见 >MAX_PATH 路径 |

#2、#3 在修复 #1 后才暴露——此前被完全掩盖。

### 回归测试（新增 15 项）

| 文件 | 项数 | 守护行为 |
|---|---:|---|
| `test_tsb366r3_long_path.py` | 12 | 长路径保存并可重新打开；短/长路径结果一致；深目录创建写入读取；EXIF/GPS 清理；**强制回退分支（Linux 也覆盖）**；错误消息分因；诊断不泄漏私人路径 |
| `test_tsb366r3_package_deep.py` | 3 | 深路径 package 的帧**确实进入 `segments.json` 与 zip**；四类计数分开；全失败消息可操作 |

关键用例：

| 用例 | 守护行为 |
|---|---|
| `test_safe_save_succeeds_past_max_path` | >260 路径保存成功且可重新打开 |
| `test_safe_save_long_path_result_matches_a_short_path` | 同输入同 quality ⇒ 像素一致 |
| `test_sanitized_copy_succeeds_past_max_path` | 包内重编码器端到端 |
| `test_safe_save_strips_exif_and_gps` | 隐私清理（无 EXIF、无 GPS、无厂商串） |
| `test_deep_directories_can_be_created_written_and_read` | 深目录创建/写入/读取 |
| `test_the_long_path_fallback_is_reachable_even_without_max_path` | **Linux 上强制覆盖回退分支** |
| `test_the_fallback_names_the_format_explicitly` | 直接证明回退分支传了 `format` |
| `test_package_in_a_deep_workspace_contains_its_frames` | 深路径下帧真的进包 |
| `test_partial_reencode_failure_keeps_good_frames_and_separates_counters` | 计数分开，重编码失败不计入预算 |
| `test_all_frames_failing_produces_an_actionable_error` | 全失败消息点名真实原因 |
| `test_no_frames_message_names_reencode_failures_not_budget` | 消息分因（纯函数） |
| `test_reencode_error_description_leaks_no_private_path` | 诊断只记长度不记路径 |

### 平台处理

Windows 真实执行全部长路径用例；Linux 上 4 项 Windows 专属用例带**明确 skip 原因**
（已在 `conftest._EXPECTED_SKIP_REASONS` 白名单），回退分支由强制模拟用例覆盖，
**不静默跳过**。

### 反向验证

移除 `format` → 5 项变红（含 Linux 执行的强制回退两条）；恢复后全绿，无残留标记。

### 数据安全

原片只读、项目目录隔离、EXIF/GPS 清理均保持；修复不改变包结构与条目命名。

---

## TSB-366-R4 视频链路长路径兼容（TSB-366-R4 / R4-F01 / R4-F02）

发布记录：[docs/RELEASE_TSB_366_R4.md](RELEASE_TSB_366_R4.md)
证据目录：[docs/validation/tsb366-r4/](validation/tsb366-r4/)

> **证据口径**：本机是 macOS，**无 MAX_PATH**，所以 35/58/76 矩阵在修复前后
> 都是绿的，不能作为修复证据。缺陷由**契约层**用例（访问是否经过兼容 helper）
> + 反向验证证明；Windows 260 字符端到端行为由 CI 的 windows-latest 承担。

### D1 — `probe_clip()` 存在性与大小

| 产物 | 守护的行为 |
| --- | --- |
| `test_tsb366r4_video_long_paths.py::test_probe_clip_resolves_the_clip_through_the_compat_layer` | 必须用 `safe_is_file`/`safe_stat`，不得回到普通 `is_file()`/`stat()` |
| `::test_probe_clip_reports_real_size_and_digest` | 大小与 SHA-256 等于真实字节 |
| `::test_probe_clip_still_rejects_a_clip_that_really_is_missing` | **反向**：真缺失仍报 `CLIP_UNREADABLE`，兼容层不吞错 |

### D2 — `_analyze_one()` 预算与 VideoPart

| 产物 | 守护的行为 |
| --- | --- |
| `::test_the_budget_and_the_payload_use_the_compat_helpers` | 必须经 `_clip_size_bytes`/`_clip_read_bytes`；不得残留 `clip.path.stat()`/`read_bytes()` |
| `::test_clip_size_helper_reads_the_real_size` | 辅助函数返回真实大小 |
| `::test_clip_read_helper_returns_the_whole_file` | 返回**完整**字节（不截断） |
| `::test_the_clip_helpers_raise_on_a_genuinely_missing_file` | **反向**：真缺失抛错，不伪造 0 字节/空 bytes |

### D3 — `path_occupies_disk()`

| 产物 | 守护的行为 |
| --- | --- |
| `::test_path_occupies_disk_resolves_overlong_paths_before_deciding` | 两条异常分支都要经 `long_path` 复核（只统计可执行代码） |
| `::test_path_occupies_disk_is_true_for_a_real_directory` | 真实目录 → True |
| `::test_path_occupies_disk_is_false_for_an_absent_path` | 真缺失 → False |
| `::test_path_occupies_disk_still_sees_a_dangling_link` | **TSB-346 回归**：悬空链接仍算占用 |

### 总闸

| 产物 | 守护的行为 |
| --- | --- |
| `::test_the_video_chain_no_longer_uses_plain_path_io` | 视频链路不得残留普通 Path I/O（`path.is_file()`/`path.stat()`/`clip.path.stat()`/`clip.path.read_bytes()`）；日后有人改回普通 API 会立刻失败 |

反向验证（见 `docs/validation/tsb366-r4/raw/REVERSE-VERIFY.txt`）：
去掉 D1 ⇒ 2 RED；去掉 D2 ⇒ 2 RED；去掉 D3 ⇒ 1 RED；恢复后 12 passed。

### R4-F01 — README.txt 实际生成文本

| 产物 | 守护的行为 |
| --- | --- |
| `test_tsb366r3_package_deep.py::test_the_readme_contains_a_real_rule_not_a_literal` | 必须有 50 个等号；不得出现字面量 `=" * 5` |
| `::test_the_readme_json_sample_has_clean_double_quotes` | JSON 示例为正常双引号；不得有 `\"` 残留 |
| `::test_the_readme_json_sample_is_valid_json` | 示例抠出后 `json.loads()` 可解析且字段齐全 |

三条都**读取实际生成的文件**，不只断言"函数没抛异常"。
反向验证：恢复两处缺陷 ⇒ 3 RED。

### R4-F02 — Package 深路径集成测试

| 产物 | 守护的行为 |
| --- | --- |
| `test_tsb366r3_package_deep.py::test_package_in_a_deep_workspace_contains_its_frames` | 必须使用 `deep_output_dir`；代表帧 / `segments.json` / ZIP 三处**各自断言 ≥260 字符**；用 `safe_open`/`long_path` 读取；ZIP 用 `long_path` 打开；`segments.json` 真实列出代表帧；帧可用 Pillow 重新解码 |

实测目标长度：`deep_output_dir` 273 → 代表帧 **~368**。
反向验证：改回浅路径 ⇒ 失败于 `segments.json 路径只有 251 字符，未跨过 MAX_PATH`。

### 测试环境稳定性（任务书 §5）

| 产物 | 守护的行为 |
| --- | --- |
| `test_deep_scan_api.py::_make_deep_dir` + `test_scan_320_char_cjk_root` | 层数按 root **实际长度**推导，目标按**最终**路径计算；TEMP 根 35/58/76/120 实测最终长度 336/328/346/328，均 ≥320；目标长度未降低 |

### R4 手动确认项（建议纳入 T01–T25）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| T-R4-01 | 在 `LongPathsEnabled=0` 的 Windows 上，把 TEMP 设成 ≥76 字符后跑视频分析 | 任务进入 `complete_video`；Observation 落库；cleanup 成功 |
| T-R4-02 | 同上环境，把项目 `output_dir` 放到深路径后再生成分析包 | 代表帧出现在 `segments.json` 与 ZIP；帧可打开 |
| T-R4-03 | 打开生成的分析包 README.txt | 分隔线是 50 个等号；JSON 示例无双引号转义残留 |
| T-R4-04 | 导出分析包后删除派生视频再重跑 | 报 `CLIP_UNREADABLE`，不得假报成功 |
| T-R4-05 | 深路径下取消视频任务 | staging 被清理，无残留字节，无跨项目删除 |

---

## TSB-366-R4-R1 视频任务生命周期与 Windows 深路径 E2E

发布记录：[docs/RELEASE_TSB_366_R4_R1.md](RELEASE_TSB_366_R4_R1.md)
证据目录：[docs/validation/tsb366-r4-r1/](validation/tsb366-r4-r1/)

### R1-A：`stats.phase` 终态不可变

| 产物 | 守护的行为 |
| --- | --- |
| `test_tsb366r4r1_job_phase_race.py::test_a3_...` | 终态写入后，迟到的 `running` **不得**把它盖回运行中（CI 失败的正是这一形态） |
| `::test_a3b_...` | 取消终态同样不可被迟到 `running` 覆盖 |
| `::test_a3c_...` | 重复写同一终态幂等，不降级 |
| `::test_a3d_...` | **反向约束**：终态保护不得挡住正常阶段性推进（`starting→running→complete`） |
| `::test_a3e_...` | 接线：视频任务必须经单调保护写 `phase`，不得整体覆盖后把终态盖回 |
| 反向验证 | 移除守卫 ⇒ **2 RED** |

### R1-A：V31 执行权归属

| 产物 | 守护的行为 |
| --- | --- |
| `test_video_e2e_formal_api.py::test_v31_...` | 只由 **API 已调度**的那个执行者跑完；等待其到达终态后才断言；`finished_at` 必须存在、status 必须终态 |
| 反向验证 | 恢复旧"自动 + 手动"双执行者 ⇒ **RED**（`finished_at is None`） |

### R1-B：Windows ≥260 字符派生 MP4 端到端

| 产物 | 守护的行为 |
| --- | --- |
| `test_tsb366r4r1_native_video_longpath_e2e.py::test_a_derived_clip_...` | 在 Windows 读回 `LongPathsEnabled`；断言**实际派生 clip 路径 ≥260**；`safe_*` 可读；重新 `probe_clip()` 校验无音轨/size/SHA/duration/宽高；字节完整；Mock 恰好 1 次且含完整 MP4；原片 SHA/size/mtime 不变；staging 清理干净 |
| `::test_the_compat_helpers_are_what_keep_a_deep_clip_readable` | 反向证据：Windows 上普通 `Path.stat` 必须失败而 `safe_stat` 成功（复现 R4 前缺陷）；POSIX 明确标注不适用 |
| 平台口径 | Windows = 真实 MAX_PATH 证据；POSIX = 仅业务链路证据，二者不互相冒充 |

### 验收漏覆盖修复

| 项 | 说明 |
| --- | --- |
| `tests/test_tsb366r3_package_deep.py` | 原先**不在** CI 强制媒体清单（命中数 0），R3 深路径 Package 回归从未在 CI 真实执行。本轮已补入，与 `TSB-366-R4-R1` 的 E2E 一并强制 |

### R4-R1 手动确认项（建议纳入 T01–T25）

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| T-R41-01 | Windows `LongPathsEnabled=0` 下把项目 `output_dir` 放到深路径后跑视频分析 | 派生 MP4 路径 ≥260 仍能完成分析，Observation 落库，staging 清理干净 |
| T-R41-02 | 任务跑完后立即读取任务状态 | `phase` 为 `complete`/`failed`，**不会**停在 `running` |
| T-R41-03 | 同一任务因异常被重复触发执行 | 不出现重复外发请求、重复 Observation 或终态回退 |
