# TSB-362 阶段 B — 真实端点探测结果（native-video-probe-summary）

授权：用户于 2026-11-03 明确回复「授权B」（同时确认 R1 明文链路风险已接受）。
执行时间：2026-11-03 16:13（Asia/Shanghai）　工作目录：`backend/`
基线：`main@0da77a19…`（+ 本轮 R3 修复，`backend/app/` 零改动）

> 本轮**只发送工具自生成的 3 秒静音合成 MP4**（3 906 B），**没有**发送任何用户真实视频。

## 一、执行了什么

```powershell
cd backend
.\.venv\Scripts\python.exe tools\probe_video_capability.py `
  --execute --i-authorize-real-upload --redact `
  --json ..\docs\validation\tsb362\probe-real.local.json      # exit 0
```

加 1 次**单轮诊断请求**（为取回 400 的响应体，见 §四）。合计 **7 次真实 HTTP 请求**：

| # | 内容 | 状态 | 说明 |
|---|---|---|---|
| 1 | 纯文本健康对照 | **200** | 端点可用；`content: null` + `refusal` 字段（见 §三） |
| 2–4 | 3 个视频轮次（各自随机颜色顺序） | **400 / 400 / 400** | 全部被拒，**无本地重试** |
| 5 | 提示词对照（无视频部件） | 报告仅记 `answered_correctly=false` | ⚠️ 该对照的 HTTP 状态**未被工具单独记录**，见 §九.3 |
| 6 | 单帧对照（**1 帧 JPEG**，R3 修复后） | 报告仅记 `answered_correctly=false` | 同上，状态未单独记录；阴性对照有效 |
| 7 | 单轮诊断（取 400 响应体） | **400** | 见 §四 |

未使用 `--record-capability`，**能力记录未被写入**。`TRAVELSB_MODEL_SUPPORTS_NATIVE_VIDEO` 仍为 `false`。

## 二、探测器的原始判定（`classify_probe_outcome` 真实输出）

```
mode   : real_probe
state  : unsupported          sendable : False
scope  : endpoint refused the video payload (HTTP 400)
evidence: rounds_total=3 rounds_passed=0
          control_attempted=true  control_answered_correctly=false
          frame_control_attempted=true frame_control_answered_correctly=false
```

按任务书 §5.3 的四档语义，这属于**「视频协议不兼容」**——服务可用但拒绝视频格式/schema。

## 三、端点健康（V05 通过）

- 纯文本对照 **HTTP 200**，返回 `model: GLM-5.3-Flash`。
- **此前记录的 `HTTP 503 no healthy upstream` 未复现**：端点当前是健康的。
- 该 200 的 `content` 为 `null`，消息对象里带 `refusal` 字段——对"reply with the single word: ok"
  的拒答，与本任务无关，但说明该模型有安全拒答行为，后续业务验收需留意。

## 四、400 的确切原因（关键结论）

探测器汇总报告丢弃了每轮的响应体（`run_multi_round_probe` 只搬 `http_status`/`error_code`，
`response_excerpt` 为空串），因此另发 1 次单轮诊断取回脱敏响应体：

```json
{"error":{"message":"At most 0 video(s) may be provided in one prompt.
 Set `--limit-mm-per-prompt` to increase this limit. (parameter=video)",
 "type":"BadRequestError","param":"video","code":400}}
```

**这不是"模型不支持视频"，而是这个部署的 vLLM 每 prompt 视频上限被配成了 0。**

工具里本来就有针对这条规则的精确分支
（`_classify_video_rejection`，匹配 `limit-mm-per-prompt` / `may be provided in one prompt`），
对同一段响应体会输出：

> vLLM recognises the video content part, but this deployment's per-prompt video limit is 0
> — the serving configuration needs `--limit-mm-per-prompt video=1` (a server-side change, not client-side)

只因响应体没被带进汇总，这份精确诊断**没有进入报告**，报告只剩笼统的 `unsupported`。

### 这意味着

| 问题 | 答案 | 证据强度 |
|---|---|---|
| 端点是否可用？ | **是**（200） | 实测 |
| 视频部件是否被服务端识别？ | **是**——vLLM 明确认出了 `video` 参数并以"上限为 0"拒绝 | 服务端错误文本 |
| 模型是否支持视频？ | **本部署下无法证实**（上限 0，请求根本没进模型） | — |
| 是否具备时序理解？ | **完全未验证**——没有任何视频帧被模型看到 | — |

⚠️ **不能**由此推断"GLM-5.3-Flash 不支持视频"，也**不能**推断"支持视频"。
当前只能说：**本部署的配置拒绝了所有视频输入**。

## 五、负对照有效性（V07 部分）

两个负对照都**按正确标准失败**（`answered_correctly=false`），说明它们不是空转：

- 提示词对照（无视频）未答对 → 模型没有从问题本身读出答案；
- 单帧对照（R3 修复后，发 1 帧 JPEG）未答对 → 单张缩略图不足以回答三色顺序问题。

但这是**否定性证据**：因为视频轮次全部 400，本轮**没有任何阳性时序证据**。

## 六、G5 门禁状态

| 项 | 状态 |
|---|---|
| G5 端点健康 | ✅ **健康**（此前 503 已不再复现） |
| G5 视频被接收 | ❌ **否**——400，每 prompt 视频上限为 0 |
| G5 时序能力证实 | ❌ **未验证**（模型从未看到任何视频） |
| `classify_probe_outcome` 输出 | `unsupported` |
| `SUPPORTS_NATIVE_VIDEO` | `false`（**未改动**） |
| 能力记录 | **未写入** |
| G6 A/B | `not_evaluated` |

**结论措辞**（按 AGENTS.md C8）：**端点已恢复可用，但原生视频能力被服务端配置阻断；
代码级整改通过，发布门禁未完成。**

## 七、阻塞解除条件（服务端，非客户端）

需要在模型服务侧调整部署参数：

```
--limit-mm-per-prompt video=1
```

这是**服务端变更**，客户端改任何代码都无法绕过，也不应该绕过（任务书 §2.2 禁止为通过
而弱化门禁）。调整后重跑同一条阶段 B 命令即可。

> 若该 vLLM 版本不支持 `--limit-mm-per-prompt video=1`（旧版只支持 `image`），
> 则需升级 vLLM 或换部署；二者都不在本仓库范围内。

## 八、隐私与预算

- 只发送 **3 906 B 合成短片** ×4 次（3 轮 + 1 诊断）+ 1 帧 JPEG，**无用户素材**。
- 总外发约 **16 KB**，**7 次请求**，**无失败重试**，总耗时约 **20 秒**。
- 报告已 `--redact`；端点主机名仅存在于被 `.gitignore` 排除的本地文件。
- 明文 HTTP 风险（R1）由用户书面接受；本轮**未**新增或放宽任何安全开关。

## 九、未完成项

1. 视频轮次全部 400，**G5 仍为 blocked**（阻塞原因已从"端点不可用"变为"部署上限为 0"）。
2. 阶段 C（真实旅行短片 + 正式 Job/DB/UI E2E）**未执行**，前置条件未满足。
3. 探测器**响应体丢失**是一个可修的观测缺陷（建议：把每轮首次失败的
   `response_excerpt` 带入 `RoundEvidence`，使 `_classify_video_rejection` 的精确诊断能进报告）。
   本轮**未修**，留作后续批次；不影响本次结论（原因已由诊断请求取回）。
