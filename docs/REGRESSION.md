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
| T03 | 裁剪后重复检测 | 标注一个镜头（收窄入出点 + 收藏）→ 强制重新检测 ≥2 次 | 镜头 id、收藏、备注、裁剪区间全部保留；`detected_*` 不变 |
| T04 | 增量检测 | 不改素材再点一次「检测」（不勾选强制） | 任务 `skipped ≥ 1`，磁盘帧数不增长 |
| T05 | 帧缓存回收 | 连续强制检测 3 次 | `<项目工作区>/frames/` 文件数稳定，不随次数增长 |
| T06 | 中途杀进程 | 检测进行中直接结束后端进程 → 重启 | 任务被重新派发（`resume_interrupted_jobs`），不产生重复镜头 |
| T07 | 并发保护 | 检测运行中再点扫描/打包 | 第二次请求返回 409 并给出正在运行的任务编号 |
| T08 | 取消 | 打包/代理运行中点取消 | 任务状态 `cancelled`，已产出的临时文件不污染正式目录；**进程应立即停止**（见 T20） |
| T09 | 部分失败可见 | 让某个素材不可读后跑打包 | 任务 `partial_failed`，前端列出失败条目，成功部分仍可用 |
| T10 | 授权边界 | 直接请求 `/api/system/token`、`/api/projects/<pid>/media?path=token.txt` | 404 / 400，绝不返回内容 |
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
| T23 | 取消后暂存与历史产物 | 取消粗剪/代理/检测后检查工作区 | 本任务 `staging-*`（或 `staging/<asset>/<token>`、`proxies/staging/<token>`）已清空；**历史**已发布的 `roughcut.mp4` / 代理 / 收藏 / 裁剪 / 计划引用原样保留 |
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
