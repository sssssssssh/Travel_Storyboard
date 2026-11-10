# TSB-370 PAI-EAS 能力与授权（脱敏）

**不含**：密钥、端点完整 URL、账号标识、任何图像内容。

---

## 1. 端点归属确认

| 项 | 值 | 判定依据 |
|---|---|---|
| 类型 | 阿里云 PAI-EAS 私有部署推理端点 | host 后缀 `pai-eas.aliyuncs.com` |
| 区域 | `cn-wulanchabu` | host 中的 region 标签 |
| 是否回环 | 否 | 解析到公网地址 |
| **协议** | **HTTPS（TLS 1.3）** | 443 端口握手成功，`/models` 返回 200 |
| 模型 ID | `GLM-5.3-Flash` | `/models` 返回的 `data[].id` |
| 归属核验 | ✅ 用户自部署 | 用户本轮明确授权 |

### 1.1 发现并修复的问题：明文 HTTP

**实验开始时**配置为 `http://`，且设了 `TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=1`。

任务书 §1.2 要求校验 TLS，§0.3 禁止启用不安全 HTTP。实测该端点
**443 端口支持 TLS 1.3**，故改为 `https://` 并关闭不安全开关。

**影响**：本轮全部真实素材（11 段静帧 + 2 段多帧的派生图）均经加密通道发送。

---

## 2. 能力表（独立状态，非单一布尔值）

| 能力 | 状态 | 证据 |
|---|---|---|
| 文字输入 | ✅ **可用** | `/models` 200；纯文字 `chat` 返回 `TEXT_ONLY_OK` |
| 单张图片 | ✅ **可用** | 11 次静帧成功，输出含具体主体/动作 |
| 多张图片 | ✅ **可用** | 2 次多帧成功，3 图/次 |
| JSON 输出 | ✅ **可用** | 静帧返回结构化 JSON，经生产 `validate_response` 通过 |
| **原生视频** | ⚠️ **无法判定** | 探测 503，但**纯文字对照也 503** |
| 端点可用性 | ❌ **实验中途离线** | 6 次健康检查全 503 `no healthy upstream` |

### 2.1 原生视频：为什么不是"不支持"

| 调用 | 结果 |
|---|---|
| `video_url` + base64 data URI | 503 `upstream connect error` |
| 同端点**纯文字**（对照） | 503 `no healthy upstream` |
| `/models`（控制面） | 503 `no healthy upstream` |

**三者同时 503** → 是端点故障。若只看视频结果就断言"模型不支持视频"，
属于错误归因。

**正确状态**：`BLOCKED_UNAVAILABLE`（可用性），**不是** `BLOCKED_CAPABILITY`（能力）。

### 2.2 一个必要的项目侧扩展

项目 Provider 原本**硬性要求至少一张图**（`assert_images_supported`）。
纯文字策划会被拒绝为 `MODEL_CAPABILITY_UNSUPPORTED`。

**扩展方式**（未削弱任何既有守卫）：

```python
VisionRequest(..., text_only=True)   # 显式声明这是文字调用
```

- 默认 `False` → 所有既有调用行为不变；
- 只有声明 `text_only` 才跳过图像数量检查；
- 「视觉调用忘给图」仍然报错。

---

## 3. 授权流

### 3.1 三层门禁（全部必须通过）

| 层 | 检查 | 失败码 |
|---|---|---|
| 全局 | `TRAVELSB_MODEL_MODE=on` | `MODEL_UPLOAD_DENIED` |
| **每请求** | `upload_acknowledged=true` | `MODEL_UPLOAD_DENIED` / `MODEL_CALL_NOT_AUTHORIZED` |
| 端点 | `model_base_url` + `model_api_key` 非空 | `MODEL_NOT_CONFIGURED` |

模型策划额外需要 `planning_mode=model` **且** `upload_acknowledged=true`。

### 3.2 授权对象

`authorize_upload()` 返回 `AuthorizedModelContext`（每调用一个，含
`project_id` / `purpose` / `acknowledged_at`）。Provider 拒绝 `None` 上下文，
因此不存在"进程级已授权"这种状态。

### 3.3 本轮授权范围

用户明确授权：**本人 PAI-EAS、受限试验、经清单确定的素材**。

**不覆盖**：第三方 API、原片上传、未知域名、全库 99 镜头、无限重试、EAS 扩容。

本轮**未越过**该范围：所有调用仅至本人 EAS，且总 attempts 恰为上限 20。

---

## 4. 预算账本机制

| 项 | 实现 |
|---|---|
| 位置 | 仓库外临时目录 JSON 文件 |
| 原子性 | 写临时文件 + `os.replace` |
| 重启安全 | ✅ 从文件重新读取，不复位 |
| 扣减时机 | **发送前**（超时/失败/取消都算） |
| 接入点 | `post_model_request()`（唯一 HTTP 口）+ `native_video.attempt_guard` |
| 渠道归属 | `ContextVar`，由各任务入口声明 |

### 4.1 上限与用量

| 渠道 | 上限 | 实际 |
|---|---:|---:|
| 能力探测 | 2 | **2** |
| 静帧 | 12 | **12** |
| 多帧 | 4 | **2** |
| 原生视频 | 2 | **2** |
| 文字策划 | 2 | **2** |
| **合计** | **20** | **20** |

见 [REAL_MODEL_RUN_LEDGER.md](REAL_MODEL_RUN_LEDGER.md)。

### 4.2 零重试的实现

`.env` 中 `TRAVELSB_MODEL_MAX_RETRIES` **保持产品默认（2）**——
为实验改全局曾导致 4 个既有测试失败，已回滚。

本轮实验进程通过环境变量把重试设为 0，且**账本在发送层兜底**：
即使某层重试，每次 POST 都要先扣额度。

---

## 5. 资源与费用

| 项 | 说明 |
|---|---|
| Token 费用 | 用户自述自部署**无额外按 Token 计费** |
| 实例/GPU | **未确认是否免费**；本轮**未扩容、未改实例、未重启服务** |
| 实际消耗 | 约 2.3 MB 出向流量；静帧约 23k tokens、多帧约 8k tokens（服务端报告） |

---

## 6. 隐私处理

| 项 | 处理 |
|---|---|
| 派生图 EXIF/GPS | 项目现有 `render_privacy_image` 已剥离 |
| 用户自由备注 | **从不发送**（单测覆盖：含人名与电话的备注不出现） |
| 原始路径 | **从不发送** |
| 原片 | **从未上传** |
| 日志 | 只保留脱敏摘要、模型 ID、字节数与状态 |

**残余风险**：航拍画面仍可能含人像、车牌、住宅。本轮**未进行**发送前的人工
画面预筛（见 `SELECTED_SHOTS.md` §4 `MANUAL_REVIEW_PENDING`）。
