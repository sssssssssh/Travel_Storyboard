# TSB-362 阶段 A — Dry-run 与配置只读诊断（dry-run-summary）

执行时间：2026-11-03 15:35（Asia/Shanghai）　工作目录：`backend/`（`.env` 生效所必需）
基线：`main@0da77a19725a6af5963672a84d6f6464d4be7ac8`（详见 [BASELINE.md](BASELINE.md)）

> 本轮**没有**发出任何真实模型请求，**没有**上传任何视频（真实或合成），
> **没有**修改任何生产安全开关，**没有**写入能力记录。

## A2 — 环境与 CLI 契约核对

| 检查项 | 实测 | 判定 |
|---|---|---|
| `python --version` | `3.13.7`（`backend/.venv`） | 与 AGENTS.md §4.1 一致 |
| `backend/.env` 存在 | **是**，且被根 `.gitignore` 覆盖（`git check-ignore` 退出码 0） | 通过 |
| 其它 `.env*` | 仓库内除 `.env.example` 外无第二份 | 通过 |
| `ffmpeg` 可定位 | `locate_ffmpeg()` 返回真值（未依赖 PATH） | 通过 |
| `--help` | 退出码 **0**；选项与任务书 §一 完全一致 | 通过 |

`--help` 实测选项（与任务书所述一致，无虚构）：`--execute`、`--i-authorize-real-upload`、
`--clip`、`--protocol`、`--json`、`--redact`、`--ascii`、`--rounds`（默认 3）、
`--duration`（默认 3.0）、`--record-capability`。

## 配置只读诊断（只报存在性，不回显值）

| 配置项 | 状态 | 说明 |
|---|---|---|
| `TRAVELSB_MODEL_BASE_URL` | **已配置**，scheme = `http`，**非回环**，无 query，path 4 段 | ⚠️ 见下方风险 R1 |
| `TRAVELSB_MODEL_API_KEY` | **已配置**（值未读取、未打印） | — |
| `TRAVELSB_MODEL_NAME` | **已配置**（Pydantic 默认值已覆盖为实际值） | 模型标识见下 |
| `TRAVELSB_MODEL_MODE` | `'on'` | ⚠️ 见风险 R2 |
| `TRAVELSB_MODEL_SUPPORTS_NATIVE_VIDEO` | **false** | ✅ 安全门禁保持默认关闭 |
| `TRAVELSB_MODEL_ALLOW_INSECURE_HTTP` | **true** | ⚠️ 见风险 R1 |
| `TRAVELSB_MODEL_MAX_REQUEST_BYTES` | `8000000` | — |
| `TRAVELSB_MODEL_MAX_RETRIES` | `2`（探测器不使用） | — |
| `TRAVELSB_MODEL_TIMEOUT_SEC` | `180.0`（探测器硬编码 120） | — |
| `TRAVELSB_FFMPEG_PATH` / `FFPROBE_PATH` | 均已配置 | — |

脱敏模型标识：`GLM-5.3-Flash`（为空时脚本回落 `settings.model_name`，本机已生效）。
端点仅以区段描述：`http://<私有子域>.<地域>.<云厂商网关域>/<4 段路径>`（主机名整体遮挡）。

## 请求协议（dry-run 判定的实际形态）

- 协议名：`video_url_data_uri`；传输：`chat_completions_content_part`；MIME：`video/mp4`。
- 请求体：`POST {base_url}/chat/completions`，`Authorization: Bearer <key>`，
  content 为 `[{"type":"text",...}, {"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}}]`，
  `max_tokens: 400`。**全程内联，服务器不抓取任何 URL**（`fetches_remote=False`，无 SSRF 面）。
- 该协议**未经本端点验证**；是否被接受正是阶段 B 要回答的问题。

## A3 — Dry-run 真实输出与退出码

命令（原样，来自任务书 §4.2）：

```powershell
cd backend
.\.venv\Scripts\python.exe tools\probe_video_capability.py --help
.\.venv\Scripts\python.exe tools\probe_video_capability.py --redact --json ..\docs\validation\tsb362\probe-dry-run.local.json
```

**`--help` 退出码 0；dry-run 退出码 0。** 关键输出原文：

```
== video capability probe ==
  mode            : dry_run
  protocol        : video_url_data_uri (chat_completions_content_part)
  clip            : synthetic_testsrc, 0 bytes, has_audio=False
  model           : GLM-5.3-Flash
  state           : unverified
  sendable        : False
...
DRY-RUN: nothing was sent. Re-run with --execute --i-authorize-real-upload
         only when you explicitly approve uploading a video to this endpoint.

report written: ..\docs\validation\tsb362\probe-dry-run.local.json
```

**V03 判定：通过。** `state=unverified`、`sendable=False`、`clip_bytes=0`、明确打印
`DRY-RUN: nothing was sent`。报告 **没有被**标成视频能力通过。

无痕核对：dry-run **不会**调用 `build_synthetic_clip`（只在 `--execute` 分支构建），
故未生成临时短片；`%TEMP%` 下现存 17 个 `tsb-video-probe-*` 目录**全部早于**
2026-11-02 18:30（最近 30 分钟内新增 **0** 个），与本轮无关。未使用
`--record-capability`，能力库无任何写入（`capability-ish files found: 0`）。

## 关于 `--redact` 的诚实说明（不构成阻断，但需知悉）

`--redact` **只脱敏写入文件的 JSON**，**不脱敏控制台**。dry-run 投屏的 `endpoint` 行
完整打印了私有主机名（形如 `http://<私有子域>.<云厂商网关域>`，此处已整体遮挡）。落盘 JSON 中
`endpoint_host_redacted` 已被替换为 `"(redacted for committed evidence)"`，全文检索
无 Key、无完整 URL。因此：

- **可提交 Git 的**只有落盘 JSON 的脱敏字段与本文档的手工摘要；
- **控制台原文**（连同主机名）本轮只保存为
  `docs/validation/tsb362/probe-dry-run.stdout.local.txt`，已被目录级 `.gitignore` 排除，
  **不得进入 Git**；上表中的主机名在本文档已做遮挡。

## A4 — 阶段 B 会发出什么（预算，已实测）

| 项目 | 实测值 |
|---|---|
| 合成短片大小 | 3 852 B / 3 905 B（两个不同颜色顺序各一片，`libx264 ultrafast`、320x240@10fps、3 s、**无音轨**） |
| 单个视频轮请求体 | **5 974 B**（含 base64 视频） |
| 本地字节守卫 `LOCAL_MAX_BYTES` | 12 000 000（短片占 0.03%） |
| 请求体上限 `MODEL_MAX_REQUEST_BYTES` | 8 000 000（单请求占 0.07%） |
| 轮数 `--rounds` 默认 | 3（`REQUIRED_PROBE_ROUNDS`） |
| 请求次数 | **6 次**＝1 次纯文本控制 + 3 次视频轮 + 1 次“无视频”提示词负对照 + **1 次帧负对照**（+`main()` 另发的文本健康探测） |
| 总外发字节（上界） | 约 **17.9 KB** 视频体 + 少量文本 |
| 重试 | 探测器**没有**重试循环，`MODEL_MAX_RETRIES` 不适用 → 最坏=最好=6 次 |
| 超时 | 每请求 120 s（探测器硬编码）→ 理论上界 12 min |
| 费用 | 合成纯色短片，输入约 6 KB/次、`max_tokens=400`；**量级可忽略**，但计费口径以服务商为准，需用户确认 |

## A5 — 当前阻塞项与风险

| 编号 | 风险 | 影响 | 处置建议 |
|---|---|---|---|
| **R1** | 端点为 **明文 `http://` 且非回环**，靠 `TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=true` 才放行 | 视频与 Bearer 令牌以**明文**过网。任务书 §2.2 明令“不得为了绕过问题而…改成公网明文 HTTP”；AGENTS.md C5 写明“当前 Mac 使用被忽略的 backend/.env 配置 HTTPS…明文例外关闭” | **阶段 B 前必须由用户裁决**：改为 HTTPS/受认证加密隧道，或**书面接受**本次探测在明文链路上执行的已知风险 |
| **R2** | `TRAVELSB_MODEL_MODE=on` | 授权边界对本次探测放行（dry-run 已提示）。这是生产安全开关，**本轮未改动** | 无需改动；仅记录当前为 on |
| **R3** | **帧负对照失效（真实缺陷，已修复）** | `run_probe` 声明了 `frame_only` 参数但**函数体 0 次使用**；视频部件仅由 `include_video` 决定 → “单帧对照”实际发送的是**完整视频**。它几乎必然答对，`classify_probe_outcome` 会因此把结果降级为 `video_accepted_sampling_unknown`，可能把**真正具备时序能力**的端点误判为未证实 | **已按 RED→GREEN 修复**，见 [r3-frame-control-fix.md](r3-frame-control-fix.md)；阶段 B 现在可用干净的单帧对照 |
| **R4** | `*.local.json` 未被忽略（已修补） | 见 [BASELINE.md](BASELINE.md) | 已加 `docs/validation/tsb362/.gitignore` 并经 `git check-ignore` 复核 |
| **R5** | 历史 `HTTP 503 no healthy upstream` | 上游健康未知 | 阶段 B 一旦出现 5xx/503，**立即停止上传**，记 `blocked_by_endpoint`，不循环重试 |

### R3 的静态证据（未发任何请求即已确证）

```
run_probe declares frame_only : True
run_probe body mentions frame_only after its signature : 0 occurrences
payload gated ONLY by include_video : True
payload gated by frame_only : False
```

现有测试为何没抓到：`backend/tests/test_probe_temporal_evidence.py` 的 `_fake_probe_factory`
用一个**假探测**替换了 `run_probe`，假探测自己按 `frame_only` 行事（第 403、422、484 行），
所以真实 `run_probe` 忽略该参数的行为**从未被覆盖**；`test_w1` 对帧对照只断言
`frame_control_attempted is True`，未断言 `frame_only` 真的被设置。属 AGENTS.md §3.2
“接线要单独测”同一类缺陷。

### 为什么当时不修 R3（现已修复）

阶段 A 只做只读检查与 dry-run，修复探测器属于“代码修改”，按任务书 §十 需要先出 RED、
再确认不是上游问题，并由用户决定是否在本轮动代码。此外 R3 **不影响阶段 B 的
`unsupported` / `blocked_by_endpoint` 判定**，只影响“2xx 但未证实”这一档的**归因质量**。

**后续（同日，经用户授权）**：已按 RED → GREEN → 反向验证修复，单帧对照现在真的只发
1 帧 JPEG。详见 [r3-frame-control-fix.md](r3-frame-control-fix.md)。修复只触及
`backend/tools/probe_video_capability.py` 与对应测试，**未改任何 `backend/app/` 生产代码**。

## 授权前检查单（任务书 §4.3）

| # | 项目 | 现状 |
|---|---|---|
| 1 | 使用哪个 endpoint | 私有 HTTPS-less PAI-EAS 主机（本文档已遮挡；仅本地文件含原文） |
| 2 | 是否真实 HTTPS / 受认证加密隧道 | ❌ **否**，明文 `http`，非回环（R1） |
| 3 | 是否发现服务端 503 历史风险 | ✅ 有历史记录（TSB-332 期 `HTTP 503 no healthy upstream`），当前健康状态**未知** |
| 4 | 每轮上传最大大小 / 守卫 | 3.9 KB 短片；`LOCAL_MAX_BYTES`=12 MB、`MODEL_MAX_REQUEST_BYTES`=8 MB 均在限内 |
| 5 | 实际请求数 / 预算 | **6 次**（含 2 个负对照），无重试，总外发约 17.9 KB |
| 6 | 是否需用户确认成本与授权 | ✅ **需要**（见下） |
| 7 | 默认安全开关 | `SUPPORTS_NATIVE_VIDEO=false` 保持不变；`MODE=on` 与 `ALLOW_INSECURE_HTTP=true` 为既有状态，本轮未改 |

## 阶段 A 验收对照

| 检查点 | 结果 |
|---|---|
| V01 基线 SHA / 工作树 | ✅ 三者与冻结基线一致 |
| V02 `.env` 与脚本契约 | ✅ 必填项已配置；无密钥回显 |
| V03 Dry-run | ✅ 未上传、退出码 0、报告已生成且 `sendable=False` |
| V04 授权门禁 | ✅ 本轮真实请求 **0** 次，合成视频上传 **0** 次 |
| V05–V13 | ⏸ 未执行（需阶段 B/C 授权），状态维持 `blocked` / 默认关闭 |
| V14 既有测试 | 阶段 A 本体**未改产品代码**，未跑全量。**后续 R3 修改**后已跑专项 40 + 相关 79 全绿；严格全量仍待补，见 [r3-frame-control-fix.md](r3-frame-control-fix.md) §九 |
