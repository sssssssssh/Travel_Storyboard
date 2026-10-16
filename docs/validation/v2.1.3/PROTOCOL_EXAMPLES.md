# TSB-325 — 实际验证的协议与探测结论

**结论（一句话）**：**`unverified`——本次未能取得任何真实视频能力证据。**
探测请求确实外发了（合成静音短片 48,829 字节），但端点返回 HTTP 503
`no healthy upstream`；随后用**纯文本**请求复测**同样 503**，证明这是端点整体
不可用，**不是**视频协议被拒绝。因此本次探测对"是否支持视频"**零信息量**。

> 不得把本文件读成"端点不支持视频"，也不得读成"端点支持视频"。
> 两者都缺乏证据。`model_supports_native_video` 必须保持 `false`。

---

## 1. 未验证项（明确写清，避免被当成示意即可用）

下列内容**全部未经真实验证**，仅为待验证的实现假设：

| 项 | 状态 | 说明 |
| --- | --- | --- |
| `video_url` content part 是否被接受 | **未验证** | vLLM 文档有该形状，但**本端点**未验证；按任务书 §2.1 不得据此认定支持 |
| `data:video/mp4;base64` 内联是否被接受 | **未验证** | 本次照此发送，但服务端未给出任何视频相关响应 |
| 文件 ID / `multipart/form-data` 协议 | **未实现** | 需另行评审，且要求安全的私有上传与删除语义 |
| 最大时长 / 最大字节 | **unknown（保持 null）** | 未做边界压测 |
| 是否接受音轨 | **unknown（保持 null）** | 默认派生静音，故未测 |
| 服务端抽帧 fps / 最大帧数 | **未披露** | `temporal_sampling_disclosed=false` |
| 超时与重试语义 | **未验证** | 无成功样本可依据 |

---

## 2. 本次实际发出的请求结构（唯一"实际验证"的部分）

即下面的结构**确实被发出过**，但**未获得可归因于视频的响应**。
因此这是"实际发送结构"的证据，**不是**"协议被接受"的证据。

```
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
       "video_url": {"url": "data:video/mp4;base64,<48829 bytes 的合成短片>"}}
    ]
  }],
  "max_tokens": 400
}
```

合成短片由本工具用 FFmpeg `testsrc` 生成：320×240、10 fps、3 秒、**无音轨**，
画面含随时间移动的方块与逐帧递增计数器——刻意做成"随时间变化"，
这样服务端若真的解码了视频，回答里应能体现时间顺序。
若用一个静止画面，服务端只解一帧也能"答对"，探测将无法区分。

---

## 3. 端点可用性对照（关键排除性证据）

| 请求内容 | 结果 | 说明 |
| --- | --- | --- |
| 文本 + 视频（本次探测） | HTTP **503** `no healthy upstream` | 2,109 ms |
| **纯文本、无图无视频** | HTTP **503** `no healthy upstream` | 对照组 |

纯文本也 503，说明故障与视频无关。**这是本次探测唯一能得出的结论**：
端点在探测时刻不可用，视频能力仍属未知。

---

## 4. 安全与隐私核对（本次执行时）

| 项 | 实测值 |
| --- | --- |
| `TRAVELSB_MODEL_MODE` | `on`（因此授权边界**允许**本次外发；dry-run 才是默认） |
| 端点 scheme | `http`（明文、非回环） |
| 外发内容 | **合成测试图样**，不含任何用户素材 |
| 音轨 | 无（`-an`） |
| 报告中的端点 | 已在提交版中脱敏为 `(redacted for committed evidence)` |
| 报告中的凭据 | 无 Key、无完整 URL、无本地路径 |

**必须记录的事实**：本次 `--execute` 真的向公网端点发送了 48,829 字节。
虽然内容是合成图样，但这是**一次真实外发**，不应被描述为"仅本地测试"。

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

工具的四道闸门：

1. 默认 dry-run；
2. `--execute` 还必须同时给 `--i-authorize-real-upload`，否则拒绝（退出码 2）；
3. 默认使用合成短片；指向真实素材需 `--clip`，且报告会标注为运行者提供；
4. 经产品自身的 `authorize_upload()` 边界取上下文——探测不能绕过产品规则。

---

## 6. 放行判定

按任务书 §5 批次 A 的停止条件：**端点未给出真实正例 → 不进入真实视频联调**。

- TSB-325 的**代码与 Mock 契约**已完成；
- TSB-326 的**适配层**可在 Mock 下实现，但 `video_native` 真实通道**保持禁用**；
- 需要端点恢复后**重跑本探测**，取得二选一结论：
  `verified_native_video` 或 `video_accepted_sampling_unknown`/`unsupported`。

**在拿到真实正例之前，不得开始 TSB-327 之后的真实视频链路，也不得宣传视频理解能力。**
