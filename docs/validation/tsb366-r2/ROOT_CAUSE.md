# TSB-366-R2 根因分析（阶段 0–1 证据）

| 字段 | 内容 |
|---|---|
| 实际 HEAD | `6771e93c30e0b1a19f8bada60bc0041404eebb00` |
| 工作区 | 干净（`git status --short` 空，`git diff --stat` 空） |
| 分支 | `main`，与 `origin/main` 一致 |

---

## 一、环境记录

| 项 | 值 |
|---|---|
| OS | `Windows-10-10.0.19045-SP0` |
| Python | 3.13.7 |
| pytest | 9.1.1 |
| httpx | **0.28.1** |
| httpcore | **1.0.9** |
| h11 | 0.16.0 |
| anyio | 4.15.1 |
| pytest 参数 | `-q --strict-markers`（无 xdist，不并行） |

### 代理变量（脱敏）

| 变量 | 状态 |
|---|---|
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` | **未设置** |
| `NO_PROXY` | **未设置** |
| `http_proxy` / `https_proxy` / `all_proxy` / `no_proxy` | **未设置** |

**关键**：环境变量全部为空，因此任务书 §2.2 的"环境变量代理"假设不成立。
真正的代理来源见 §四。

---

## 二、失败复现（完整 traceback 要点）

命令：
```
python -m pytest -vv -s --tb=long "tests/test_vision_provider.py::test_status_classification[408-MODEL_TIMEOUT]"
```

结果：**1 failed**，稳定复现。

```
mock_server = <test_vision_provider.mock_server.<locals>.Control object>
status = 408, expected = 'MODEL_TIMEOUT'

    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
>   assert ei.value.code == expected
E   AssertionError: assert 'MODEL_NETWORK_ERROR' == 'MODEL_TIMEOUT'
```

| 问题 | 答案 |
|---|---|
| 失败发生在哪个环节 | **断言**（不是请求发送、不是响应接收） |
| 实际 `ModelError.code` | `MODEL_NETWORK_ERROR` |
| 实际 `ModelError.status` | 非 408（走的是传输层异常分支，不带 status） |
| mock server 是否收到请求 | **是**，path `/v1/chat/completions`，1 次 |
| mock server 实际发出的 status | **408**（原始 socket 验证） |

---

## 三、排除过程（每一项都有实验证据）

### 3.1 分类函数是否正确？——正确，不是根因

`classify_status(408)` 返回 `MODEL_TIMEOUT`，与任务书事实一致。**未修改**。

### 3.2 是否走了传输层异常分支？

Provider 在 `httpx.post` 抛 `httpx.HTTPError` 时归类为 `MODEL_NETWORK_ERROR`。
`MODEL_NETWORK_ERROR` 正是该分支的产物 → **请求没有返回 Response 对象**。

### 3.3 状态码特例扫描

用同一 mock，对 13 个状态码逐一调用 `httpx.post`：

```
400: HTTP 400     401: HTTP 401     403: HTTP 403     404: HTTP 404
408: RAISED RemoteProtocolError
409: HTTP 409     429: HTTP 429     500: HTTP 500     502: HTTP 502
503: HTTP 503     504: HTTP 504     505: HTTP 505     511: HTTP 511
```

**只有 408 抛错**。其余全部正常。

### 3.4 是服务端问题吗？——不是

原始 socket 直接读取 408 响应字节：

```
b'HTTP/1.1 408 Request Timeout\r\nServer: BaseHTTP/0.6 Python/3.13.7\r\nDate: ...\r\n
  Content-Type: application/json\r\nContent-Length: 25\r\n\r\n{"error":{"message":"x"}}'
```

**服务端响应完全合法且完整**。

### 3.5 是 httpx 还是 httpcore？——httpx

同一 408 响应：

| 调用方式 | 结果 |
|---|---|
| `httpcore.ConnectionPool().request(...)` | **OK status=408** |
| `httpx.post(...)` | **RAISED RemoteProtocolError** |

`httpcore` 正常，问题在 httpx 层。

### 3.6 是连接复用/keepalive 吗？——不是

| httpx 选项 | 结果 |
|---|---|
| 默认 | RAISED |
| `max_keepalive_connections=0` | RAISED |
| `http2=False` | RAISED |
| `Connection: close` 头 | RAISED |
| **`trust_env=False`** | **OK status=408** |

`trust_env=False` 是唯一能修复的开关 → 问题与环境变量/系统代理读取有关。

### 3.7 mock server 共享状态？——不是根因，但确实存在风险

`_Handler.responder` / `_Handler.seen` 是类级共享状态，并行或交错测试会串扰。
这一点**本轮另外加固**（任务书 §2.3），但它与 408 失败无关：单用例、无并行、
且 mock 每次都收到了正确请求。

---

## 四、根因

**Windows 系统代理（注册表）被 httpx 读取，loopback 请求被送往代理，代理破坏 408 响应。**

证据链：

1. 环境变量代理全部未设置；
2. httpx 在 `trust_env=True`（默认）时调用 `get_environment_proxies()`，
   Windows 上该函数经 `urllib.request.getproxies()` 读取**注册表**：

   | 注册表项 | 实际值 |
   |---|---|
   | `ProxyEnable` | `1` |
   | `ProxyServer` | `127.0.0.1:<proxy-port>` |
   | `ProxyOverride` | `localhost;127.*;...;<local>` |

3. httpx 得到的代理映射（脱敏）：

   ```
   'http://'  -> http://127.0.0.1:<proxy-port>
   'https://' -> http://127.0.0.1:<proxy-port>
   ```

   **注意：httpx 没有应用 `ProxyOverride` 中的 `127.*` 绕过规则**
   （该规则是 Windows/WinINET 语义，Python 的 `getproxies()` 不解析它）。

4. 因此 `http://127.0.0.1:<port>` 的请求被送往 `127.0.0.1:<proxy-port>` 代理；
5. 代理把请求**转发到了 mock server**（所以 mock 的 `calls` 断言一直通过），
   但代理在回传 408 时中断/破坏了连接；
6. 客户端看到的是"服务端断开"，httpx 抛 `RemoteProtocolError`；
7. Provider 归类为 `MODEL_NETWORK_ERROR`。

实测对照（同一次运行）：

```
trust_env=TRUE : RemoteProtocolError | mock saw: ['/v1/chat/completions']
trust_env=FALSE: HTTP 408           | mock saw: ['/v1/chat/completions']
```

**两种情况下 mock 都收到了请求** —— 这正是本缺陷隐蔽之处：
"请求到达了 mock" 无法证明"响应没有被中间层破坏"。

### 为什么 CI 通过

GitHub Actions 的 runner 没有配置系统代理（注册表为空、环境变量为空），
`get_environment_proxies()` 返回空映射，loopback 直连，408 正常返回。

---

## 五、修复方案（最小、仅测试范围）

1. **测试内**让 loopback 请求绕过任何代理：mock server fixture 显式提供
   `trust_env=False` 的客户端配置，并断言 loopback 不被代理接管；
2. **不改生产代码的代理支持**（生产仍尊重 `trust_env`，用户需要代理访问云端模型）；
3. 若测试辅助函数构造 `httpx` 请求，仅在**指向 loopback** 时禁用 env；
4. 保留并恢复环境变量，不要求用户改机器设置。

### 不采用的方案

| 方案 | 拒绝理由 |
|---|---|
| 改预期为 `MODEL_SERVER_ERROR` | 掩盖问题，任务书明令禁止 |
| skip/xfail | 任务书明令禁止 |
| 全局关闭 `trust_env` | 破坏生产代理支持（任务书 §6.5） |
| 让用户关闭系统代理 | 任务书 §8 明令禁止要求用户改全局设置 |
| 增加 timeout | 与根因无关 |

---

## 六、修复后必须满足

- HTTP 408 用例断言：`code == MODEL_TIMEOUT`、`status == 408`、`calls == 1`、path 正确；
- 传输层 `ReadTimeout`/`ConnectTimeout` → `MODEL_TIMEOUT`，独立保留；
- `ConnectError` → `MODEL_NETWORK_ERROR`；5xx → `MODEL_SERVER_ERROR`；
- 目标用例本机 20/20；Provider 文件 5/5；后端完整测试无该失败；
- 不新增 skip/xfail，不放宽既有断言。
