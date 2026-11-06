# TSB-366-R4 验证证据索引

**开工基线：** `main@5890c10e7c33b70dfd66e776673528389b72ea65`（与 `origin/main` 同步，工作区干净）
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / pytest 9.1.1 / FFmpeg+ffprobe 9.0.2 /
Pillow 12.3.0 / OpenCV 5.0.0
**重要：** 本机是 **macOS，没有 Windows MAX_PATH 限制**。所有"绿色"都只代表
"在本机没有回归"，不代表 Windows 行为。Windows 行为由 CI 的
`Backend (*) — windows-latest` 与 `Required FFmpeg (windows-latest)` 承担。

---

## 一、证据文件

| 文件 | 内容 |
| --- | --- |
| `raw/BASELINE.txt` | 阶段 0 基线核对、环境版本、缺陷定位（修复前） |
| `raw/RED-before-fix.txt` | 修复前的确定性 RED 证据与完整异常 |
| `raw/RED-before-fix.xml` | 对应 junitxml 原件 |
| `raw/REVERSE-VERIFY.txt` | 反向验证：去掉每处修复 -> 对应用例转红 |
| `ROOT_CAUSE.md` | 根因分析：准确失败节点、每阶段定位表、修复前后对比 |
| `../RELEASE_TSB_366_R4.md` | 发布记录与完整交付矩阵 |

---

## 二、TEMP 根长度矩阵（目标用例）

用 `--basetemp` 构造**精确长度**的 TEMP 根（实测 basetemp 生效，
`tmp_path` 实际长度 117/123 字符）。

| TEMP 根长度 | `test_video_provider_wiring.py` | `test_video_e2e_formal_api.py` | `test_tsb366r4_video_long_paths.py` |
| ---: | --- | --- | --- |
| 35 | 8 passed | 9 passed | 12 passed |
| 58 | 8 passed | 9 passed | 12 passed |
| 76 | 8 passed | 9 passed | 12 passed |
| 90 | 8 passed | 9 passed | 12 passed |

**注意：** 修复前这四项在 macOS 上**也**全绿 —— 因为 macOS 没有 MAX_PATH。
所以本表证明的是"没有回归"，**不是**"缺陷已修复"。缺陷的证据见
`raw/RED-before-fix.txt` 与 `ROOT_CAUSE.md`。

---

## 三、重复验证（任务书 §9.4）

| 要求 | 环境 | 结果 |
| --- | --- | --- |
| R07/R10/V31/V34 连续 10 次 | TEMP 根 76 | **10/10 clean**（junit：4 tests / 0 fail / 0 error 每次） |
| `test_video_provider_wiring.py` 连续 3 次 | TEMP 根 76 | **3/3 clean**（8 tests） |
| `test_video_e2e_formal_api.py` 连续 3 次 | TEMP 根 76 | **3/3 clean**（9 tests） |
| Package 深路径连续 5 次 | 本机默认 TEMP | **5/5 clean**（6 tests） |

---

## 四、目标用例的完整 node id（任务书 §4.2 要求）

```text
tests/test_video_provider_wiring.py::test_r07_the_job_uses_the_native_video_provider
tests/test_video_provider_wiring.py::test_r07b_exactly_one_silent_video_is_sent
tests/test_video_provider_wiring.py::test_r07c_the_source_file_is_never_modified
tests/test_video_provider_wiring.py::test_r10_a_temporal_event_is_persisted_at_source_absolute_seconds
tests/test_video_e2e_formal_api.py::test_v31_the_full_chain_produces_an_observation_and_cleans_up
tests/test_video_e2e_formal_api.py::test_v34_a_rerun_does_not_destroy_the_previous_observation
tests/test_video_e2e_formal_api.py::test_v34b_a_rerun_marks_exactly_one_current_row
tests/test_tsb366r4_video_long_paths.py::test_probe_clip_resolves_the_clip_through_the_compat_layer
tests/test_tsb366r4_video_long_paths.py::test_probe_clip_reports_real_size_and_digest
tests/test_tsb366r4_video_long_paths.py::test_probe_clip_still_rejects_a_clip_that_really_is_missing
tests/test_tsb366r4_video_long_paths.py::test_the_budget_and_the_payload_use_the_compat_helpers
tests/test_tsb366r4_video_long_paths.py::test_clip_size_helper_reads_the_real_size
tests/test_tsb366r4_video_long_paths.py::test_clip_read_helper_returns_the_whole_file
tests/test_tsb366r4_video_long_paths.py::test_the_clip_helpers_raise_on_a_genuinely_missing_file
tests/test_tsb366r4_video_long_paths.py::test_path_occupies_disk_resolves_overlong_paths_before_deciding
tests/test_tsb366r4_video_long_paths.py::test_path_occupies_disk_is_true_for_a_real_directory
tests/test_tsb366r4_video_long_paths.py::test_path_occupies_disk_is_false_for_an_absent_path
tests/test_tsb366r4_video_long_paths.py::test_path_occupies_disk_still_sees_a_dangling_link
tests/test_tsb366r4_video_long_paths.py::test_the_video_chain_no_longer_uses_plain_path_io
```

---

## 五、R4-F02 深路径测试的实际目标路径长度

`deep_output_dir` 在**真实运行时**的长度（实测，非估算）：

| 对象 | 长度 | 是否 ≥ 260 |
| --- | ---: | --- |
| `deep_output_dir` | 273 | ✅（本机默认 TEMP 下） |
| `<output_dir>/<pid>/packages/<ver>/` | ~333 | ✅ |
| 代表帧 `<...>/frames/<id>.jpg` | **~368** | ✅ |
| 修复前的 `tmp_path / ("out" + "z"*24)` 下的代表帧 | **~136** | ❌ 从未跨过 |
| 修复前的 `segments.json`（反向验证实测） | **251** | ❌ |

反向验证：把 output_dir 改回浅路径，
`test_package_in_a_deep_workspace_contains_its_frames` 立即失败于
`segments.json 路径只有 251 字符，未跨过 MAX_PATH`。

---

## 六、本机环境与已知限制

1. **`alembic` 起初未安装在 root `.venv`**（`requirements-dev.txt` 已声明
   `alembic>=1.13`）。这导致 11 个与 schema/迁移相关的用例在本机失败，
   在**干净 HEAD `5890c10`** 上同样失败 —— 属环境缺口，与本轮改动无关。
   安装后已消除，全量矩阵据此重跑。
2. 本机为 macOS，**无法**复现 Windows MAX_PATH 端到端失败。
3. `test_scan_320_char_cjk_root` 等用例带 `@requires_windows`，
   在 macOS 上是**登记在册的平台事实 skip**，不是本轮新增。
