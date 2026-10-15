# v2.1 (M5.1) — 视觉上传授权边界与旧链路帧泄漏修复

基线：`main@306b1fd`（`fix(TSB-312): 新旧视觉入口统一上传授权与拒绝路径`）。
本轮完成 **TSB-312 的收尾接线** 与 **TSB-313**，两者都在 M5 的视觉上传边界上。

> 结论措辞：**代码级整改通过，真实素材发布门禁未完成**（见第五节）。
> 本轮未新增任何真机素材覆盖，不得据此宣称发布门禁完成。

---

## 一、问题、修复、验收

### TSB-312 — 旧 `/story/analyze` 曾仅凭"配了 Key"就上传画面

**问题（原缺陷）**：`POST /api/projects/{id}/story/analyze` 只调用
`model_client.is_configured()`，即"base URL + Key 存在"就发送画面。
而新的 `vision_analyze` / `motion_review` 任务要求
`TRAVELSB_MODEL_MODE=on` **且** `upload_acknowledged=true`。
同一个产品里两条入口的隐私规则**互相矛盾**：配了一个 Key 就等于默认同意上传。
这违反 C5（默认不上传、需显式同意）。

**修复**：把规则收敛到唯一判定点 `services/vision/authorization.py`。

| 位置 | 改动 |
| --- | --- |
| `api/story.py::analyze_story` | 改为调用 `authorize_upload(...)`，拒绝时回 403/422 + 稳定错误码 + 迁移提示 |
| `story_planner.direct_analyze` | 新增仅关键字参数 `authorization`，**首调与 JSON 重试都转发** |
| `model_client.chat` | 新增仅关键字参数 `authorization`，入口即 `require_context()` |
| `providers/base.py::VisionRequest` | 携带 `authorization`，provider 在发请求前校验 |

`AuthorizedModelContext` 是**逐次调用**的令牌（构造时无进程级标志位），
因此不存在"某次同意后全局放行"的状态。

**验收**：
- `test_vision_authorization.py` 14 项全通过（含旧入口无请求体即 403、
  mode=off 拒绝、两个入口共用同一边界、被拒时零外部调用）；
- `test_vision_compat.py` 中 3 项专测接线：令牌附在出站请求上、
  `direct_analyze` 两处调用都转发、路由先授权且不在路由内自查配置。

### TSB-313 — 每次旧链路调用都在磁盘上留下一份用户画面

**问题（原缺陷）**：`model_client._flatten()` 遇到 inline `data:` URI 时，
把解码后的字节写到 `<tempdir>/tsb-vision-<uuid>.jpg`，再把**路径**交给 provider。
该文件**从不删除**，成功、报错、超时、取消四条路径都泄漏。

**修复前实测证据**（`docs/validation/v2.1-m5-1/tsb313-before.txt`，修订 `306b1fd`）：

```
temp files created by _flatten: 1
    C:\Users\...\Temp\tsb-vision-57b095bf610d4185aedb1658027a9db3.jpg  (203 bytes)
ImagePart.path = 'C:\\Users\\...\\Temp\\tsb-vision-....jpg'
ImagePart has .data attribute: False
The file is never deleted: exists after _flatten returns = True
```

这不只是卫生问题：画面副本落到了**项目工作区之外**的系统临时目录，
不受 C1/清理白名单管辖，任何能读 `%TEMP%` 的进程都能读到用户素材。

**修复**：让载荷以字节形式在内存中传输，彻底不落盘。

| 位置 | 改动 |
| --- | --- |
| `providers/base.py::ImagePart` | 新增互斥载体 `data: bytes` + `mime`；`path`/`data` 二选一，否则构造即失败 |
| `model_client.decode_data_uri` | 内存内解码，按稳定错误码分类失败，并在**解码前**按字符串长度设上限 |
| `model_client._flatten` | data URI → `ImagePart(data=...)`；普通路径仍按路径透传 |
| `openai_compatible.validate_inline_image` | 用 magic bytes 判定真实类型；伪造 MIME、非图片、空、超限一律 `MODEL_BAD_REQUEST` |
| 旧 `_materialize_data_uri` | **删除**（不是留着不用） |

**验收**：
- `test_chat_leaves_no_frame_copy_in_the_temp_directory` 对临时目录做**前后计数**断言。
  只断言"调用成功"看不见这个缺陷——修复前后都会成功；
- `test_the_image_survives_as_in_memory_bytes` 确保"不落盘"没有退化成"丢图"（C6）；
- `test_an_error_path_also_leaves_nothing_behind` 单独覆盖失败路径。

---

## 二、先复现、再转绿

按 AGENTS.md §3.1 的顺序执行，两个方向都留了证据：

1. **修复前**：新套件对回退到 `306b1fd` 的实现运行，
   26 项中 **22 项失败**，首个失败为
   `TypeError: chat() got an unexpected keyword argument 'authorization'`；
   另有 `ImagePart` 无 `data`、`sniff_mime` 不存在等。
2. **修复后**：同一套件 **26 passed**。

反向验证说明这些用例不是空断言：它们确实能区分修复前后的可观测差异
（临时目录是否新增文件、出站请求是否携带令牌、伪造 MIME 是否被拒）。

---

## 三、验证结果

环境：Windows 开发机，Python 3.13.7（`backend/.venv`），FFmpeg 9.0.2（项目自带），
Node 18.19.0。**`LongPathsEnabled=0`**。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q --junitxml` | **702 collected / 0 failed / 0 errors / 1 skipped** |
| 强制媒体模式（视觉套件 + skip 守卫） | `TSB_REQUIRE_FFMPEG=1 pytest <8 个套件>` | **190 passed / 0 failed / 0 skipped** |
| skip 守卫自检 | `python tools/probe_skip_guard.py` | `VERDICT: PASS`，未登记原因不会被误判 |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **33 passed** |
| 前端 build | `vite build` | 通过 |

> **口径说明**：`702` 是 pytest **收集数**口径。基线 `306b1fd` 的提交信息记录为
> **676 collected**，本轮新增 `test_vision_compat.py` **26** 项，676 + 26 = 702，
> 与实测一致。
> 注意 `RELEASE_V2_1_M5.md` 中的 `659` 是**更早**（TSB-308）的口径，
> 不适用于 `306b1fd`——该文件第九行声明的基线是 `de9b5e7`。
> 1 个 skip 是平台事实类。**不得**把它写成 "702 passed"。

本轮新增用例：

| 套件 | 用例数 | 守护的行为 |
| --- | --- | --- |
| `test_vision_compat.py` | 26 | 临时目录零新增、内存字节送达、载荷与 MIME 校验、`ImagePart` 双载体互斥、授权接线 |

媒体作业 190 项的构成（实测 collect 数，逐套件相加吻合）：
`test_vision_provider` 43 + `test_vision_motion` 40 + `test_vision_schema` 30 +
`test_vision_compat` 26 + `test_vision_semantics` 24 + `test_vision_authorization` 14 +
`test_vision_candidates` 10 + `test_ci_skip_guard` 3 = **190**。
（`RELEASE_V2_1_M5.md` 记录的 147 是本轮新增套件之前的旧口径。）

CI 清单同步：`tests/test_vision_compat.py` 已加入 `.github/workflows/ci.yml`
的**强制媒体作业**清单（该作业设 `TSB_REQUIRE_FFMPEG=1`，静默 skip 即失败）。
该文件不含任何 `skipif`，`probe_skip_guard.py` 复核通过。

---

## 四、范围声明

**未改动**：
- 镜头检测算法与 `ANALYSIS_VERSION`（仍为 `2.0.2`，既有分析结果不失效）；
- `direct_analyze` 的业务行为——抽样、提示词、覆盖率、校验、落库语义全部不变，
  仅新增授权透传（`test_story_direct.py` 全部保持通过）；
- 离线分析包、`ai_review.py`、复核请求 / 代理 / 粗剪 API；
- 前端任何代码（本轮前端只做四项检查，未修改）；
- 任何 `Asset` / `Segment` / `Frame` / `ReviewRequest` / `EditPlanShot` 数据行。

**未删除或改写**：任何原片、任何既有测试断言、任何既有文档数字。
唯一删除的是 `model_client._materialize_data_uri`（造成泄漏的函数本身），
并新增一条用例断言它不再存在。

**未写入人工状态**：`user_rating`、`story_role`、`start_sec`/`end_sec`、
`is_trimmed` 对本轮改动仍是只读输入。

**隐私默认值未变**：`TRAVELSB_MODEL_MODE=off`。

---

## 五、尚未完成（诚实列出）

1. **真实素材发布门禁仍未完成。** 本轮**没有**新增任何真机素材运行；
   Windows 实机 8～10 段多维度（竖屏 / 无音轨 / HDR）素材端到端验收**未执行**。
   TSB-312/313 的结论仅到代码级与自动化验证层。
2. **TSB-313 的泄漏只在本机 Windows 实测过。** Linux/macOS 的 `%TEMP%` 语义不同
   （通常是 `/tmp`），未在该平台复现过修复前的泄漏行为。
3. **`MAX_DATA_URI_CHARS`（18,000,000 字符）与 `MAX_INLINE_IMAGE_BYTES`
   （12,000,000 字节）为保守取值**，未针对真实端点做上限压测，
   也未确认该端点自身对请求体的上限；两者可能偏松或偏紧。
4. **magic bytes 校验只覆盖 JPEG/PNG/WEBP。** 其他合法格式（如 HEIC、AVIF）
   会被判为"不是图片"而拒绝——是刻意收紧的取舍，但未做真实素材矩阵验证。
5. **多帧/多图场景下每张图都会重新 base64 编码**，旧链路未做编码复用，
   大请求体下有额外内存峰值，未做压力测试。
6. **CI 结论待本轮最终 HEAD 的运行**；此前绿色 run 对应更早的 commit，
   与本轮 HEAD 无关，不得互相冒充。
7. 原生视频理解、音频语义、60+ 段 / 200GB 全量性能**均未测**，
   不依据样本耗时外推。

---

## 六、证据文件

| 文件 | 内容 |
| --- | --- |
| [`docs/validation/v2.1-m5-1/tsb313-before.txt`](validation/v2.1-m5-1/tsb313-before.txt) | 修复前实测：`_flatten` 每次产生 1 个临时帧副本且不删除 |
| [`docs/validation/v2.1-m5-1/tsb313-new-suite.txt`](validation/v2.1-m5-1/tsb313-new-suite.txt) | 新套件修复后 26 项通过 |
| [`docs/validation/v2.1-m5-1/local-backend.txt`](validation/v2.1-m5-1/local-backend.txt) | 后端全量与强制媒体模式的原始输出 |
