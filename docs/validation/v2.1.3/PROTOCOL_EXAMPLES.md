# TSB-325 — 实际验证的协议与探测结论

**结论（一句话）**：**`unsupported` —— 该私有部署当前不接受视频输入，
但原因是服务端的一个开关，而且这个端点具备视频能力所需的软件栈。**
`video_native` 通道保持禁用；图片模式不受影响。

> 这不是"模型不行"。错误信息来自 **vLLM**，它**认得** `video_url` 这个内容类型，
> 只是明确说：**每个 prompt 最多允许 0 个视频**。

---

## 1. 决定性证据（2026-10-16 实测，端点正常）

| 探测 | 结果 | 说明 |
| --- | --- | --- |
| 纯文本对照 | **HTTP 200** | 端点健康，结论可信（对照通过才有资格下判断） |
| 图片输入 | **HTTP 200**，回答 `Red`（正确） | 部署**确实是多模态的**，视觉通路正常 |
| **视频输入**（`video_url` + `data:video/mp4;base64`，48,829 字节合成短片） | **HTTP 400** | 见下方原始报错 |
| 视频输入（`type=video` 形状） | **HTTP 400** | 该 part 类型根本不被识别 |

### 原始错误（`video_url`）

```json
{"error":{"message":"At most 0 video(s) may be provided in one prompt. Set
 `--limit-mm-per-prompt` to increase this limit. (parameter=video)",
 "type":"BadRequestError","param":"video","code":400}}
```

### 原始错误（`type=video`，作为对照）

```json
{"error":{"message":"Unsupported chat content part type: 'video'. Supported types:
 audio_embeds, audio_url, image_embeds, image_pil, image_url, input_audio,
 input_image, input_text, output_text, prompt_embeds, refusal, t..."}}
```

两次报错放在一起才有意义：

- `type=video` → **类型本身不存在**（`video_url` 才是 vLLM 的写法）；
- `type=video_url` → **类型被识别**，被拒的是**数量上限 0**。

所以这是**服务端配置问题，不是客户端协议写错，也不是模型不支持**。

---

## 2. 结论的确切含义

| 说法 | 是否成立 |
| --- | --- |
| 端点接受视频输入 | ❌ **不成立**（当前 limit=0，请求被拒） |
| 端点软件栈具备视频能力 | ✅ **成立**（vLLM 报错专门针对 video 且给出调参提示） |
| 模型本身不支持视频 | ❌ **不能这样断言**（未验证；被限流挡住，看不到模型层行为） |
| 图片/多帧通路可用 | ✅ 成立（图片实测 200 且回答正确） |

**能改变结论的唯一操作**（服务端，不是客户端）：

```
--limit-mm-per-prompt video=1
```

这是 vLLM 的启动参数。改完后需要**重启部署**，然后重跑本探测。

---

## 3. 实际发出的请求结构（已实际验证的部分）

```http
POST {model_base_url}/chat/completions
Authorization: Bearer <已脱敏, 未记录>
Content-Type: application/json

{
  "model": "GLM-5.3-Flash",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "text",
       "text": "Describe what changes over time in this video, in order. ..."},
      {"type": "video_url",
       "video_url": {"url": "data:video/mp4;base64,<48829 字节合成短片>"}}
    ]
  }],
  "max_tokens": 400
}
```

- 内容是 FFmpeg `testsrc` 生成的**合成静音**短片：320×240、10 fps、3 秒、`-an` 无音轨。
  刻意做成**随时间变化**（移动方块 + 逐帧计数器），这样服务端若真解码了视频，
  回答里应能体现时间顺序；用静止画面则只解一帧也能"答对"，探测将无法区分。
- `video_url` 是本端点**唯一**正确的视频 part 类型（`type=video` 已被实测否定）。

### 仍然未验证的项

| 项 | 状态 |
| --- | --- |
| 视频被接受后能否给出有用的时间信息 | **未验证**（被 limit 挡住，无样本） |
| 最大时长 / 最大字节 | **unknown（保持 null）**，未做边界压测 |
| 是否接受音轨 | **unknown**，默认派生静音故未测 |
| 服务端抽帧 fps / 最大帧数 | **未披露**，`temporal_sampling_disclosed=false` |
| 文件 ID / `multipart/form-data` 协议 | **未实现**，需另行评审 |
| 视频在 GLM-5.3-Flash 上的实际理解质量 | **完全未知**——这才是 TSB-330 A/B 要回答的问题 |

---

## 4. 安全与隐私核对（本次执行时）

| 项 | 实测值 |
| --- | --- |
| `TRAVELSB_MODEL_MODE` | `on`（授权边界允许本次外发；dry-run 才是默认） |
| 端点 scheme | `http`（明文、非回环）——**图片与令牌在网络不加密** |
| 外发内容 | **合成测试图样**，不含任何用户素材 |
| 音轨 | 无（`-an`） |
| 报告中的端点 | 提交版已脱敏为 `(redacted for committed evidence)` |
| 报告中的凭据 | 无 Key、无完整 URL、无本地路径 |

**已发生的真实外发**：本次探测向公网端点发送了 48,829 字节**合成**视频 +
若干文本/图片请求。内容全为合成素材，但**这是一次真实外发**，
不应被描述为"仅本地测试"。

---

## 5. 复现方式

```powershell
cd backend
# 默认 dry-run：只打印拟用协议、字节估算与风险，不发送任何字节
.\.venv\Scripts\python.exe tools\probe_video_capability.py

# 真实探测：两道门都必须显式给出
.\.venv\Scripts\python.exe tools\probe_video_capability.py `
    --execute --i-authorize-real-upload `
    --json ..\docs\validation\v2.1.3\CAPABILITY_REPORT.json --redact
```

工具的四道闸门：① 默认 dry-run；② `--execute` 还必须给
`--i-authorize-real-upload`；③ 默认合成短片（指向真实素材需 `--clip` 且会被标注）；
④ 经产品自身 `authorize_upload()` 取上下文。

工具还会自动跑一次**纯文本对照**——没有它，上一次的端点 503 就会被误报成
"不支持视频"（一个凭空捏造的否定结论）。

---

## 6. 放行判定

按任务书 §5 批次 A 停止条件：**端点当前不接受视频 → 不进入真实视频联调**。

- TSB-325 的**代码与契约**已完成，能力结论为 **`unsupported`（服务端配置所致）**；
- TSB-326 的**适配层**已完成，`video_native` 真实通道**保持禁用**；
- **若要让视频通道真正可用**，需要服务端加 `--limit-mm-per-prompt video=1`
  并重启部署，然后重跑本探测取得二选一结论：
  `verified_native_video` 或 `video_accepted_sampling_unknown`。

**在拿到真实正例之前，不得开始 TSB-327 之后的真实视频链路，
也不得宣传视频理解能力。** 目前仍无任何证据表明该模型能有效理解视频内容本身。
