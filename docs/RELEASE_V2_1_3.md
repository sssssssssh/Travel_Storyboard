# v2.1.3 (M5.3) — 原生视频理解接入（批次 A：TSB-325 / TSB-326）

基线：`main@664239ba16769e19c6e3564728953a39e213d4f4`（执行前核对 HEAD 一致）。

**本轮范围：只做批次 A。** 按任务书 §5 与 §7 的明确指令，
TSB-327～332（短片派生、时间 Schema、Job/预算、A/B 评价、前端审阅、发布证据）
**本轮不执行**，留给下一批。

> **最终结论（按任务书 §6 的三选一措辞）**：
> **① `端点不支持，视频模式未启用` —— 成立，且已查明具体原因。**
>
> 端点健康（纯文本 200、图片 200），但视频请求被 **HTTP 400** 拒绝，
> vLLM 原始报错为：
> `At most 0 video(s) may be provided in one prompt. Set --limit-mm-per-prompt
> to increase this limit. (parameter=video)`。
>
> 也就是说：**vLLM 认得 `video_url` 这个类型，但该部署把每 prompt 的视频上限设成了 0。**
> 这是**服务端启动参数**问题，不是客户端协议写错，也**不能**据此断言模型不支持视频。
> `video_native` 通道保持禁用；图片/多帧通路不受影响。
> 证据见 [CAPABILITY_REPORT.json](validation/v2.1.3/CAPABILITY_REPORT.json) 与
> [PROTOCOL_EXAMPLES.md](validation/v2.1.3/PROTOCOL_EXAMPLES.md)。
>
> **要让视频通道可用**：服务端加 `--limit-mm-per-prompt video=1` 并重启部署，
> 然后重跑探测。在此之前仍**没有任何证据**表明该模型能有效理解视频内容。


---

## 一、任务、问题与交付

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
| 后端全量 | `pytest -q --junitxml` | **911 collected / 0 failed / 0 errors / 1 skipped** |
| 强制媒体模式（11 个套件） | `TSB_REQUIRE_FFMPEG=1 pytest` | **273 passed / 0 failed / 0 skipped** |
| skip 守卫自检 | `tools/probe_skip_guard.py` | `VERDICT: PASS` |
| 反向验证 | `tools/reverse_verify_native_video.py` | `VERDICT: PASS`（4/4 守卫被覆盖） |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **48 passed** |
| 前端 build | `vite build` | 通过 |

**数字口径**（AGENTS.md §3.5）：`911` = v2.1.2 基线 **828** + 本轮新增 **83**
（capability 21 + probe 32 + provider 30）。**911 是收集数，不得写成 "911 passed"。**
1 个 skip 是平台事实类（`link creation is a platform fact`，
`test_staging_ownership.py`）。

### 一个预先存在的间歇性失败（如实记录，本轮未修）

全量运行中观察到 **1 次**（约数次运行中 1 次）：

```
tests/test_vision_staging_ownership.py::test_success_registry_is_bounded_and_failure_evidence_is_retained
```

**这不是本轮引入的**，证据链：

- 在**未包含本轮任何新测试**的基线上同样复现（`tests=828 failures=1`，同一个用例）；
- 在 pristine `664239b` 上连跑 3 次全绿，说明是**间歇性**而非必现；
- 单独运行该用例连跑 3 次全部通过。

**根因（已定位）**：该用例断言模块级全局的**绝对**状态——
`assert len(cleanup._OWNERS) == baseline` 与 `assert len(cleanup._CLEANED) <= 4`。
而 `_OWNERS` / `_CLEANED` 是 `staging_cleanup` 的**进程级全局**
（`staging_cleanup.py:51` / `:53`），在整个 pytest 会话中跨用例累积；
当会话顺序让其他用例留下条目时，这两个断言就可能不成立。
属于**测试隔离**问题，不是产品缺陷。

**处理**：本轮**未修改**该用例或 `staging_cleanup`——不在批次 A 范围内，
且改动清理所有权代码风险高。已在此如实记录，建议后续单独任务修复
（让该用例用 `monkeypatch` 隔离 `_OWNERS`/`_CLEANED`，或进入用例时清空并恢复）。
**本轮结论不依赖"全量一次全绿"**：全量运行多次均为 0 failures
（加入拒绝原因分类的 3 项用例后为 `911 tests / 0 failures`）。


CI 清单同步：三个新套件已加入 `.github/workflows/ci.yml` 的**强制媒体作业**
（该作业设 `TSB_REQUIRE_FFMPEG=1`，静默 skip 即失败）。
三者均无 `skipif`，`probe_skip_guard.py` 复核通过。

---

## 四、范围声明

**未改动**：
- 图片路径（`semantic.py`）与多帧路径（`motion.py`）的行为；
- `ANALYSIS_VERSION`、`SegmentSemanticResult`、`SegmentMotionReview`、
  `SegmentMotionVerdict` 的读写语义与历史；
- `direct_analyze` 旧链路；
- 任何 `Asset` / `Segment` / `Frame` / `ReviewRequest` / `EditPlanShot` 数据行；
- 前端代码（本轮前端只跑四项检查，未修改）。

**未删除或改写**：任何原片、任何既有测试断言、任何既有文档数字。
`model_supports_native_video` 默认值**保持 `false`**。

**未启用任何真实视频通道**：`video_native` 在能力为 `unverified` 时不可发送，
由 `capability_gate()` 在渲染任何媒体之前拒绝。

**未写入人工状态**：`user_rating`、`story_role`、`start_sec`/`end_sec`、
`is_trimmed` 仍是只读输入。

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
   在拿到真实正例之前，**不得**进入 TSB-327 之后的真实视频链路，
   也不得宣传任何视频理解能力。
2. **视频被接受后能否给出有用时间信息，完全未知。** 被 limit 挡住，无任何样本。
   **当前没有任何证据表明 GLM-5.3-Flash 能有效理解视频内容。**
2. **TSB-327～332 全部未执行**（批次 B/C/D）。短片派生、时间映射、
   独立版本化结果表、`video_analyze` 任务、A/B 评价、前端面板均未开工。
3. **协议仅覆盖 `video_url_data_uri`（内联 Base64 MP4）。**
   文件 ID / `multipart/form-data` 未实现——任务书要求先核对私有上传与删除语义，
   未通过前不启用该通道。
4. **上限字段全部保持 `unknown`**：`max_bytes` / `max_duration_sec` /
   `accepts_audio` 未做边界压测，故不填猜测值。
5. **视频容器仅接受 ISO-BMFF（MP4/MOV）**，`video/quicktime` 也不在白名单中
   （fail closed）；是否应放宽，需端点能力确认后再决定。
6. **本机 CI 未运行。** 本地模拟通过不等于 GitHub Actions 七作业通过；
   需要推送后核对**同一 HEAD** 的运行结论。
7. **未做 200GB 全量性能验证**，不依据样本外推。
8. **`test_vision_staging_ownership.py` 的间歇性失败未修**（详见第三节）——
   预先存在的测试隔离问题：该用例断言 `staging_cleanup` 进程级全局
   `_OWNERS`/`_CLEANED` 的绝对状态，在全量会话顺序下可能不成立。
   建议另立任务修复，本轮未动。


---

## 六、下一批是否可执行的判定

按任务书 §5 批次 A 的停止条件：

> 「端点不支持、外发不被授权：停止真实接入，仅保留可复用适配层」

本次属于**第一种情形：端点当前不接受视频**（原因是服务端 `--limit-mm-per-prompt`
把视频上限设为 0）。因此：

- **不要**开始批次 B 的真实视频联调；
- **可以**继续在 Mock 下完善适配层与测试脚手架；
- **要放行批次 B 的真实联调**，先做服务端配置变更（`--limit-mm-per-prompt video=1`
  + 重启），重跑探测拿到 `verified_native_video` 或
  `video_accepted_sampling_unknown`，再决定。

