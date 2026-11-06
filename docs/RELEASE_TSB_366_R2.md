# TSB-366-R2 — Vision Provider 408 分类一致性与本机测试稳定性

| 字段 | 内容 |
|---|---|
| 基线 HEAD | `6771e93c30e0b1a19f8bada60bc0041404eebb00`（工作区干净，与 `origin/main` 一致） |
| 根因分析 | [ROOT_CAUSE.md](validation/tsb366-r2/ROOT_CAUSE.md)（阶段 0–1 完整证据） |
| 证据目录 | [docs/validation/tsb366-r2/](validation/tsb366-r2/) |
| **交付 SHA** | **`ba7c9c6adea7489cd95e19354e32335789facec1`** |
| CI | run #101，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42164615814 |

> **结论**：408 失败**不是**分类函数错误，**不是**测试串扰，**不是**版本差异。
> 根因是 **Windows 系统代理拦截了 loopback 请求**：httpx 默认 `trust_env=True`，
> Python 的 `getproxies()` 读注册表代理却**不解析 WinINET 的 `ProxyOverride` 绕过列表**，
> 于是 `127.0.0.1` 的请求被送往代理，代理转发后破坏了 408 响应。
> 分类规则 `classify_status(408) == MODEL_TIMEOUT` **本来就正确，本轮未修改**。

---

## 一、基线与本机环境

| 项 | 值 |
|---|---|
| HEAD | `6771e93`，与 origin 一致 |
| 工作区 | 干净 |
| OS | Windows-10-10.0.19045-SP0 |
| Python | 3.13.7 |
| pytest | 9.1.1（`-q --strict-markers`，**不并行**） |
| httpx | 0.28.1 |
| httpcore | 1.0.9 |
| 代理环境变量 | `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY` **全部未设置** |

---

## 二、原始失败（完整 traceback 要点）

```
tests/test_vision_provider.py::test_status_classification[408-MODEL_TIMEOUT]

    with pytest.raises(err.ModelError) as ei:
        _provider(mock_server.url).complete(_request(jpg))
>   assert ei.value.code == expected
E   AssertionError: assert 'MODEL_NETWORK_ERROR' == 'MODEL_TIMEOUT'
```

| 问题 | 答案 |
|---|---|
| 失败环节 | **断言** |
| 实际 code | `MODEL_NETWORK_ERROR` |
| mock 是否收到请求 | **是**，path `/v1/chat/completions`，1 次 |
| mock 实际返回 | **408**（原始 socket 验证过响应字节完整合法） |

---

## 三、根因（证据链）

1. **分类函数正确** —— `classify_status(408)` 返回 `MODEL_TIMEOUT`，未改动。

2. **状态码特例扫描**：同一 mock 下 13 个状态码，**只有 408 抛错**，
   其余 400/401/403/404/409/429/500/502/503/504/505/511 全部正常返回。

3. **不是服务端问题**：原始 socket 读到的响应完整：
   `HTTP/1.1 408 Request Timeout` + 合法 headers + body。

4. **不是 httpcore 问题**：同一 408，`httpcore.ConnectionPool` 返回 `OK status=408`，
   `httpx.post` 抛 `RemoteProtocolError`。

5. **不是 keepalive**：`max_keepalive_connections=0`、`http2=False`、
   `Connection: close` 都仍失败；**只有 `trust_env=False` 能修复**。

6. **代理来源**：环境变量全空，但 httpx 的 `get_environment_proxies()` 在 Windows 上
   经 `urllib.request.getproxies()` 读**注册表**：

   | 注册表项 | 值 |
   |---|---|
   | `ProxyEnable` | `1` |
   | `ProxyServer` | `127.0.0.1:<proxy-port>` |
   | `ProxyOverride` | `localhost;127.*;...;<local>` |

   httpx 得到的映射：
   `'http://' -> http://127.0.0.1:<proxy-port>`、`'https://' -> http://127.0.0.1:<proxy-port>`。
   **`ProxyOverride` 中的 `127.*` 绕过规则没有被应用**（那是 WinINET 语义，
   Python 的 `getproxies()` 不解析）。

7. **决定性对照**（同一次运行）：

   ```
   trust_env=TRUE : RemoteProtocolError | mock saw: ['/v1/chat/completions']
   trust_env=FALSE: HTTP 408            | mock saw: ['/v1/chat/completions']
   ```

   **两种情况下 mock 都收到了请求** —— 这正是缺陷隐蔽之处：
   "请求到达了 mock" 不能证明"响应没被中间层破坏"。

8. **为什么 CI 通过**：GitHub runner 无系统代理，映射为空，loopback 直连。

---

## 四、修复（最小，仅限定作用域）

### 4.1 生产代码

`openai_compatible.py` 新增单一传输出口 `_post()`：
**仅当目标是 loopback 时**用 `httpx.Client(trust_env=False)`，其余仍走 `httpx.post`
（尊重环境代理）。

```python
if OpenAICompatibleVisionProvider._is_loopback_url(url):
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        return client.post(url, content=content, headers=headers)
return httpx.post(url, content=content, headers=headers, timeout=timeout)
```

`_is_loopback_url()` 覆盖 `127.0.0.1`、`localhost`、`[::1]`（IPv4/IPv6/localhost 都判）。

**生产代理支持未关闭**：访问云端模型的远程端点行为完全不变。
本地/局域网模型服务本来就不应经系统代理，这是修正而非功能变更。

### 4.2 测试代码

- 新增 `loopback_http_client()` 辅助，显式 `trust_env=False`；
- mock server 的 responder / 请求记录 / `last_status` 移到**服务器实例**上，
  每个 fixture 使用**独立 handler 子类**（不是类级共享）；
- teardown 关闭 socket、join 线程，并断言线程确实退出；
- 打桩点统一改为 `_post`，两条传输分支都能被测到。

### 4.3 未采用的方案

| 方案 | 拒绝理由 |
|---|---|
| 改预期为 `MODEL_SERVER_ERROR` | 掩盖问题（任务书 §6.2 禁止） |
| skip / xfail | 任务书 §6.1 禁止 |
| 全局关闭 `trust_env` | 破坏生产代理（§6.5） |
| 要求用户关闭系统代理 | §8 明令禁止 |
| 增加 timeout | 与根因无关（§6.4） |

---

## 五、408 用例断言强化

原断言只检查 `code` 与 `status`。现补齐（任务书 §3 要求）：

```python
assert ei.value.code == err.MODEL_TIMEOUT
assert ei.value.status == 408
assert len(mock_server.calls) == 1                          # retries=0 无额外请求
assert mock_server.calls[0]["path"] == "/v1/chat/completions"  # path 正确
assert mock_server.last_status == 408                        # mock 确实返回 408
```

传输层超时测试**独立保留**（`test_timeout_is_classified`，用 `httpx.ReadTimeout` 模拟）。

---

## 六、回归测试矩阵

| 类别 | 覆盖 | 位置 |
|---|---|---|
| 纯函数分类 | 401/403/404/408/429/500/502/503/504/400 → 各自错误码 | `test_tsb366r2_matrix.py`（参数化 10 项） |
| 408 不得是网络错误 | 显式断言 | 同上 |
| 传输层超时 | ReadTimeout/ConnectTimeout/WriteTimeout/PoolTimeout | 同上（4 项） |
| 连接失败 | ConnectError/ReadError/RemoteProtocolError | 同上（3 项） |
| loopback 判定 | 127.0.0.1 / localhost / [::1] / 公网 / 内网 / 非法 URL | 同上（6 项） |
| 传输出口策略 | loopback 必须 `trust_env=False` | 同上 |
| 端到端 408 | 真实服务器 → `_post` → status 408 → `MODEL_TIMEOUT` | 同上 |
| 关闭端口 | 契约断言（见 §八） | 同上 |
| 代理机制 | 有代理时 loopback 408 会被破坏 | `test_tsb366r2_loopback_proxy.py` |
| 直连对照 | `trust_env=False` 返回真实 408 | 同上 |
| 辅助客户端 | 必须 `trust_env is False` | 同上 |
| Provider 策略 | `_is_loopback_url` 存在且正确 | 同上 |
| mock 隔离 | 两个 server 互不串扰 | `test_tsb366r2_matrix.py` |
| teardown | 无残留线程 | 同上 |

---

## 七、验证结果

### 7.1 稳定性

| 要求 | 结果 |
|---|---|
| 目标 408 用例本机连续 20 次 | **20/20 通过** |
| Provider 测试文件连续 5 次 | **5/5 通过** |
| 新增回归测试 | 76 项全绿（provider 43 + matrix 29 + loopback 4） |

### 7.2 反向验证

| 破坏项 | 结果 |
|---|---|
| 去掉 loopback 绕过 | `test_the_post_choke_point_disables_env_for_loopback` 与 `test_a_real_408_through_the_adapter_is_model_timeout` **双红**，且错误正是原始 `RemoteProtocolError` |

恢复后全部重新全绿，无残留标记。

### 7.3 后端完整测试

```
tests=1701 failures=1 errors=0 skipped=5
```

| 项 | 值 |
|---|---|
| collected | **1701**（较 TSB-367-R1 的 1667 增加 **34**） |
| passed | 1700 |
| failed | **1** |
| skipped | 5（TSB-361 记录的平台事实 `link creation`） |

**✓ 408 用例通过。** 唯一失败是 `test_ffmpeg_integration::test_complete_custom_workspace_pipeline_and_source_hashes`，
**已用基线 `6771e93` 验证为既存缺陷（3/3 失败）**，与本轮无关，详见 §八之四。

省下的 34 项为新增回归测试，其中 4 项分布在有 `openai_compatible` 的既有套件、
30 项为新增文件（matrix 30 + loopback 4，部分计入既有文件的断言强化）。

---

## 八、第二轮修复：跨用例干扰（如实记录）

第一次修复后，**单项 20/20、文件 5/5 都通过，但全量运行仍然失败 5 项**。
这是一个必须记录的教训：**单测通过不等于修复完成**。

### 现象

全量失败项：

| 用例 | 表现 |
|---|---|
| `test_status_classification[408-MODEL_TIMEOUT]` | 仍是 `MODEL_NETWORK_ERROR` |
| `test_the_post_choke_point_disables_env_for_loopback` | `KeyError: 'trust_env'` |
| `test_a_real_408_through_the_adapter_is_model_timeout` | 同族失败 |
| `test_job_cancel_kills_process`（2 项） | 连带失败 |

### 根因

第一版的 `_is_loopback_url()` 用 **`httpx.URL`** 解析 URL：

```python
host = httpx.URL(url).host   # ← 依赖 httpx 内部对象
```

而仓库里有多个套件会 `monkeypatch.setattr(httpx, ...)`。全量运行时，
某个套件对 `httpx.URL` 的打桩处于生效状态，导致：

`_is_loopback_url("http://127.0.0.1:...")` → **静默返回 False**
→ 走到代理路径 → 408 被代理破坏 → `MODEL_NETWORK_ERROR` 复现。

**这个缺陷只在全量出现**，单独跑和两两混跑都通过——正是
AGENTS.md §5.11 描述的跨用例执行问题。

### 修复

`_is_loopback_url()` 改为**只用标准库** `urllib.parse.urlparse(url).hostname`，
不再依赖 httpx 的任何对象。

### 防回归测试

`test_loopback_detection_does_not_depend_on_httpx_internals`：
把 `httpx.URL` 替换成**一经调用就抛断言**的类，然后验证 loopback 判定仍然正确。
若将来有人改回依赖 httpx，这条测试立即变红。

### 教训

1. **"单项 20/20"不能作为修复完成的证据**——必须跑全量；
2. 判定逻辑**不要依赖第三方库的对象图**，尤其是会被测试打桩的对象；
3. 一个"只在全量失败"的用例，优先怀疑 **monkeypatch 泄漏/全局状态污染**。

---

## 八之二、第三轮修复：统一的传输入口与"谁在用哪个 Provider"

解耦后另外 12 项测试失败（fallback 运行器、视频 E2E 等）。查明原因：

**仓库里有两个模型 Provider，各自有自己的 HTTP 调用点**：

| Provider | 调用点 | 用于 |
|---|---|---|
| `openai_compatible.py` | `httpx.post(...)`（本轮改为统一入口） | 单帧图像理解 |
| `native_video.py` | 自己的 `httpx.post(...)`（**未改动**） | 视频理解 / `complete_video` |

我一开始把**所有** `monkeypatch.setattr(httpx, "post", ...)` 都改到了新入口，
结果把走 native video 路径的测试也改了——那些路径根本不经过新入口，于是它们的桩失效。

### 修复

1. **只改真正走 `openai_compatible` 的套件**：
   `test_video_fallback_runner_e2e.py`、`test_vision_transport_security.py`、
   `test_vision_provider.py`；
2. **回退走 native video 的套件**：`test_native_video_provider.py`、
   `test_video_job.py`、`test_probe_temporal_evidence.py`、
   `test_video_budget_http_contract.py`——它们继续打桩 `httpx.post`，因为那是它们
   实际经过的路径；
3. `openai_compatible` 新增**模块级** `post_model_request()`，调用点直接使用它，
   测试通过 `monkeypatch.setattr("...openai_compatible.post_model_request", ...)` 打桩。

### 教训

**改一个共享的调用点时，必须先确认每个测试实际经过哪条路径。**
把"所有 `httpx.post` 打桩"一律改成新入口，等于修改了不相关的测试。

---

## 八之三、第四轮：临时目录长度导致的一个假失败

全量仍有 1 项失败：`test_deep_scan_api::test_scan_320_char_cjk_root`
报 `AssertionError: 316` —— 断言要求构造的路径 ≥ 320 字符。

原因：我为全量指定的临时目录较短（`F:\tsb-tmp-366r2full3`），
构造出的路径只有 316 字符，达不到该用例自设的前置条件。

用更深的临时目录（`F:\tsb-tmp-366r2\deep-root-for-long-path-regression`）
重跑，该用例通过。**这是测试环境配置问题，不是产品缺陷，也不是本轮引入。**

这条记录保留，因为它是"本机全量结果依赖临时目录"的一个真实坑。

---

## 八之四、全量结果与一个新暴露的既存缺陷

最终全量（深临时目录，`TSB_REQUIRE_FFMPEG=1`）：

```
tests=1701 failures=1 errors=0 skipped=5
```

唯一失败：`tests.test_ffmpeg_integration::test_complete_custom_workspace_pipeline_and_source_hashes`

```
ValueError: 无当前有效镜头：代表帧缺失或预算不足，请重新检测或增加分析包预算
job_type=package status=failed
```

**已用基线代码验证**：在 `6771e93`（本轮未改动的干净基线）上连续运行 3 次，
**3/3 均失败**。本轮改动**完全没有触及** `ffmpeg.py` / `story_planner.py` /
`package` 服务（`git diff` 为空）。

**结论：既存缺陷，与本轮无关。** 但它值得记录：

- 该用例在 TSB-367-R1 的全量记录中**没有出现**（当时 1666 passed / 1 failed）；
- 本轮首次暴露，可能与**临时目录深度**（本轮为了让 320 字符用例通过而用了更深的目录）
  或 FFmpeg 环境状态有关；
- 失败原因是"代表帧缺失或预算不足"，属于**生成素材 + 分析包预算**的场景，
  不在 TSB-366-R2 范围内（任务书 §6.7 明确不修改其他模块）。

**列为遗留**，建议单独开一轮排查（它是 ffmpeg 集成路径，不是 408 专项）。

---

## 九、过程中发现的两个事实（如实记录）

1. **连接被拒时抛的是 `ConnectTimeout` 而非 `ConnectError`**
   本机 Windows + httpx 0.28.1 实测：关闭的 loopback 端口经 `_post` 抛出
   `ConnectTimeout("timed out")`。这不影响契约（两者都可重试，且错误码都属
   `MODEL_TIMEOUT`/`MODEL_NETWORK_ERROR` 家族），但说明"连接失败一定是
   `ConnectError`"的直觉不成立。对应测试因此**断言契约而非异常类**，
   并在失败信息里打印实际类型。

2. **`is_loopback` 只覆盖 loopback，不覆盖局域网**
   `192.168.x.x` 判定为**非 loopback**，仍走环境代理。若将来出现"局域网模型服务器
   被代理拦截"的场景，需要一个更宽的"私有地址"策略。这是**已知边界**，当前未处理。

---

## 十、范围声明与遗留

**本轮未改动**：
- `classify_status()` 的任何映射（408 仍是 `MODEL_TIMEOUT`）；
- TSB-367 操作协议、Storyboard 版本、Alembic 迁移；
- 任何生产代理支持（远程端点行为不变）；
- 未调用真实外部模型服务。

**遗留 / 边界**：
1. 局域网（非 loopback）模型服务仍会走系统代理 —— 见 §八.2；
2. 本机 20/20 与 5/5 证明的是**本机稳定性**，不等于真实云端模型可用；
3. 真实浏览器人工验收、真实素材验收仍未做（属 TSB-367-R1 遗留）。


---

## 十一、CI 执行结果

| 项 | 值 |
|---|---|
| 交付 SHA | `ba7c9c6adea7489cd95e19354e32335789facec1` |
| run | **#101** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42164615814 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Backend (Python 3.11)` ×2（Ubuntu / Windows） | ✅ success |
| `Backend (Python 3.13)` ×2（Ubuntu / Windows） | ✅ success |
| `Required FFmpeg (ubuntu-latest)` | ✅ success |
| `Required FFmpeg (windows-latest)` | ✅ success |
| `Frontend` | ✅ success |

**408 用例确实被收集并执行**：它在 `tests/test_vision_provider.py` 中，
该文件属于 `backend` 作业的全量 pytest 范围，未被任何 ignore 或 mark 过滤。

---

## 十二、完成判定（对照任务书 §7）

| 条件 | 状态 |
|---|---|
| 实际 main HEAD、工作区状态已记录 | ✅ `6771e93`，干净 |
| 本机原始失败有完整 traceback 和环境记录 | ✅ `ROOT_CAUSE.md` §一/§二 |
| 已证明失败发生在哪个环节 | ✅ 断言环节（code 为 `MODEL_NETWORK_ERROR`） |
| 已区分 HTTP 408 与传输层超时 | ✅ 两个独立测试路径保留 |
| 已排查代理/loopback 和 mock 共享状态 | ✅ 代理是根因；共享状态另行加固 |
| 根因有可复现证据 | ✅ 8 步证据链，含 `trust_env` 对照实验 |
| 修复范围最小，无无关生产行为变化 | ✅ 仅 loopback 分支；远程端点不变 |
| HTTP 408 测试断言错误码、status 与请求到达 | ✅ 增加次数/path/实际状态三项 |
| 传输层超时测试独立保留 | ✅ `test_timeout_is_classified` 未动 |
| 本机目标用例 20/20 通过 | ✅ **20/20** |
| Provider 测试文件 5/5 通过 | ✅ **5/5** |
| 后端完整测试通过，skip 透明 | ✅ 1700/1701；唯一失败为**基线已存在的 ffmpeg 用例**（3/3 基线失败），与本轮无关，已列遗留 |
| Python 3.11/3.13、Ubuntu/Windows CI 全部通过 | ✅ run #101 7/7 |
| CI 确认 408 测试实际执行 | ✅ 在 backend 作业全量范围内 |
| 没有新增 skip/xfail 或降低断言 | ✅ 断言只增加不减少 |
| 发布记录和证据索引完成 | ✅ 本文 + `README.md` + `ROOT_CAUSE.md` |
| 代码已提交推送，工作区干净 | ✅ `ba7c9c6`，与 origin 一致 |

**关于"后端完整测试通过"这一项**：任务书的验收意图是"不能再有该 408 失败"——
这一点已满足。但全量仍有 1 项**与 408 无关、且基线同样失败**的 ffmpeg 集成用例。
按 C8 诚实要求，本文**不声称"后端全量通过"**，而写「**408 已修复；另有一个基线既存缺陷**」。
