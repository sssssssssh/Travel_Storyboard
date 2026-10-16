# v2.1.3 (M5.3) — 原生视频理解接入与 A/B 验证

基线：`main@664239ba16769e19c6e3564728953a39e213d4f4`（执行前核对 HEAD 一致）。

**本轮范围**：批次 A（TSB-325/326）+ **批次 B（TSB-327/328/329）**
+ **批次 C（TSB-330/331）** + **批次 D（TSB-332）**。

> **关于真实联调**：视频通道**仍未启用**，因为端点拒绝视频（原因见下）。
> 因此批次 B/C 的**代码与 Mock 测试全部完成**，而**真实视频联调与 A/B 评价
> 仍标记为待验证**。按任务书 §7，未获真实正例前不得进入真实视频链路。

---

## 零、批次 B / C / D 交付摘要（TSB-327 ～ TSB-332）

| 任务 | 交付 | 新增用例 |
| --- | --- | --- |
| **TSB-327** | `video_clip.py`：镜头短片派生、相对→绝对时间映射、源指纹校验、元数据剔除 | 38 |
| **TSB-328** | `video_schema.py` + `video_analysis.py` + `SegmentVideoObservation` 表 + 5 个 API | 37 + 25 + 22 |
| **TSB-329** | `video_job.py`：`video_analyze` 任务（能力门禁/预算/回退/取消/权限） | 31 |
| **TSB-330** | `video_ab.py` + `evaluate_video_ab.py` + `AB_PROTOCOL.md` | 31 |
| **TSB-331** | `VideoUnderstandingPanel.tsx`（三路证据并列 + 不确定性与来源标注） | 17（前端） |
| **TSB-332** | CI 媒体清单、回归清单、发布记录、`PROTECTION_REPORT.json` | — |

**总计新增后端用例 267 项**（另将 `video_analyze` 纳入 `HEAVY` 参数化，+1）。

### 开发中发现并修正的真实缺陷

这一轮有四处**由测试或真实调用暴露**的问题，不是"测试写错"：

1. **能力校验放错了层**（TSB-329）。
   初版把校验放在 worker 内。**通过真实 API 调用**才暴露后果：请求返回 202、
   任务入队并占住项目锁，之后才在 worker 里以 `VIDEO_CAPABILITY_UNVERIFIED` 失败 ——
   用户看到的是一个永远不可能成功的排队任务。任务书 V03 要求**请求即拒绝**。
   已改到路由层，并**不创建任何 Job 行**。
2. **判定顺序掩盖了项目忙**（TSB-329）。
   能力拒绝若先于互斥检查求值，会答 422 而隐藏"项目正忙"，导致调用方对着一个
   它从未获知的锁重试。现改为互斥优先返回 409。
3. **"跑了但没检出"与"根本没跑"被混为一谈**（TSB-330）。
   首版对无预测的臂返回 `recall=0.0`，读起来是"跑了且什么都没找到"；
   而真实语义是"该通道从未运行"。已区分：`0.0` = 跑了没检出，`null` = 没跑。
4. **一个未登记的 skipif 被 CI 守卫拦下**（TSB-327）。
   HDR 用例最初用 `pytest.skip("this FFmpeg build did not preserve...")`，
   在 `TSB_REQUIRE_FFMPEG=1` 下**被 skip guard 正确拦截** —— 该原因不是平台事实，
   不该允许它悄悄移除 COLOR_UNVERIFIED 路径的唯一测试。改为直接断言分类逻辑。

### 两处刻意的"拒绝声明"

- **元数据剔除不等于人脸/车牌脱敏**：代码与 UI 都写明这一点。
- **`encoder=Lavf<version>` 去不掉**：实测 FFmpeg 9.0.2 下
  `-map_metadata -1` / `+bitexact` / `-metadata encoder=` 均无效。它标识编码器版本
  而非用户，因此**分开报告**而不是谎称已剥离（谎报会造成假阴性、掩盖真问题）。
  `creation_time` / `make` / `model` / `location` 与**自定义 `handler_name`** 已确认去除。

---

## 一、任务、问题与交付（批次 A）

### TSB-325 [P0] 端点能力探测与协议契约

**问题**：仓库里 `model_supports_native_video: bool = False` 只是一个配置声明，
**没有任何东西能证明或否证端点是否接受视频**。历史文档提到 vLLM 的 `video_url`
示例，但任务书明确：**不得用公开示例替代本端点实测**。
在没有能力证据前就把视频通道建起来，等于把"配了一个 flag"当成"模型支持视频"。

**修复/交付**：

| 文件 | 内容 |
| --- | --- |
| `app/services/vision/video_capabilities.py` | `VideoCapabilities` 契约 + 四态枚举 + **协议注册表** |
| `backend/tools/probe_video_capability.py` | 探测工具，默认 dry-run，四道闸门 |
| `backend/tests/test_native_video_capability.py` | 21 项契约测试 |
| `backend/tests/test_native_video_probe.py` | 29 项分类与闸门测试 |
| `docs/validation/v2.1.3/CAPABILITY_REPORT.json` | 脱敏探测报告（不含 Key / URL / 路径） |
| `docs/validation/v2.1.3/PROTOCOL_EXAMPLES.md` | 实际验证的请求结构 + 未验证项清单 |

三条设计规则，都是为了防止"看起来知道"：

1. **未知是值，不是零。** 未测量的上限保持 `None`，并显式列入
   `unknown_fields`。填入一个"看起来合理"的 10MB/30s 会让下游预算校验
   依据一个从未观测的数字，且显得权威。
2. **协议是注册的，不是嗅探的。** 按 `model_name` 里是否含 "glm" 来选协议，
   正是任务书点名的反模式。
3. **只有 `record_verification()` 能产生可发送状态**，且
   `verified_native_video` 必须给出 `protocol` 与 `verification_scope`——
   "调用成功了"不构成证据。

四道闸门（`probe_video_capability.py`）：

1. 默认 dry-run，只打印拟用协议/字节估算/风险；
2. `--execute` **还必须**同时给 `--i-authorize-real-upload`，否则退出码 2；
3. 默认发送合成静音图样；指向真实素材需 `--clip`，报告会如实标注来源；
4. 经产品自身的 `authorize_upload()` 取上下文，探测**不能绕过**产品规则。

### TSB-326 [P1] 可插拔视频 Provider 与协议实现

**问题**：`VisionRequest.images: list[ImagePart]` 只有图片语义。
把视频塞进 `ImagePart` 会让图片代码路径**意外**传输视频（反之亦然）而无类型错误拦截；
且视频携带时轴、时长与源坐标映射，与静态帧不是同一种东西。

**修复/交付**：

| 文件 | 内容 |
| --- | --- |
| `providers/base.py` | 新增 `VideoPart` / `VideoRequest` / `VideoResponse` / `VideoProvider` |
| `providers/native_video.py` | `NativeVideoVisionProvider` + `capability_gate` + 载荷校验 |
| `backend/tests/test_native_video_provider.py` | 30 项测试（含"零出站"断言） |

三个在**任何网络调用之前**发生的拒绝：

1. **无授权上下文不发送** —— 与图片路径共用同一 chokepoint（TSB-312）；
2. **无已验证能力不发送** —— 环境里 `model_supports_native_video=true` 是**意图**不是证据；
3. **协议未注册 / MIME 不在白名单 / 内容与声明不符 / 超限不发送** —— 按注册表 fail closed。

SSRF：本适配器只发**内联**载荷，服务端不会替我们取 URL。
`fetches_remote=True` 的协议被显式拒绝，而不是半实现一个 host 白名单。

**关于取消的诚实语义**：请求在途时远端可能已解码并计费。我们停止等待并拒绝发布结果，
但**不声称已避免费用**——`CANCELLED` 的消息里写明 `remote_cost_uncertain`。

---

## 二、先复现、再转绿（含反向验证）

### 2.1 探测的真实结果（本次唯一真机证据）

端点状态在两次探测之间发生了变化，两次都记录如下——**保留失败那次**，
因为它恰好证明了对照请求的价值。

**第一次（端点异常时）**：

| 请求 | 结果 |
| --- | --- |
| 文本 + 视频（合成 48,829 字节） | HTTP **503** `no healthy upstream`，2,109 ms |
| **纯文本对照**（无图无视频） | HTTP **503** `no healthy upstream` |

对照组同样 503，因此当时的结论只能是 **`unverified`**。
若不做对照，就会把一次网关故障误报成"端点不支持视频"——一个**凭空捏造的否定结论**。
这个对照已写进工具（`control_probe`）与分类器，并有专门用例守护：

- `test_a_dead_endpoint_cannot_be_reported_as_unsupported`
- `test_a_control_500_also_poisons_the_video_verdict`
- `test_a_healthy_control_lets_a_video_rejection_stand`

**第二次（端点恢复正常后，本次最终结论）**：

| 请求 | 结果 |
| --- | --- |
| 纯文本对照 | **HTTP 200** |
| 图片输入 | **HTTP 200**，回答 `Red`（正确） |
| 文本 + 视频（`video_url`） | **HTTP 400** — `At most 0 video(s) may be provided in one prompt. Set --limit-mm-per-prompt to increase this limit. (parameter=video)` |
| 视频用 `type=video` 形状（对照） | **HTTP 400** — `Unsupported chat content part type: 'video'` |

两个视频报错放在一起才有意义：`type=video` 是**类型不存在**，
而 `video_url` 是**类型被识别、但数量上限为 0**。
因此结论是 `unsupported`，**原因是服务端配置**，不是协议写错、也不是模型不支持。

**必须记录的事实**：`--execute` 真的向公网端点发送了 48,829 字节。
内容为合成测试图样（FFmpeg `testsrc`，320×240、10fps、3s、`-an` 无音轨），
**不含任何用户素材**，但这仍是一次真实外发，不应被描述为"仅本地测试"。

### 2.2 反向验证（任务书 §3.2 强制要求）

`backend/tools/reverse_verify_native_video.py` 逐个削弱守卫并确认测试转红：

```
baseline (all guards present): 30 passed
  OK   protocol-registry gate (accept any protocol name)
         -> FAILED test_a_fetching_protocol_is_refused_as_an_ssrf_surface
  OK   payload validation (skip content sniffing)
         -> FAILED test_a_non_video_payload_sends_nothing
  OK   authorization chokepoint
         -> FAILED test_missing_authorization_sends_nothing
  OK   capability_gate body
         -> FAILED test_capability_gate_refuses_sampling_unknown
VERDICT: PASS - every guard is covered by a failing test
```

脚本在每次变异后恢复原文件，并在结束时断言无变异残留。

---

## 三、验证结果

环境：Windows，Python 3.13.7（`backend/.venv`），FFmpeg 9.0.2（项目自带），
Node 18.19.0，`LongPathsEnabled=0`。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q --junitxml` | **1096 collected / 0 failed / 0 errors / 1 skipped** |
| 强制媒体模式（完整 ci.yml 清单） | `TSB_REQUIRE_FFMPEG=1 pytest` | **457 passed / 0 failed / 0 skipped** |
| skip 守卫自检 | `tools/probe_skip_guard.py` | `VERDICT: PASS` |
| 反向验证（TSB-326） | `tools/reverse_verify_native_video.py` | `VERDICT: PASS`（4/4 守卫被覆盖） |
| 反向验证（TSB-327） | `tools/reverse_verify_video_clip.py` | `VERDICT: PASS`（5/5 守卫被覆盖） |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **65 passed** |
| 前端 build | `vite build` | 通过 |

**数字口径**（AGENTS.md §3.5）：
`1096` = v2.1.2 基线 **828** + 本轮新增 **267**
（capability 21 + probe 32 + provider 30 + clip 38 + schema 37 +
observation 25 + job 31 + api 22 + ab 31）+ `HEAVY` 参数化扩展 **1**。
**1096 是收集数，不得写成 "1096 passed"。**
1 个 skip 是平台事实类（`link creation is a platform fact`，
`test_staging_ownership.py`）。

强制媒体作业清单已加入 9 个新套件（`test_video_*.py` 六项 +
`test_native_video_*.py` 三项），实测 457 项 **0 skip**。

### 反向验证的覆盖（两个工具）

`reverse_verify_native_video.py`（TSB-326）：协议注册闸门、载荷校验、
授权 chokepoint、`capability_gate` —— 逐个削弱均使测试转红。

`reverse_verify_video_clip.py`（TSB-327）：源变化检查、边界夹取、
负值拒绝、非有限值拒绝、无声强制 —— **首轮发现 2 个守卫未被覆盖**
（负值检查、无声强制），补测后 5/5 通过。这正是反向验证的价值所在。

### 一个预先存在的间歇性问题（如实记录，本轮未修）

全量运行中**偶发**出现以下两类现象，**均不影响退出码与用例结果**：

1. `tests/test_vision_staging_ownership.py::test_success_registry_is_bounded_and_failure_evidence_is_retained`
   （约数次运行中 1 次失败）；
2. 运行末尾的 `StateConvergenceError` / `cannot schedule new futures after
   interpreter shutdown` 噪声。

**证据链**：

- 第 1 类在**未包含本轮任何新测试**的基线上同样复现（`tests=828 failures=1`，
  同一个用例），在 pristine 上连跑 3 次全绿 —— 属**间歇性**；
- 第 2 类在 pristine 树上同样出现；
- 但同时观察到：加入本轮的 `test_video_api.py`（会启动**真实异步任务**）
  后，第 2 类噪声出现频率**上升**。

**根因（已定位）**：`staging_cleanup._OWNERS` / `_CLEANED` 是**进程级全局**
（`staging_cleanup.py:51` / `:53`），在整个 pytest 会话中跨用例累积；
该用例断言的是绝对状态（`== baseline`、`<= 4`），所以会话顺序会影响结果。
属于**测试隔离**与**共享执行器**问题，不是产品缺陷。

**处理**：本轮**未修改**该用例或 `staging_cleanup`。已在此如实记录为
**已知风险**，建议后续单独任务修复（用 `monkeypatch` 隔离全局状态）。
**本轮结论不依赖"全量一次全绿"**：多次完整运行均为
`1096 collected / 0 failed`。


---

## 四、范围声明

**未改动**：
- 图片路径（`semantic.py`）与多帧路径（`motion.py`）的**行为**；
- `ANALYSIS_VERSION`、`SegmentSemanticResult`、`SegmentMotionReview`、
  `SegmentMotionVerdict` 的读写语义与历史；
- `direct_analyze` 旧链路；
- 任何 `Asset` / `Segment` / `Frame` / `ReviewRequest` / `EditPlanShot` **数据行**；
- 原片与素材目录：只读，`PROTECTION_REPORT.json` 记录了 6 段 4.76 GB 的
  size / mtime / 采样 SHA-256 作为事后比对基线。

**为接入新任务类型而扩展的既有集合**（有意的最小改动，且同步了守卫用例）：
- `job_service.VALID_JOB_TYPES` 与 `runner.EXCLUSIVE_JOB_TYPES` 加入
  `video_analyze`；
- `test_job_mutex.HEAVY` 同步扩展（该用例是**有意设置的守卫**，
  防止新增任务类型漏登记 —— 因此扩展清单而不是放宽断言）；
- `models.py` **新增** `SegmentVideoObservation` 表，**未**对既有表做任何 ALTER。

**未删除或改写**：任何原片、任何既有测试断言、任何既有文档数字。
`model_supports_native_video` 默认值**保持 `false`**。

**未启用任何真实视频通道**：`video_native` 在能力不可发送时被拒绝，
且拒绝发生在**派生任何媒体之前**（`preflight`）与**入队之前**（路由层）。

**未写入人工状态**：`user_rating`、`story_role`、`start_sec`/`end_sec`、
`is_trimmed` 仍是只读输入；A/B 的事件候选只作展示，需人工确认。

**未新增任何自动剪辑决策**：本轮**不做** M6（不自动评分、不重排
`EditPlanShot`），与任务书 §1.3 一致。

---

## 五、尚未完成（诚实列出）

1. **端点不接受视频（服务端配置所致），视频通道未启用。**
   实测报错为 vLLM 的 per-prompt 视频上限 0。要解除，需服务端加
   `--limit-mm-per-prompt video=1` 并**重启部署**，然后重跑探测：
   ```powershell
   cd backend
   .\.venv\Scripts\python.exe tools\probe_video_capability.py `
       --execute --i-authorize-real-upload `
       --json ..\docs\validation\v2.1.3\CAPABILITY_REPORT.json --redact
   ```
   **注意**：这只改变"能否发送"，不证明模型理解质量——那需要 TSB-330 的 A/B。
2. **视频被接受后能否给出有用时间信息，完全未知。** 被 limit 挡住，无任何样本。
   **当前没有任何证据表明 GLM-5.3-Flash 能有效理解视频内容。**
3. **真实视频联调与 A/B 评价未执行。** 批次 B/C 的**代码与 Mock 测试全部完成**，
   但没有一条真实视频经过 `video_analyze` 全链路。
   `AB_RESULTS.json` 的 `status` 为 `not_annotated`（且 arm C 被端点阻塞），
   **所有指标为 null**。按任务书 §5，A/B 必须标 `blocked_by_endpoint`，
   **绝不能用 Mock 结果替代 C 组真实分数**。
4. **真实 8～10 段多维度素材门禁未执行**（竖屏/无音轨/HDR），
   本轮**无任何新增真机运行**。
5. **协议仅覆盖 `video_url_data_uri`（内联 Base64 MP4）。**
   文件 ID / `multipart/form-data` 未实现——任务书要求先核对私有上传与删除语义，
   未通过前不启用该通道。
6. **上限字段全部保持 `unknown`**：`max_bytes` / `max_duration_sec` /
   `accepts_audio` 未做边界压测，故不填猜测值。
7. **视频容器仅接受 ISO-BMFF（MP4/MOV）**，`video/quicktime` 也不在白名单中
   （fail closed）；是否应放宽，需端点能力确认后再决定。
8. **HDR 只做标记不做转换**：源为 PQ/HLG 时标 `COLOR_UNVERIFIED`，
   **不**声称色调映射正确。
9. **本机 CI 未运行。** 本地模拟通过不等于 GitHub Actions 七作业通过；
   需要推送后核对**同一 HEAD** 的运行结论。
10. **未做 200GB 全量性能验证**，不依据样本外推。
11. **共享全局状态导致的间歇噪声未修**（详见第三节）——
    预先存在的测试隔离问题（`staging_cleanup` 进程级全局 + 共享执行器），
    本轮的 `test_video_api.py` 会提高其出现频率。建议另立任务修复，本轮未动。
12. **A/B 的人工盲评未做。** 没有真人标注，因此
    `AB_PROTOCOL.md` 中预注册的采纳阈值**未被判定**，不得声称视频更优。

---

## 六、最终结论与放行判定

按任务书 §6.2 的三选一措辞：

> **① `端点不支持，视频模式未启用`**

原因已查明并留证：该 PAI-EAS 部署的 vLLM 启动参数把每 prompt 视频上限设为 0
（`--limit-mm-per-prompt`），视频请求被 HTTP 400 拒绝，
而纯文本与图片请求均 200 —— 端点健康，问题在配置。

**必须与以下事实分开陈述**（AGENTS.md 第六节）：

- **端点软件栈具备视频能力** ≠ **模型能有效理解视频内容**；
- **代码级整改通过** ≠ **真实素材发布门禁通过**；
- **Mock/合成素材通过** ≠ **真机验收通过**。

因此本轮结论措辞为：

> **代码级开发完成（批次 A～D），真实视频联调与 A/B 发布门禁未完成。**

### 放行下一阶段的前置条件

要做真实视频联调，**必须先**完成服务端配置变更并重跑探测，取得
`verified_native_video` 或 `video_accepted_sampling_unknown`。
在此之前：

- **不要**宣传任何视频理解能力；
- **不要**把 `video_native` 提升为首选模式；
- **不要**依据 Mock 结果给出 A/B 结论。

---

## 七、使用说明（面向使用者，无需理解协议细节）

### 现在的状态

视频模式是**默认关闭的实验功能**，而且**当前不可用** ——
服务端还没打开开关。界面上的"开始视频分析"会在提交前被拒绝，
并直接告诉你原因和处理方式。**图片与多帧分析完全不受影响，照常可用。**

### 想用视频时

1. 打开项目 → AI 导演页 → "视频理解（M5.3·实验）"面板；
2. 勾选**本次**上传同意（这一项每轮都要重新勾，不会沿用上次）；
3. 点"开始视频分析"。

若按钮提交后被拒绝，面板会显示能力状态与处理方式：
- **要真正启用**：请让部署方在 vLLM 启动参数加
  `--limit-mm-per-prompt video=1` 并重启服务；
- **想立刻继续干活**：改用 `mode=multiframe`（多帧动态分析），
  它不需要视频能力，现在就能跑。

### 怎么知道花了多少

面板与任务详情会显示：请求模式 vs 实际模式、处理镜头数、
**实际发送的短片秒数与字节数**、以及模型返回的 token 用量（若服务端提供）。
**注意**：图片按 token 计费、视频还额外发送秒数与字节，
两者**不可直接比较**，报告里也这样标注。

### 怎么关闭

- 全局：`TRAVELSB_MODEL_MODE=off`（默认值）——关闭后视频与图片都不外发；
- 单次：不勾选上传同意即可，按钮会保持禁用。

### 回退是怎么回事

如果服务端不支持视频，而请求里带了"允许回退"，任务会**改用多帧分析**，
并在界面上明确写出：

> 已回退：VIDEO_CAPABILITY_UNSUPPORTED。**这是回退后的多帧分析，不是视频分析。**

**绝不会**把多帧结果当成视频结果展示 —— 这是代码与测试共同保证的
（`test_a_fallback_never_claims_video`）。

### 结果怎么看

面板把三种读数的**真实输入**并排显示：抽帧语义（1 张代表帧）/
多帧动态（5 帧带时间戳）/ 视频观察（派生短片）。
视频事件同时给出**短片内相对秒**与**源片绝对秒**，
置信度标注为"未校准"。以下信息一定会显示，不会被绿色状态盖住：

- 音频**未**分析（派生短片是静音的）；
- 色彩未验证（源为 HDR 且未做可靠色调映射）；
- 被丢弃的事件及其原因；
- 未覆盖的镜头数（"这不是已分析全库，只是子集"）。

---

## 八、证据文件与复现命令

| 文件 | 内容 |
| --- | --- |
| [`validation/v2.1.3/CAPABILITY_REPORT.json`](validation/v2.1.3/CAPABILITY_REPORT.json) | 脱敏能力探测（无 Key / URL / 路径） |
| [`validation/v2.1.3/PROTOCOL_EXAMPLES.md`](validation/v2.1.3/PROTOCOL_EXAMPLES.md) | 实际验证的请求结构 + 未验证项清单 |
| [`validation/v2.1.3/PROTECTION_REPORT.json`](validation/v2.1.3/PROTECTION_REPORT.json) | 原片与生产库的保护快照（6 段 4.76 GB + 14 张表行数） |
| [`validation/v2.1.3/AB_PROTOCOL.md`](validation/v2.1.3/AB_PROTOCOL.md) | A/B 预注册方案与采纳阈值 |
| [`validation/v2.1.3/AB_RESULTS.json`](validation/v2.1.3/AB_RESULTS.json) | A/B 当前结果（`not_annotated`，**所有指标 null**） |
| [`validation/v2.1.3/local-validation.txt`](validation/v2.1.3/local-validation.txt) | 本机验证原始输出 |

复现全部自动化证据：

```powershell
cd backend
# 能力探测（默认 dry-run；真实执行需两道显式闸门）
.\.venv\Scripts\python.exe tools\probe_video_capability.py

# 保护快照（只读原片与生产库）
.\.venv\Scripts\python.exe tools\protection_report.py `
    --json ..\docs\validation\v2.1.3\PROTECTION_REPORT.json

# 反向验证：逐个削弱守卫，确认测试转红
.\.venv\Scripts\python.exe tools\reverse_verify_native_video.py
.\.venv\Scripts\python.exe tools\reverse_verify_video_clip.py

# A/B（无标注时不出数）
.\.venv\Scripts\python.exe tools\evaluate_video_ab.py --project <id>

# 全量与强制媒体模式
.\.venv\Scripts\python.exe -m pytest -q
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe tools\probe_skip_guard.py
```


