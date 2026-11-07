# TSB-366-R4-R1 验证证据索引

**开工基线：** `afeefe3b6789abc94c98234b7f092afef6f2b300`（= `origin/main`，工作区干净）
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg+ffprobe 9.0.2

> **本机是 macOS，没有 MAX_PATH。** 本目录所有"绿色"只证明业务链路与无回归，
> **不**证明 Windows 长路径行为；Windows 证据只见于 CI 的 windows-latest。

| 文件 | 内容 |
| --- | --- |
| `BASELINE.md` | 阶段 0：基线核对、CI 三轮对照、受控实验、机制证据（F1–F6） |
| `ROOT_CAUSE.md` | 根因：三步机制、定性、修复、R1-B 缺口与漏覆盖 |
| `raw/RED-before-fix.xml` | R1-A 修复前的 junit 原件（2 RED） |
| `full-backend.xml` | 后端全量（1738 / 0 / 0 / 48） |
| `media-run-1..3.xml` | 媒体清单 72 文件连续 3 次（1299 / 0 / 0 / 44） |
| `ci-run-<id>.txt` | 最终 CI 运行记录 |

## 关键数字

| 项 | 值 |
| --- | --- |
| R1-A 修复前 RED | **2** |
| R1-A 修复后 | **5/5** |
| 反向（移除 phase 守卫） | **2 RED** |
| 反向（恢复旧 V31 双执行者） | **1 RED**（`finished_at is None`） |
| V31 单例 ×25（CI 条件） | **25/25** |
| 后端全量 | **1738 / 0 / 0 / 48** |
| 媒体清单 ×3 | **1299 / 0 / 0 / 44** |
| 本机派生 clip 路径长度 | **394**（≥260） |
| CI 强制清单文件数 | **72**（含新 E2E 与 R3 package deep） |

## 目标用例 node id

```text
tests/test_tsb366r4r1_job_phase_race.py::test_a3_a_late_running_write_cannot_overwrite_a_terminal_phase
tests/test_tsb366r4r1_job_phase_race.py::test_a3b_a_cancelled_job_also_keeps_its_terminal_phase
tests/test_tsb366r4r1_job_phase_race.py::test_a3c_the_terminal_phase_is_idempotent
tests/test_tsb366r4r1_job_phase_race.py::test_a3d_non_terminal_transitions_still_progress
tests/test_tsb366r4r1_job_phase_race.py::test_a3e_the_job_stats_writer_is_phase_monotonic
tests/test_tsb366r4r1_native_video_longpath_e2e.py::test_a_derived_clip_beyond_max_path_survives_the_whole_native_video_chain
tests/test_tsb366r4r1_native_video_longpath_e2e.py::test_the_compat_helpers_are_what_keep_a_deep_clip_readable
tests/test_video_e2e_formal_api.py::test_v31_the_full_chain_produces_an_observation_and_cleans_up
tests/test_tsb366r3_package_deep.py::test_package_in_a_deep_workspace_contains_its_frames
```

## 无法在本机取得证据的项

1. Windows `LongPathsEnabled=0` 下派生 MP4 ≥260 的真实行为 —— 只能在 CI。
2. CI 那次 V31 失败的两个执行者**逐指令**交错点 —— 本机未复现原始交错；
   受控交错已证明该机制足以造成完全相同的症状。
