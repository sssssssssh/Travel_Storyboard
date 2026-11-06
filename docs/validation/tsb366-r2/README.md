# TSB-366-R2 证据索引

基线：`6771e93c30e0b1a19f8bada60bc0041404eebb00`

## 可提交的脱敏摘要

| 文件 | 内容 |
|---|---|
| [../../RELEASE_TSB_366_R2.md](../../RELEASE_TSB_366_R2.md) | 完整发布记录 |
| [ROOT_CAUSE.md](ROOT_CAUSE.md) | **根因分析**：环境记录、失败复现、7 步排除过程、证据链、修复方案与拒绝的替代方案 |

## 仅本地、不提交（本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `REPRO-single.local.txt` | 原始失败完整 traceback |
| `RED-loopback.local.txt` | 回归测试在修复前的 RED 输出 |
| `STABILITY-20x.local.txt` | 目标 408 用例连续 20 次结果 |
| `STABILITY-file5x.local.txt` | Provider 文件连续 5 次结果 |
| `backend-full.local.txt` / `.xml` | 后端完整运行日志与 junit |

## 结论速查

| 项 | 结果 |
|---|---|
| 分类函数 `classify_status(408)` | **正确，未修改** |
| 根因 | Windows 系统代理拦截 loopback，httpx 不解析 `ProxyOverride` |
| 失败环节 | 断言（code 为 `MODEL_NETWORK_ERROR` 而非 `MODEL_TIMEOUT`） |
| mock 是否收到请求 | **是**（两种情况下都收到——故"到达"不能证明"响应未被破坏"） |
| 修复 | 仅 loopback 用 `trust_env=False`；远程端点与生产代理支持不变 |
| 目标用例 | 本机 20/20 |
| Provider 文件 | 5/5 |
| 新增回归测试 | 76 项全绿 |
| 反向验证 | 去掉绕过即复现原始 `RemoteProtocolError` |
| 是否改预期/skip | **否** |
| 真实模型服务 | 未调用（本轮不要求） |

## 证据链摘要（为何排除其他假设）

| 假设 | 证据 | 结论 |
|---|---|---|
| 分类函数错误 | `classify_status(408)` 返回 `MODEL_TIMEOUT` | ✗ 排除 |
| 服务端返回错误 | 原始 socket 读到完整合法 408 响应 | ✗ 排除 |
| httpcore 问题 | `httpcore.ConnectionPool` 返回 `OK status=408` | ✗ 排除 |
| keepalive/复用 | `max_keepalive_connections=0` 仍失败 | ✗ 排除 |
| 环境变量代理 | `HTTP_PROXY` 等**全部未设置** | ✗ 排除 |
| 系统代理（注册表） | `ProxyEnable=1`、httpx 映射到 `127.0.0.1:<proxy-port>`、`trust_env=False` 唯一可修复 | ✓ **根因** |
| mock 状态串扰 | 单用例可复现、无并行；但共享状态风险确实存在，已另行加固 | 非根因 |
| 依赖版本差异 | 本机 httpx 0.28.1 / httpcore 1.0.9，与 CI 一致 | ✗ 排除 |
