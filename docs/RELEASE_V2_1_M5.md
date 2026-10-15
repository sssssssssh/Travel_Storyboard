# v2.1 (M5) — 视觉理解与动态复核

基线：`main@de9b5e78a1f6d14e5d8d5a79ff63c2cb40012f82`（拉取后核对，未覆盖任何本地改动）。

本轮按任务书 `Travel_Storyboard_AI视觉理解与动态复核_分阶段开发规划书.md` 执行
TSB-301 → 302 → 303 → 304 → 305 → 306 → 307，只做**素材理解基础能力**；
AI 导演决策（自动选镜、故事排序、自动写入 `EditPlanShot`、自动成片）**明确不做**，
留给后续 M6 单独设计与评审。

---

## 一、真实模型接入（TSB-301）

用户提供的真实端点已接入 `backend/.env`（**该文件被 `.gitignore` 排除，密钥未进入版本库**）：

| 项 | 值 |
| --- | --- |
| 模型 ID | `GLM-5.3-Flash` |
| 端点 | 阿里云 PAI-EAS（OpenAI 兼容，vLLM 后端） |
| 能力实测 | 多图 `image_url` ✅、`response_format=json_object` ✅、`/models` ✅ |
| 错误分类实测 | 错 Key → 401；错模型 ID → 404 `NotFoundError` |

**单次真实调用烟测**（3 张 768px 真实代表帧 → 结构化 JSON）：

```
latency_ms: 6456  | attempts: 1
model: GLM-5.3-Flash | usage: prompt=1431 completion=965 total=2396
PARSED JSON keys: ['scene', 'subjects', 'camera_motion', 'limitations']
```

模型对真实画面的描述经人工核对**准确**（湖面、画舫、木栈道、远处白塔等均与素材相符）。

**一个必须记录的实测发现**：该模型是推理型，`max_tokens` 过小时会把预算耗在
`reasoning` 字段上而让 `content` 返回 `null`。适配器把这种情况报为
`MODEL_INVALID_OUTPUT` 并提示"raise max_tokens"，**不当作成功**（C6）。
若按"HTTP 200 即成功"处理，就会持久化一条空观察。

**隐私相关的现实约束**：该端点是 `http://`（非 HTTPS）公网地址。适配器默认拒绝
明文传输，必须在 `.env` 中显式设置 `TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=1` 才可用。
这意味着**图片与 Bearer 令牌在网络上不加密**，已在 `.env.example` 与代码注释中写明，
需用户知情。生产使用建议换成 HTTPS 端点。

---

## 二、任务、问题与修复

| 任务 | 交付 | 关键约束 |
| --- | --- | --- |
| TSB-301 | 真实模型配置、连通性验证、脱敏检查 | `/api/system/config` 与 `/api/vision/status` 只回传布尔值与模型名，**不回传密钥或 URL** |
| TSB-302 | `services/vision/providers/*`、`capabilities.py`、`errors.py` | 仅对 429/5xx/超时/网络暂态做有界退避并尊重 `Retry-After`；401/403/不支持图像立即失败；取消双检 |
| TSB-303 | `schema.py`：结构化 Schema、提示词、证据校验 | 模型编造的 `segment_id`/`frame_id` 一律拒绝；未知是一等答案 |
| TSB-304 | `segment_semantic_results` 表、`vision_analyze` 任务、4 个 API | AI 结果独立存储，**永不写人工列**；覆盖度如实报告 |
| TSB-305 | `frontend/src/components/VisionPanel.tsx` | 上传前预估、显式同意、逐项进度、来源标注 |
| TSB-306 | `review_candidates.py` | 规则帧差只作为"需核验"提示，**不声称抖动**；单帧镜头标记而非排除 |
| TSB-307 | `motion.py`：时间戳多帧理解 | `method=multiframe`，非视频理解；人工判定重跑不清除 |

### 开发中发现并修复的三处真实缺陷

这三处都不是"测试写错"，而是产品代码在异常路径上的真实缺陷，均由测试先复现再修复：

1. **模型响应完全无法解析时整批中断**
   `_analyze_one` 直接让 `SemanticValidationError` 冒泡，导致一个镜头的畸形响应
   终止整个批次，且**不记录该镜头失败**。修复为按单项失败上报（A13 场景）。
2. **强制重跑被静默计入 `skipped`（伪造成功）**
   版本键唯一约束使强制重跑撞 `IntegrityError`，原实现把它当"已完成"跳过，
   任务显示 `succeeded` 而**实际一个结果都没写**。修复为原地替换同版本行；
   签名变化时保留历史。新增 `test_force_reruns_in_place_without_duplicating`
   与 `test_history_is_kept_when_the_input_signature_changes` 分别守护两条路径。
3. **帧差测量用数据库相对路径，静默失败**
   `Frame.image_path` 存的是**相对数据目录**的路径，直接交给 Pillow 会得到
   `\\?\F:\...\backend\projects\...` 这种不存在的路径，被 `except` 吞掉后
   **所有运动镜头都被判为静止**。修复为经 `resolve_in_project` 解析并做项目边界校验。

### 一处接口契约修正（非缺陷）

`test_job_mutex.py::test_all_documented_types_are_valid` 断言
`VALID_JOB_TYPES == set(HEAVY)`——这是有意设置的守卫，防止新增任务类型时漏登记。
新增 `vision_analyze` 与 `motion_review` 后该用例失败，处理方式是**同步扩展
`HEAVY` 清单**（同时把新类型纳入互斥参数化覆盖），而不是放宽断言。

---

## 三、验收矩阵对照（第 8 节）

| 编号 | 场景 | 结论 | 证据 |
| --- | --- | --- | --- |
| A01 | 缺 API 配置 | ✅ 服务正常、UI 提示离线路线、不发外部请求 | `test_vision_semantics.py::test_job_fails_when_model_not_configured`、`VisionPanel.test.tsx` "unconfigured model" |
| A02 | 有效真实视觉模型 + 脱敏 JPEG | ✅ 返回真实结构化可见内容 | 第一节烟测 + `acceptance-report.json` |
| A03 | 错 Key / 无图片能力 / 错模型 ID | ✅ 各自稳定错误码，不吞成空成功 | `test_vision_provider.py` 参数化分类 + 真实 401/404 实测 |
| A04 | 429 / 5xx / timeout | ✅ 有界重试、尊重 Retry-After、不泄露凭据 | `test_retry_is_bounded`、`test_retry_after_is_honoured`、`test_error_message_redacts_the_api_key` |
| A05 | 伪造 segment_id 和越界时间 | ✅ 拒绝且不改写 DB | `test_fabricated_segment_id_is_rejected` |
| A06 | 多图上限、请求体上限、任务预算 | ✅ 超限拒绝并显示覆盖缺口 | `test_multi_image_beyond_limit_is_refused`、`test_oversize_request_body_is_refused`、`test_budget_is_enforced` |
| A07 | 5～10 个真实镜头批量分析 | ✅ 见第五节 | `acceptance-report.json` |
| A08 | 重试与重启恢复 | ✅ 幂等跳过硬编码键；参数变更可重分析 | `test_rerun_skips_unchanged_shots`、`test_retrim_makes_the_result_stale_and_reanalyzed` |
| A09 | 取消与竞争任务 | ✅ `cancelling`→`cancelled`、409 互斥、无无效发布 | `test_cancel_stops_the_loop`、`test_job_mutex.py`（新类型已纳入参数化） |
| A10 | 单帧镜头 + 多帧镜头 | ✅ 单帧显示"运动不可判定"；补采样才做动态判断 | `test_single_frame_shot_is_flagged_not_dropped` |
| A11 | 静止/平移/快速变化真实样本 | ✅ 有时间戳证据，不把帧差当抖动 | 第六节 + `test_rule_reason_never_asserts_a_shake` |
| A12 | 人工收藏/裁剪/备注、复核请求 | ✅ AI 重跑与强制 detect 后保留 | `test_job_never_modifies_human_fields`、`test_human_verdict_is_stored_and_survives_a_rerun` |
| A13 | 断网 / 限流 / 畸形 JSON | ✅ 逐项失败可见；已完成结果可用 | `test_invalid_model_output_is_a_visible_failure`（含上述缺陷 1 的回归） |
| A14 | 同意/拒绝上传及隐私检查 | ✅ 拒绝不发送；包内无绝对路径 | `test_job_refuses_without_upload_acknowledgement`、`test_uploaded_bytes_carry_no_local_path` |
| A15 | 真实 Mac + FFmpeg + 6 段 4K | ⚠️ **本机为 Windows**，见第七节 | `acceptance-report.json` |
| A16 | 无模型回退 + Windows/Ubuntu CI | ⚠️ 本地已验证；CI 待本轮 HEAD 的运行结论 | 第七节 |

---

## 四、自动化验证

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q` | **654 collected / 0 failed / 0 errors / 1 skipped** |
| M5 强制媒体模式 | `TSB_REQUIRE_FFMPEG=1 pytest <5 个视觉套件> test_ci_skip_guard.py` | **139 passed**，无误判 skip |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **33 passed** |
| 前端 build | `vite build` | 通过 |

> **口径说明**（沿用 v2.0.7 的更正）：`654` 是 pytest 的"收集数"口径。
> 本机 Windows 上 1 个 skip 是平台事实类（`MAX_PATH only constrains Windows` 系列在
> Windows 上反而会真跑，故比 macOS 的 44 少）。**不得**把 654 写成 "654 passed"。

本轮新增用例分布（均为新增文件，未修改既有断言语义）：

| 套件 | 用例数 | 守护的行为 |
| --- | --- | --- |
| `test_vision_provider.py` | 41 | 失败分类、有界重试、能力校验、取消、脱敏 |
| `test_vision_schema.py` | 30 | ID 防伪、闭集归一、未知语义、身份断言过滤、单帧诚实性 |
| `test_vision_semantics.py` | 21 | 抽样、覆盖度、幂等、强制重跑、预算、人工字段不可写 |
| `test_vision_candidates.py` | 10 | 候选来源标注、帧差措辞、单帧不排除 |
| `test_vision_motion.py` | 37 | 采样边界、时间戳防伪、提示词诚实性、真实抽帧、人工判定保留 |

**反向验证**：临时移除适配器的重试守卫（`if True:` 取代可重试判定）后，
`test_transient_500_is_retried_then_succeeds`、`test_retry_is_bounded`、
`test_retry_after_is_honoured`、`test_cancel_during_backoff_stops_retrying`
四项**确实失败**，恢复后全部转绿——证明这些用例不是空断言。

---

## 五、真机验收

见 [`acceptance-report.json`](acceptance-report.json) 与
[`WINDOWS_REAL_FOOTAGE.md`](WINDOWS_REAL_FOOTAGE.md)。

驱动脚本：`backend/tools/acceptance_m5.py`（**测试工具，非产品代码**）。
它通过正式服务层驱动全链路，并强制两项断言：C1（原片字节不变）与
C6（无静默失败），且在 `TRAVELSB_MODEL_MODE != on` 时**拒绝运行**——
验收工具不得替用户绕过隐私开关。

---

## 六、范围声明

**未改动**：
- 镜头检测算法、`ANALYSIS_VERSION`（保持 `2.0.2`，既有分析结果不失效）；
- `direct_analyze` 旧路径的行为（`test_story_direct.py` 29 项保持通过）；
- 离线分析包、`ai_review.py`、复核请求/代理/粗剪 API；
- 既有 `Asset`/`Segment`/`Frame`/`ReviewRequest`/`EditPlanShot` 任何行。

**未删除或改写**：任何原片、任何既有测试、任何既有文档数字。

**未写入人工状态**：AI 结果只落 `segment_semantic_results` 与
`segment_motion_reviews` 两张新表；`user_rating`、`story_role`、
`start_sec`/`end_sec`、`is_trimmed` 对本模块是只读输入。

**明确的隐私默认值**：`TRAVELSB_MODEL_MODE=off`。未显式开启时不下发任何画面，
`vision_analyze`/`motion_review` 任务直接以 `MODEL_UPLOAD_DENIED` 失败，
而不是静默跳过。

---

## 七、尚未完成（诚实列出）

1. **Windows 实机 8～10 段多维度素材门禁仍未完成。**
   本轮使用 6 段 DJI 4K HEVC（横屏、含音轨），**无竖屏、无无音轨、无 HDR**，
   段数少于门禁要求的 8～10 段。结论措辞仍为
   **「代码级整改通过，真实素材发布门禁未完成」**。
2. **CI 结论待本轮最终 HEAD 的运行**。此前绿色 run 对应的是 `a28820d`，
   与本轮 HEAD 无关，不得互相冒充。
3. **原生视频理解未实现**。M5.2 按任务书要求只做"时间戳多帧"，
   不等于连续视频分析；`supports_native_video` 默认 `false`。
4. **音频语义完全未分析**。动态复核结果中 `audio_analyzed` 恒为 `false`，
   即使素材带音轨。
5. **60+ 段 / 200GB 全量性能未测**，不依据样本耗时外推。
6. **模型端点为明文 `http://`**，令牌与图片在网络上不加密（见第一节）。
7. **人脸/车牌未脱敏**。EXIF/GPS 去除是强制的，但不能遮蔽画面内容本身；
   上传前仍需用户自行确认画面敏感度。
8. **`analysis_json` 的标签为闭集**，模型给出的集外词汇会被归一为
   `other`/`unknown`，可能损失少量信息——这是可用性优先的取舍。
