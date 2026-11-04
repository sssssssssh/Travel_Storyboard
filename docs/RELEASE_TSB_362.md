# TSB-362 — 真实视频模型能力探测与端到端验证（阶段 A + B）

| 字段 | 内容 |
|---|---|
| 任务编号 | TSB-362（P0，M5.3 G5 真实端点能力门禁） |
| 冻结基线 | `main@0da77a19725a6af5963672a84d6f6464d4be7ac8`（核对一致，未 reset/覆盖） |
| 产品版本 | `2.1.2`（未升版） |
| 本轮范围 | **阶段 A（只读核对 + dry-run）**、**阶段 B（真实端点诊断）**、**附带修复 R3** |
| 未执行 | 阶段 C（真实旅行短片 + 正式 Job/DB/UI E2E）、G6 A/B |
| 证据目录 | [docs/validation/tsb362/](validation/tsb362/) |

> **结论措辞（AGENTS.md C8）**：**端点已恢复可用，原生视频能力被服务端部署配置阻断；
> 代码级整改通过，发布门禁未完成。**

---

## 一、阶段 A：基线核对与 dry-run

**基线核验（V01 通过）**：`HEAD` = `main` = `0da77a19…`，分支 `main`，工作树干净。

**路径订正**：任务书 §三 指定的 `backend/app/services/vision/native_video.py` **不存在**，
实际为 [providers/native_video.py](../backend/app/services/vision/providers/native_video.py)。

**配置只读诊断（V02 通过，未回显任何值）**
`BASE_URL` / `API_KEY` / `NAME` 均已配置；`MODEL_MODE=on`；
`SUPPORTS_NATIVE_VIDEO=false`（保持不变）；`ALLOW_INSECURE_HTTP=true`。

**Dry-run（V03 通过）**：`--help` 与 dry-run 均 `exit 0`，输出
`DRY-RUN: nothing was sent`、`state=unverified`、`sendable=False`。

**发现的环境问题（已修补）**：任务书假设 `*.local.json` 被忽略——**实测不成立**
（根 `.gitignore` 只有精确路径规则）。已新增目录级
[tsb362/.gitignore](validation/tsb362/.gitignore) 并经 `git check-ignore` 复核。

**发现的风险（用户已裁决）**

| 编号 | 风险 | 用户裁决 |
|---|---|---|
| R1 | 端点明文 `http://` 且非回环 | **用户接受风险**，阶段 B 在明文链路上执行 |
| R2 | `MODEL_MODE=on` | 既有状态，未改动 |
| R3 | 单帧负对照未接线 | **用户指示「先修 R3」**，已闭环（见 §二） |
| R4 | `*.local.json` 未被忽略 | 已修补 |
| R5 | 历史 503 | 本轮**未复现**（端点健康） |

## 二、附带修复 R3：单帧负对照必须真的只发一帧

**问题**：`run_probe` 声明 `frame_only` 却**从未读取**（AST 实测 `used=False`），视频部件只由
`include_video` 决定 → "单帧负对照"实际发送**完整 MP4**，等价第 4 个视频轮次。它几乎必然
答对，使 `classify_probe_outcome` 判成 `video_accepted_sampling_unknown`，把**真能读时序**的
端点判死（`SENDABLE_STATES` 只认 `verified_native_video`）。

**为什么 TSB-342 的 W1–W4 没抓到**：那组用例用 `_fake_probe_factory` 整体替换 `run_probe`，
假探测自己遵守 `frame_only`；`test_w1` 只断言 `frame_control_attempted is True`，从不检查该
参数是否生效。属 AGENTS.md §3.2「接线要单独测」同类缺陷。

**修复**：新增 `build_still_frame()` 从合成短片末段抽 1 帧 JPEG；`frame_only=True` 时发
**恰 1 个 `image_url`、0 个 `video_url`**。取末段是刻意的——短片是三个纯色相位，单帧
**不可能**产出合法三色顺序，这才使它成为真阴性对照而非提示词对照的重复。抽帧失败返回
`FRAME_CONTROL_UNAVAILABLE` 且不发送（C6 不静默跳过）。

**验收（RED → GREEN → 反向验证）**

| 阶段 | 结果 |
|---|---|
| RED | `1 failed`：`parts were ['text', 'video_url']` |
| GREEN | `2 passed`；专项 **40 passed**；相关 5 套件 **79 passed** |
| 反向验证 | 改回 `if False:` → W05 立刻转红、W06 仍绿；恢复后双双转绿 |

新增用例：`test_probe_temporal_evidence.py::test_w05_the_still_frame_control_sends_no_video`、
`::test_w06_a_normal_round_still_sends_the_video`（后者是防"修过头"的反向守卫）。
用例通过 monkeypatch 拦截 `httpx.post` 截获**真实** `run_probe` 构造的 payload，无网络请求。

## 三、阶段 B：真实端点探测

**执行**：用户回复「授权B」后，运行任务书 §5.2 原命令，`exit 0`，外加 1 次单轮诊断。
合计 **7 次真实 HTTP 请求**，共外发约 **16 KB** 合成数据，**无用户素材**。

**结果**

| 维度 | 实测 |
|---|---|
| 端点健康 | **HTTP 200**（此前 503 未复现） |
| 视频轮次 | **HTTP 400 × 3**（无本地重试） |
| `classify_probe_outcome` | **`unsupported`**，`sendable=False` |
| 提示词对照 | 未答对（阴性对照有效） |
| 单帧对照（R3 后） | 未答对（阴性对照有效） |
| 时序证据 | **无**——模型从未看到任何视频帧 |
| 能力记录 | **未写入**（未用 `--record-capability`） |
| `SUPPORTS_NATIVE_VIDEO` | **`false`**（未改动） |

**400 的确切原因**（另发 1 次诊断取回响应体）：

```json
{"error":{"message":"At most 0 video(s) may be provided in one prompt.
 Set `--limit-mm-per-prompt` to increase this limit. (parameter=video)", ...}}
```

**这不是"模型不支持视频"**，而是本部署的 vLLM 每 prompt 视频上限被配成 **0**。
服务端明确**认出了 video 部件**。

**解除条件**（服务端，客户端不可绕过）：部署加 `--limit-mm-per-prompt video=1`。

## 四、验证结果表

| 项目 | 实际结果 | 证据 |
|---|---|---|
| 后端严格全量（`TSB_REQUIRE_FFMPEG=1`） | **1385 collected / 1379 passed / 1 failed / 5 skipped / 0 errors**（691 s） | `backend-full.local.xml` |
| 其中唯一失败 | `test_job_cancel_kills_process.py::test_cancelling_one_run_does_not_kill_a_concurrent_run`（**基线既有偶发，用户选 B 不修**） | `full-suite-result.md` |
| R3 专项 | 40 passed | `GREEN-r3-full-suite.local.txt` |
| R3 相关 5 套件 | 79 passed | `GREEN-r3-related-suites.local.txt` |
| 新建用例数 | +2（collected 1383 → **1385**） | junit |
| 前端 | **未改动，未运行**（本轮无前端改动） | — |

> ⚠️ **不得表述为"全量全绿"**：1385 中有 **1 个基线既有失败**，已与用户确认本轮不修。

## 五、G5 / G6 门禁状态

| 门禁 | 状态 | 阻塞原因 |
|---|---|---|
| G5 端点健康 | ✅ 通过 | — |
| G5 视频被接收 | ❌ 阻断 | 部署 `--limit-mm-per-prompt video=0` |
| G5 时序能力 | ❌ 未验证 | 同上，模型未见到视频 |
| **G5 总体** | **`unsupported` + `blocked_by_deployment_config`** | 服务端参数 |
| G6 A/B | `not_evaluated` | 依赖 G5 |
| 阶段 C E2E | `not_started` | 依赖 G5 |

## 六、范围声明

**未改动**：`backend/app/` **一行未改**；`TRAVELSB_MODEL_SUPPORTS_NATIVE_VIDEO`（仍
`false`）、`MODEL_MODE`、`ALLOW_INSECURE_HTTP` 等任何安全开关；任何能力记录；
Prompt、分析算法、视频 Provider、识别结果模式；前端；数据库；历史人工评分/收藏/备注/裁剪。

**未删除或改写**：既有测试一行未改（R3 只**新增**用例）；未新增 skip/xfail；未放宽任何守卫。

**未上传**：任何用户真实视频、真实原片路径、EXIF/GPS。

**提交状态**：本轮改动**尚未提交 Git**，等待用户指示。

## 七、尚未完成项（诚实列出）

1. **视频轮次全部 400**，G5 仍为阻断；阻塞已从"端点不可用"变为**服务端部署上限为 0**。
2. **阶段 C 未执行**：真实旅行短片、正式 `video_analyze` Job、入库、UI 展示均未做，
   前置条件（G5 阳性证据）未满足。
3. **G6 未执行**，`not_evaluated`。
4. **严格全量有 1 个基线既有失败**，按用户决定**本轮不修**。
5. **探测器观测缺陷（未修）**：`run_multi_round_probe` 丢弃每轮 `response_excerpt`，
   导致 `_classify_video_rejection` 的精确诊断进不了报告（本轮结论另用诊断请求取回）。
   建议后续把首次失败的响应体带入 `RoundEvidence`。
6. 真实素材验收、200GB 性能测试**状态不变**。
