# TSB-366-R4 发布记录：视频分析链路 Windows 长路径兼容专项

**开工基线：** `main@5890c10e7c33b70dfd66e776673528389b72ea65`（与 `origin/main` 逐字符一致，工作区干净）
**本批 commit：** 见本文件末尾（独立 TSB-366-R4 commit）
**机器（本机测量）：** macOS 15.7.7 arm64 / Python 3.12.14 / pytest 9.1.1 /
FFmpeg+ffprobe 9.0.2 / Pillow 12.3.0 / OpenCV 5.0.0

> **本机是 macOS，没有 Windows MAX_PATH 限制。** 所有本机"绿色"只证明
> "没有引入回归"，**不证明** Windows 行为。真实 Windows 260 字符端到端验证由 CI 的
> `Backend (Python 3.x) — windows-latest` 与 `Required FFmpeg (windows-latest)` 承担。
> 按 AGENTS.md C8，本记录不把 "CI 7/7" 写成 "本机所有路径环境均通过"。

---

## 一、结论

**「R4 代码级整改通过；Windows 长路径端到端验证依赖 CI，本机不可复现。」**

| 项目 | 状态 |
| --- | --- |
| 35/58/76 字符 TEMP 矩阵（本机） | 全绿 —— 但**不能**作为修复证据（macOS 无 MAX_PATH） |
| 视频链路三处缺陷（D1/D2/D3） | 已定位 + 已修复 + 契约层回归测试 + 反向验证 |
| R4-F01 README 格式 | 已修复，测试断言**实际生成文件文本** |
| R4-F02 Package 深路径测试 | 已修复，实际目标路径 **~368 字符**，测试内断言 ≥260 |
| 320 字符测试不再依赖 TEMP 长度 | 已修复（35/58/76/120 全部 ≥320） |
| 后端全量（三个根长度） | **1731 / 0 failed / 0 error / 48 skipped** |
| CI 媒体作业（70 文件，无 `.env`） | **1291 / 0 / 0 / 44** |
| 前端四项 | typecheck ✅ / lint ✅ / **123 passed** / build ✅ |
| Windows 真机 260 字符端到端 | **未在本机验证**（依赖 CI） |

---

## 二、阶段 0：基线与环境

| 项 | 值 |
| --- | --- |
| HEAD / origin/main | `5890c10` / `5890c10`（**同步**） |
| 分支 | `main` |
| 工作区 | clean |
| OS | macOS 15.7.7 arm64 (24G720) |
| Python / pytest | 3.12.14 / 9.1.1 |
| FFmpeg / FFprobe | 9.0.2 / 9.0.2 |
| Pillow / OpenCV | 12.3.0 / 5.0.0 |
| TMPDIR | 48 字符（已脱敏） |
| `LongPathsEnabled` | **N/A** —— macOS 无此策略 |

**本机环境修复（与业务无关）：** root `.venv` 起初**未安装 `alembic`**
（`requirements-dev.txt:11` 已声明 `alembic>=1.13`）。这使 11 个 schema/迁移用例
失败，且在**干净 HEAD `5890c10`** 上同样失败，与本轮改动无关。已安装后全绿。

---

## 三、阶段 1：35/58/76 字符 TEMP 根复现矩阵

用 `--basetemp` 构造**精确长度**根（实测生效，`tmp_path` 实际 117/123 字符）。

| TEMP 根 | `test_video_provider_wiring.py` | `test_video_e2e_formal_api.py` | `test_tsb366r4_video_long_paths.py` |
| ---: | --- | --- | --- |
| 35 | 8 passed | 9 passed | 12 passed |
| 58 | 8 passed | 9 passed | 12 passed |
| 76 | 8 passed | 9 passed | 12 passed |
| 90 | 8 passed | 9 passed | 12 passed |

### 重要：本表**不是**修复证据

修复前这四项在 macOS 上**同样全绿**。原因：macOS 没有 MAX_PATH，
`Path.is_file()`/`stat()`/`read_bytes()` 对任意长度路径都正常。
任务书所述"76 字符失败"是 **Windows 特有**现象。

**所以我没有假装在 macOS 上复现了它。** 缺陷改由下面两层定位：

---

## 四、`used == []` 的准确失败节点（任务书 §2.3）

`used == []` 只证明 `complete_video()` 未被调用，**不指认具体函数**。
逐环节定位结果：

| # | 环节 | 缺陷 | 依据 |
| --- | --- | --- | --- |
| 1 | `create_owned_staging()` | 无 | 已用 `long_path`（`_lstat`/`_listdir`/`pinned_directory`） |
| 2 | `derive_clip()` FFmpeg | 无 | 原生程序不受 MAX_PATH 限制 |
| 3 | FFmpeg 输出验证 | 无 | `_verify_expected()` **已内置** `long_path` 回退 |
| **4a** | **`probe_clip()` `is_file()`** | **D1** | 普通 API |
| 4b | `probe_clip()` → ffprobe | 无 | `probe()` 已用 `long_path` |
| **5** | **`probe_clip()` `stat()`** | **D1** | 普通 API（`_sha256` 已用 `safe_open`，正确） |
| **6a** | **`_analyze_one()` 预算 `stat()`** | **D2** | 普通 API |
| **6b** | **`_analyze_one()` `read_bytes()`** | **D2** | 普通 API |
| 7 | 授权检查 | 无 | 不涉文件系统 |
| 8–10 | `complete_video()` / HTTP / Observation | **未到达** | 被 D1/D2 拦截 |
| **11** | **`path_occupies_disk()`** | **D3** | 普通 `os.lstat`，靠吞异常凑巧方向正确 |

**准确失败节点：`probe_clip()` 的 `is_file()`/`stat()`（D1）
与 `_analyze_one()` 的 `stat()`/`read_bytes()`（D2），全部位于
`complete_video()` 之前。D1 先命中**（`probe_clip` 在 `derive_clip` 内）。

### 为什么是 `used == []` 而不是明确失败

- D1 抛 `VideoClipError(CLIP_UNREADABLE, "derived clip was not created")`
  —— 消息误导：文件**真实存在**，普通 API 看不见。
- D2 抛 `FileNotFoundError` —— **不是** `ModelError`，不经预算分支，
  直接终结该 segment。

两者都在 Provider 之前 ⇒ spy 永远空列表。
**也解释了"单独复跑就过"**：换台 TEMP 更短的机器，路径没到 260。

### 完整异常（修复前实测）

```
tests/test_tsb366r4_video_long_paths.py::test_probe_clip_survives_a_path_beyond_max_path
  VideoClipError: derived clip was not created
```

（该文件用 long_path 写入、`os.path.isfile(long_path)` 为真、ffprobe 可读，
 却因普通 `path.is_file()` 判定"未创建"。）

---

## 五、修复（最小改动）

### D1 — `backend/app/services/vision/video_clip.py::probe_clip()`

```diff
-    if not path.is_file():
+    if not safe_is_file(path):
         raise VideoClipError(CLIP_UNREADABLE, "derived clip was not created")
-    size = path.stat().st_size
+    size = safe_stat(path).st_size
```

### D2 — `backend/app/services/vision/video_job.py::_analyze_one()`

```diff
-    plan.budget.note_derived([(clip.duration_sec, clip.path.stat().st_size)])
+    plan.budget.note_derived([(clip.duration_sec, _clip_size_bytes(clip.path))])
-            data=clip.path.read_bytes(),
+            data=_clip_read_bytes(clip.path),
```

新增两个辅助函数（内部 `safe_stat` / `safe_open`），并导入
`long_path, safe_open, safe_stat`。

### D3 — `backend/app/services/vision/video_job.py::path_occupies_disk()`

两条异常分支都先用 `long_path(target)` 复核再判定；`lstat`（不跟随链接）
的 lexists 语义保持不变。

### 契约保持（任务书 §6.2 / §6.3）

- `probe_clip()`：派生文件真实存在、ffprobe 可读、无音轨、实测 duration、
  大小 == 真实字节、SHA-256 == 真实字节、HDR/VFR/rotation 保持、源素材不变。
- `_analyze_one()`：预算按真实派生字节、Provider 收到**完整** MP4、
  仍为 `video_native`、授权仍在上传前、每次 HTTP attempt 计费规则不变、
  失败有稳定错误码、Observation 只在真实成功后持久化。
- 真缺失仍抛错（不伪造 0 字节 / 空 bytes）。

**未改动**：视频模型调用协议、授权、预算规则、数据安全逻辑、staging 所有权。

---

## 六、R4-F01：README.txt 输出格式

### 修复前（实测输出）

```
Travel Storyboard — Round 1 analysis package
=" * 5                                    <- 7 个字符的字面量
    \"schema_version\": \"2.0\",            <- 多余反斜杠
```

### 修复后

```
Travel Storyboard — Round 1 analysis package
==================================================    <- 50 个等号
When you reply with review requests, return a JSON object:
  {
    "schema_version": "2.0",
    "requests": [
      {"asset_id": "...", "segment_id": "...",
```

### 新增测试（3 项，均读**实际生成文件**）

| 用例 | 断言 |
| --- | --- |
| `test_the_readme_contains_a_real_rule_not_a_literal` | 含 `"=" * 50`；不含 `'=" * 5'` |
| `test_the_readme_json_sample_has_clean_double_quotes` | 含正常双引号；不含 `\"` |
| `test_the_readme_json_sample_is_valid_json` | 示例抠出后 `json.loads()` 成功，字段齐全 |

反向验证：恢复两处缺陷 ⇒ **3 RED**。

---

## 七、R4-F02：Package 深路径集成测试

| | 修复前 | 修复后 |
| --- | --- | --- |
| 是否用 `deep_output_dir` | ❌ 定义了但**未使用** | ✅ 使用 |
| output_dir 长度 | 41–43 | **273** |
| 代表帧目标路径 | **~136** | **~368** |
| `segments.json` 路径 | 251（反向验证实测） | ≥260 |
| ZIP 路径 | 未断言 | ≥260 |
| 长度断言 | **无** | 代表帧 / segments.json / ZIP 三处均断言 ≥260 |
| 读取方式 | 部分普通 API | `safe_open` / `long_path` |
| ZIP 打开 | 普通 | `long_path(zip_path)` |

反向验证：改回浅路径 ⇒ 失败于
`segments.json 路径只有 251 字符，未跨过 MAX_PATH`。

---

## 八、阶段 5：320 字符测试不再依赖 TEMP 长度

`_make_deep_dir()` 按 root **实际长度**推导层数，并在即将越过目标时用一个
补齐的组件精确落点。`test_scan_320_char_cjk_root` 改为按**最终**路径
（含 `二级 目录/it's here` 后缀）计算目标。

实测最终长度：

| TEMP 根 | 最终路径长度 | ≥320 |
| ---: | ---: | --- |
| 35 | 336 | ✅ |
| 58 | 328 | ✅ |
| 76 | 346 | ✅ |
| 120 | 328 | ✅ |

目标长度**未降低**（仍 ≥320），未新增 skip/xfail。
该用例带 `@requires_windows`，在 macOS 属**登记在册**的平台事实 skip。

---

## 九、回归矩阵

### 9.1 视频链路

| 要求 | 结果 |
| --- | --- |
| R07 Provider 被实例化 | ✅ |
| R07 `complete_video()` 被调用 | ✅ |
| R07b Mock 只收到预期视频请求 | ✅ |
| R10 Observation 正确持久化 | ✅ |
| V31 API → worker → mock → Observation → cleanup | ✅ |
| V34 历史与人工判定不被覆盖 | ✅ |
| R07c 原片 SHA/size/mtime 不变 | ✅ |
| 取消/失败/cleanup/budget 无回归 | ✅ |

### 9.2 重复（§9.4）

| 要求 | 结果 |
| --- | --- |
| R07/R10/V31/V34 @ 根 76 连续 10 次 | **10/10** |
| `test_video_provider_wiring.py` 连续 3 次 | **3/3**（8 tests） |
| `test_video_e2e_formal_api.py` 连续 3 次 | **3/3**（9 tests） |
| Package 深路径连续 5 次 | **5/5**（6 tests） |

### 9.3 全量

| 环境 | 结果 |
| --- | --- |
| 后端全量 @ TEMP 根 35 | **1731 / 0 / 0 / 48** |
| 后端全量 @ TEMP 根 58 | **1731 / 0 / 0 / 48** |
| 后端全量 @ TEMP 根 76 | **1731 / 0 / 0 / 48** |
| CI 媒体作业（70 文件，**无 `.env`**，`TSB_REQUIRE_FFMPEG=1`） | **1291 / 0 / 0 / 44** |
| 前端 typecheck / lint / test / build | ✅ ✅ **123 passed** ✅ |

三个根长度结果**完全一致** ⇒ 业务覆盖不再随 TEMP 长度变化。

---

## 十、范围声明

**本轮改动**

- `backend/app/services/vision/video_clip.py`（D1）
- `backend/app/services/vision/video_job.py`（D2、D3）
- `backend/app/services/review_package.py`（R4-F01）
- `backend/tests/test_tsb366r4_video_long_paths.py`（**新增**，12 项）
- `backend/tests/test_tsb366r3_package_deep.py`（R4-F02 + 3 项 README 测试）
- `backend/tests/test_deep_scan_api.py`（§5 独立性）
- `.github/workflows/ci.yml`（登记新套件；**并修正我引入的行内注释**）
- 本记录与 `docs/validation/tsb366-r4/`

**本轮明确未做**

- 未修改 TSB-366-R2 的 loopback 代理修复与 HTTP 408 分类。
- 未修改 TSB-367 操作协议、Storyboard 状态机或 Alembic。
- 未把 native video 改成 multiframe / image_only 规避失败。
- 未关闭视频授权、预算、能力验证或上传确认。
- 未放宽源文件身份校验。
- 未绕过 staging 所有权与安全清理。
- 未把普通 `Path` 访问错误吞成"文件不存在"。
- 未缩短 TEMP、缩短测试路径或减少目录层级来掩盖缺陷。
- 未删除/skip/xfail 任何现有测试；未降低现有断言；未使用宽泛
  `pytest.raises(Exception)`。
- 未修改原片；未把真实视频上传外部服务。
- 未做与本轮无关的架构重构。

**过程中自查发现并修正的两处自身问题（已记录）**

1. **我在 `ci.yml` 折叠块里加了行内注释**，YAML 会把它与前后的行拼成一条命令，
   导致**该注释之后的所有文件**（含 `test_video_provider_wiring.py`）被吞掉。
   仓库自带的 `test_b10_media_suite_is_explicitly_forced_in_ci` 立即抓到。
   已把说明移到块**上方**的 YAML 注释，并验证 70/70 文件全部命中、块内 0 个 `#`。
2. **D3 的断言一度把 docstring 里的 `long_path` 也计入**，导致反向验证漏判；
   已改为只统计可执行代码，重跑后能正确转红。

---

## 十一、未完成项 / 风险（诚实列出）

1. **Windows 260 字符端到端未在本机验证。** 本机是 macOS，无法复现 MAX_PATH。
   D1/D2/D3 的证据是**契约层**（访问是否经过兼容 helper）+ 反向验证，
   不是 Windows 实测。真实 Windows 行为须以 CI 的 windows-latest 作业为准。
2. **早期"注入式模拟 Win32 短路径 API"方案已废弃。** POSIX 上不存在 `\\?\` 别名，
   模拟要么自相矛盾，要么需要改写生产代码的 `os.name` 门禁 ——
   那是"为让测试变绿而改代码"，明确禁止。故改用契约层断言。
3. **`shortPath`/`LongPathsEnabled` 关闭时的行为**仍只在 CI 覆盖。
4. 真实旅行素材 8–10 段端到端验收、60+/200GB 全量性能 **仍未执行**（历史遗留）。
5. 视频模型真实端点能力（G5）与真实 A/B（G6）**仍为独立未验证门禁**。
6. `test_scan_320_char_cjk_root` 等 `@requires_windows` 用例在 macOS 上 skip，
   属登记平台事实，其 Windows 执行依赖 CI。
