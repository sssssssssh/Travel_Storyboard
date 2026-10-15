# TSB-317 用户侧安全端点迁移验收

2026-10-15 用户提供服务与凭据后，验证同一服务的 HTTPS 入口：保持证书校验，未携带凭据的探测返回 401；随后真实多图 probe 返回结构化结果（1 次请求、2 张生成图片、413 tokens），见 `HTTPS_REAL_PROBE.json`。收到真实素材帧外发授权后，另通过 HTTPS 分析 8 个真实镜头；完整计数见 Mac 报告。私有地址及 Key 仅写被 Git 忽略、权限 0600 的 backend/.env；默认 MODEL_MODE=off，明文例外关闭。没有修改任何云资源。

以下为可复用清单；真实接入结果与模拟 TLS 失败/限流测试分开记录，不能互相替代。

1. 请实际模型服务所有者提供有效 HTTPS 网关、证书与凭据，或者已经认证且加密的本地隧道。不要修改云资源来绕过审批；隧道另一端的认证和加密由服务所有者验证。本地工具不能仅凭 localhost URL 证明隧道可信。
2. 只在被 Git 忽略的 `backend/.env` 填入 `TRAVELSB_MODEL_BASE_URL`、`TRAVELSB_MODEL_API_KEY`、`TRAVELSB_MODEL_NAME`。不要在聊天、命令历史、Git、公开日志中填真实地址/Key。`TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=false`，先保持 `TRAVELSB_MODEL_MODE=off`。
3. 从 backend 运行 `../.venv/bin/python tools/verify_secure_model.py`（Windows 使用 `.venv\Scripts\python.exe`）。默认仅检查配置，**零上传**；输出不包含端点、凭据或本地路径。localhost 隧道需额外 `--authenticated-loopback-tunnel`，这仅记录用户确认，并不自动验证远端隧道。
4. 准备接入测试后将 MODEL_MODE 设 on，运行 `../.venv/bin/python tools/verify_secure_model.py --probe --acknowledge-upload`。此命令明确授权发送两张内存生成的红/蓝小图片；不读取用户素材。保留输出中的结构化结果、实际请求次数和供应商返回的 token 数。不会估算供应商价格。
5. 无效/过期证书应返回 `MODEL_TLS_FAILED`，不能使用 verify=False、curl -k 或自签证书冒充生产验收。401 为 MODEL_AUTH_FAILED、不重试；429/超时/网络故障有界重试；取消或关闭总开关后停止下一次请求。错误正文不公开，HTTPX 日志隐藏地址。
6. 新旧业务入口在 mode=off 或本次未同意时必须零出站；已创建的授权上下文不能绕过后来关闭的总开关。再次打开开关后，真实镜头外发仍需 UI 当次同意。
7. G2 真实 HTTPS/隧道接入 + G7 真实素材流程另行验收。合成图片 probe 通过仅证明这次端点传输与结构化多图协议，不证明素材理解或导演决策。

`calls` 为交给 HTTP 客户端的请求尝试数，包含失败/重试；`images_sent` 为这些尝试携带的图像项，并不承诺远端已接收或计费。实际 token 只记录供应商明确返回的数字，不能由图片数推断计费。
