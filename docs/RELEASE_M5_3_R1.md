# M5.3-R1 发布记录（批次 R1-A：TSB-333、TSB-334）

**基线 commit：** `main@6649b6f`（任务书审阅基线）
本批起点：`5f62709`（仅在其后追加了一篇 docs 记录，**未改动任何代码**，
见"范围声明"）。代码基线等价于 `6649b6f`。

**批次：** R1-A（TSB-333、TSB-334）
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**结论：** **代码级整改通过（本机），跨平台 CI 与发布门禁未完成。**
两条 P0 已修复并有 RED→GREEN 与反向验证证据；真实 PAI-EAS 视频能力
**仍 not verified**（外部部署状态，见第六节）。

---

## 一、TSB-333 — CI 跨平台失败（P0）

### 问题（RED）

`backend/tests/test_native_video_provider.py` 在**默认环境**
（`TRAVELSB_MODEL_MODE=off`，即生产默认值）下 30 项中有 **15 项失败**，
全部以同一句收场：

```
ModelError: MODEL_UPLOAD_DENIED: 全局开关未开启（TRAVELSB_MODEL_MODE=off）。
出于隐私默认不上传任何画面，请显式设为 on。
```

**根因**：这 15 项测的是协议注册、MIME 嗅探、载荷大小、HTTP 状态分类、
重试与密钥脱敏——它们都位于**上传授权边界之后**。开关关着时
`authorize_upload()` 提前拒绝，用例根本走不到自己要测的分支。
**失败原因不是被测逻辑错了，而是测试够不着它。**

实测对照（同一文件、同一命令、同一机器）：

| 环境 | 结果 |
| --- | --- |
| 默认（`.env` 中 `MODEL_MODE=off`） | `tests=30 failures=15` |
| `TRAVELSB_MODEL_MODE=on` | `tests=30 failures=0` |

与任务书所述 Ubuntu「主要 15 个 Provider failures」数目一致。

### 修复（GREEN）

在 `test_native_video_provider.py` 内新增 **autouse fixture**
`_enable_model_switch_for_this_module`，**仅对本模块**把
`settings.model_mode` 置 `on`，并由 `monkeypatch` 在 teardown 自动还原。

**为何不按 CI 全局打开**：任务书 §1.3 规则 4 明确禁止。
全局 `on` 会关掉阻止真实外发的守卫，等于用"移除被测行为"换绿灯。
本修复只让特定用例够得着目标分支，**生产默认与守卫完全不变**
（`ci.yml` 中 `TRAVELSB_MODEL_MODE` 出现次数实测 = **0**）。

### 反向验证（§3.2）

把 fixture 里的 `model_mode="on"` 一行删掉后：

```
FAILED tests/test_video_model_switch_scope.py::
  test_r02_the_enabling_fixture_lets_a_validated_call_reach_the_real_server
E  ModelError: MODEL_UPLOAD_DENIED: 全局开关未开启（TRAVELSB_MODEL_MODE=off）
```

恢复后 5 项全绿——证明该用例**真能捕获缺陷**，不是恒过的空断言。

### 新增契约测试 `tests/test_video_model_switch_scope.py`（5 项）

| 用例 | 守护的行为 |
| --- | --- |
| R02 | fixture 开启后**确实**发生 1 次真实 loopback HTTP 请求（不是 0 次） |
| R02b | 该开启**不依赖**进程环境变量 |
| R01 | 开关关着时 0 外发且抛 `MODEL_UPLOAD_DENIED` |
| R01b | `.env.example` 默认值仍为 `off` |
| R01c | `ci.yml` 中不得出现 `TRAVELSB_MODEL_MODE`（防"全局打开"式假绿） |

### 其余核对（诚实区分已验/未验）

- `test_native_video_probe.py` **32 项全通过**；dry-run 在 `env -i`
  剥离环境下仍退出 0 且不外发（R04/R05）。任务书所述 2 项 dry-run 失败
  为 **Windows 特有**，本机不复现，**未凭空断言已修**。
- `test_vision_commit_races.py` 连续 **5 次全绿**（16 项/次）。
  任务书称其为 **Ubuntu 偶发竞态**；本机无 Linux 环境，
  **本批未验证**，未标 xfail、未放宽 skip 白名单、未用 sleep 掩盖。

---

## 二、TSB-334 — 视频 Provider 工厂与真实调用链（P0）

### 问题（RED）— 比任务书描述的多一层

任务书定位：`video_analyze_task()` 用 `.get_provider()` 取到图片
Provider。**实测确认，并多发现一个缺陷**：

```
get_provider() -> OpenAICompatibleVisionProvider
hasattr(p, 'complete_video') -> False
=> provider.complete_video(...) -> AttributeError:
   'OpenAICompatibleVisionProvider' object has no attribute 'complete_video'
```

该 `AttributeError` 落到 `video_job.py` 的宽 `except Exception` 分支，
**而该分支记录的 `MODEL_FAILED` 在错误码注册表中根本不存在**：

```
grep -n "MODEL_FAILED" app/services/vision/video_job.py
  676:        code=MODEL_FAILED,          # 未 import、未定义
app/services/vision/errors.py 中没有 MODEL_FAILED
=> NameError: name 'MODEL_FAILED' is not defined
```

**两个缺陷叠加**的后果：视频任务一旦真正走到网络就失败，
**失败处理器自己也崩溃**，Job 既拿不到结果、也说不清原因，
且**不产生任何 observation**——一条"静默死掉的通道"。
既有测试全部漏过：门禁类用例在构造 Provider **之前**就返回了，
`test_video_job.py` 中真正跑任务的用例则把能力注入为
unverified/unsupported，同样到不了 Provider。

### 修复（GREEN）

1. **`vision/__init__.py` 新增独立工厂 `get_video_provider()`**，
   返回 `NativeVideoVisionProvider`。与 `get_provider()` 分离，
   使视频调用点**不可能**再继承到图片适配器。
   该工厂**只选适配器、不授予发送权**：授权上下文、已验证能力、
   协议注册、MIME 白名单、载荷上限仍全部由适配器自身强制。
2. **`video_job.py` 调用点改用 `get_video_provider()`**，
   并加一道显式检查：若返回对象没有 `complete_video`，立即以
   `MODEL_NOT_CONFIGURED` **大声失败**，而不是退化成静默死通道。
3. **修复幽灵错误码**：新增 `MODEL_PROVIDER_FAULT`，语义为
   "Provider 适配器逃逸出未预期异常（我方接线缺陷）"，与
   `MODEL_INVALID_OUTPUT`（模型返回内容不可用）**刻意区分**——
   避免把接线故障误读成"模型胡说"，让运维查错方向。

### 反向验证 R08（任务书 §6 要求）

把调用点临时改回图片工厂：

```
provider = get_provider()  # REVERSE-VERIFY: wrong factory
-> 5 failed（R07/R07b/R07c/R09/R10 全红）
```

恢复 `get_video_provider()` 后 **5 项全绿**。
证明这组测试**真的**守着接线，不是空断言。

### 新增集成测试 `tests/test_video_provider_wiring.py`（5 项）

真实链路：**真实 FFmpeg 派生短片 → 真实 loopback HTTP 服务 →
真实 `NativeVideoVisionProvider.complete_video` → Schema 校验 →
`SegmentVideoObservation` 入库**。

| 用例 | 断言（可观测差异，非"终态正确"） |
| --- | --- |
| R07 | 收到 `complete_video` 的类**就是** `NativeVideoVisionProvider` |
| R07b | loopback 服务**恰好收到 1 个**请求；请求体含 `data:video/mp4;base64,`；**不含** `image_url`；base64 解出的载荷 `ftyp` 魔数完好（真 MP4，不是抽帧） |
| R07c | **C1**：原片 size/mtime/SHA-256 前后一致 |
| R09 | `allow_insecure_http=False` + 非回环 http 端点 → **0 外发**且不发布 observation |
| R10 | 事件 `start_rel_sec=0.5` 映射为**源片绝对 1.5s**；`is_current` 唯一；`method=video_native`；`audio_analyzed` 被强制为 `False` |

实测 Job 统计（R10）：

```json
{"total":1,"done":1,"failed":0,"events":1,"clip_bytes":34750,
 "clip_seconds":3.0,"usage":{"calls":1,"prompt_tokens":11,"completion_tokens":22}}
```

---

## 三、验证结果（本机 macOS）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q --junitxml` | **1106 collected / 12 failed / 0 errors / 44 skipped** |
| 强制媒体作业（ci.yml 原样 56 文件） | `TSB_REQUIRE_FFMPEG=1 pytest` | **1022 collected / 12 failed / 0 errors / 44 skipped** |
| TSB-333 目标套件 | `pytest test_native_video_provider.py` | **30 / 0 failed**（修复前 15 failed） |
| TSB-334 新集成 | `pytest test_video_provider_wiring.py` | **5 / 0 failed**（反向断开后 5 failed） |
| 开关范围契约 | `pytest test_video_model_switch_scope.py` | **5 / 0 failed** |
| skip 守卫自检 | `tools/probe_skip_guard.py` | **VERDICT: PASS** |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **65 passed** |

### 关于那 12 项失败（必须说清，否则会被误读为回归）

**全部集中**于 `tests/test_video_clip.py`，根因是**本机 ffmpeg 构建缺
`drawtext` 滤镜**（该滤镜需编译期 `libfreetype`；Homebrew 的 ffmpeg
formula 依赖列表中**没有** freetype，故任何 Homebrew ffmpeg 9.0.2 都缺）。

- `drawtext` 在 `backend/app/` 中**零引用**，仅测试夹具用于合成素材；
- 反证：`--ignore=tests/test_video_clip.py` 时
  **1058 collected / 0 failed**，即这 12 项是唯一阻塞点；
- CI 用 Ubuntu apt / Windows 自带件，二者均带 `drawtext`，不受影响。

完整分析与复现命令见
[docs/validation/v2.1.3/MACOS_PULL_NOTES.md](../validation/v2.1.3/MACOS_PULL_NOTES.md)。

### 数字口径（AGENTS.md §3.5）

`1106` / `1022` 均为 **collected（收集数）**，含 skip。
本机非跳过项为 `1106 - 44 = 1062`。**不得**把 collected 写成 "passed"。
上一轮曾犯此错，已在 `MACOS_PULL_NOTES.md` 更正。

---

## 四、模型端点变更（用户指定）

按用户指示把视频识别模型改为 **GLM-5.3-Flash**，写入 **gitignored 的
`backend/.env`**（`git check-ignore` 确认忽略；未提交、未写进任何 tracked 文件）：

| 键 | 值 |
| --- | --- |
| `TRAVELSB_MODEL_NAME` | `GLM-5.3-Flash` |
| `TRAVELSB_MODEL_BASE_URL` | `http://1643210566335864.../glm_53_flash_video_test_wzy/v1` |
| `TRAVELSB_MODEL_API_KEY` | **不记录**（仅存于本地 `.env`） |
| `TRAVELSB_MODEL_ALLOW_INSECURE_HTTP` | `true`（该端点仅提供 **http**） |
| `TRAVELSB_MODEL_MODE` | `off`（**保持隐私默认不变**） |

**已核实不泄露**：`git grep` 确认 **0 个** tracked 文件含该 Key 或端点主机名；
`GET /api/system/config` 返回 `{"model_name":"GLM-5.3-Flash",
"model_configured":true}`，**不含** Key 与主机名。

### 能力实测（外部部署状态，不是代码问题）

| 探测 | 结果 |
| --- | --- |
| `GET /v1/models`（首次） | **HTTP 200**，返回 `{"id":"GLM-5.3-Flash","owned_by":"vllm"}` |
| 文本 chat（随后） | **HTTP 503** `upstream connect error ... connection termination` |
| 视频 chat | **HTTP 503**（同上，非 `At most 0 video(s)`） |
| `GET /v1/models` 连续 11 次重试 | **全部 503** |

**判读**：文本与视频**同样**返回 503，且 `/models` 由 200 转为持续 503，
说明是**网关/服务端不可用**，**不能**据此判定"支持或不支持视频"。
与旧端点 `At most 0 video(s)`（明确的能力上限为 0）是**不同性质**的结论。

因此本批**未**取得 `verified_native_video` 证据，视频通道按设计
**仍被拒绝**（`unverified`），真实视频能力**仍未验证**。

---

## 五、范围声明

- **未改动**原片、既有测试的断言、既有文档数字、M5.1/M5.2 的
  Prompt / schema / 检测算法、人工字段，**未开发 M6**。
- **未修改**生产默认 `TRAVELSB_MODEL_MODE=off`，**未**在 `ci.yml`
  设置全局开关（实测 0 处），**未**放宽 skip 白名单，
  **未**使用 `verify=False`，**未**删除任何文件。
- `ci.yml` 仅**新增 2 行**测试文件登记（本批新增的两个套件）。
- 本批起点 `5f62709` 相对 `6649b6f` 仅新增一篇 docs
  （`MACOS_PULL_NOTES.md`），**未触碰任何源码**，故代码基线等价。

新增文件：
`backend/tests/test_video_model_switch_scope.py`、
`backend/tests/test_video_provider_wiring.py`、本记录。

---

## 六、尚未完成 / 未验证（诚实列出）

1. **跨平台 CI 未取得绿灯** —— 本批全部验证在 macOS 本机完成。
   任务书要求 Windows Py3.11/3.13、Ubuntu Py3.11/3.13 共 7 个作业
   success 才算代码级放行；**本机无法替代**。特别是：
   - 任务书所述 **Windows 17 failures（含 2 个 dry-run 子进程用例）**
     本机不复现，**未验证**；
   - `test_vision_commit_races` 的 **Ubuntu 偶发竞态**本机无 Linux
     环境，**未验证**（5 次本机全绿不构成证据）。
2. **本机 12 项 `drawtext` 失败**未消除（环境限制，非代码）。
   是否为缺失滤镜加 `skipif` 守卫属**产品决策**；
   若加，必须同步登记到 `conftest._EXPECTED_SKIP_REASONS`，
   否则在 `TSB_REQUIRE_FFMPEG=1` 下会被判为意外 skip 而使作业失败。
3. **真实 PAI-EAS 视频能力仍未验证** —— 端点现持续 503（第五节）。
   需部署所有者恢复服务后重新探测。**不得**用本批的 loopback Mock
   通过冒充"真实视频可用"。
4. **R1-B/336 的现象已提前暴露** —— TSB-334 的集成测试已实测到
   `STAGING_NOT_OWNED` 清理失败（Job failures 中出现
   `error_code: STAGING_NOT_OWNED`），与任务书 TSB-336 对
   `clip.path.parent.parent` 的预判一致。**本批未修**（属 R1-B），
   但已有可复现的观测证据。
5. **TSB-335 的能力闭环未实现** —— `current_capabilities()` 仍固定返回
   `unverified`；本批未改（属 R1-B）。
6. **A/B 真实标注 0、C 臂不可用**，不能给"视频优于抽帧"结论。

---

## 七、结论措辞

本批达到的是**代码级整改通过（本机证据充分）**；
**跨平台 CI 绿灯、真实端点视频能力、发布门禁均未完成**。

按任务书 §8.2，**不得**宣称"真实 PAI-EAS 原生视频已可用"、
"视频比抽帧质量更好"或"M6 AI 导演已就绪发布"。
