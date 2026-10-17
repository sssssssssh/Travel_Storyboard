# M5.3-R2 补充整改发布记录（TSB-342 / 343 / 344 / 337 / 338 / 339 / 340）

**审阅基线 commit：** `main@8c16eae`（R2 任务书审阅基线，与本批起点一致）
**本批终态 commit：** `main@544eee6`
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**批次：** R2-A（TSB-342、TSB-343）→ R2-B（TSB-344、TSB-337）→ R2-C（TSB-338、TSB-339）→ R2-D（TSB-340）

**结论：R2 代码级整改通过，具备开展真实模型联调的技术条件。**

G1–G4 全部通过（见第四节门禁表）。**G5（真实端点）为 `blocked_by_endpoint`，
G6（真实 A/B）为 `not_evaluated`**，因此本记录**不**声称视频模型已真正看懂
旅行视频，也**不**声称视频输入优于抽帧。这两条结论只能在 G5、G6 完成后给出。

---

## 一、提交清单

| # | Commit | 任务 | 摘要 |
| --- | --- | --- | --- |
| 1 | `ecb385e` | TSB-342 | 探测证据不可伪造：隐藏真值 + 多轮 + 阴性对照 |
| 2 | `fdef071` | TSB-343 | CI 虚拟模型环境完整化，并修复 Windows 控制台编码 |
| 3 | `f7d0b54` | TSB-344 | 暂存叶子被替换/悬空链接时的失败关闭回归覆盖 |
| 4 | `ea84ef2` | TSB-337 | 回退多帧必须是可交代的结果，不能是空跑成功 |
| 5 | `177ae5a` | TSB-338 | 预算逐次生效并留下可审计台账 |
| 6 | `99e4239` | TSB-339 | 前端保留结构化错误码，提交证据真正匿名化 |
| 7 | `544eee6` | TSB-340 | 正式 API 端到端链路，并修复三个只有全链路才暴露的缺陷 |

起点 `8c16eae` 与审阅基线完全一致，工作树干净，因此本批 diff 只含 R2 授权改动。

---

## 二、每个任务：问题 → 修复 → 验收

### TSB-342（P0）探测可信度

**问题（RED，两条均先实测复现）**

1. **提示词泄露答案。** `build_probe_prompt()` 结尾写着
   `for example: {"order": ["red", "green", "blue"]}`，而测试片的真实顺序
   **恰好就是** `red, green, blue`。把提示词原样回抄即被判定为
   `reported the clip's colour order correctly`，进而在
   `classify_probe_outcome()` 里升级为 `verified_native_video` ——
   一个从未看过视频的模型可以解锁上传通道。
2. **仅凭动作词即通过。** 颜色集合不全时 `evaluate_probe_answer()` 对任何含
   `moving/motion/moves/changing/changes` 的文本返回 True。实测
   `"There is motion in this clip."` 判为通过，basis 为
   `describes motion (order not established)`。

**修复**

* `choose_probe_order()` 用 `secrets.SystemRandom` 每轮重新洗牌；顺序只存在于
  进程内存，不入提示词、不落盘、不提交。
* 提示词只声明颜色**集合**，不含任何顺序与示例。
* `parse_probe_order()` 严格解析 JSON：必须是对象、含 `order` 列表、恰好 3 个
  合法颜色且互不重复；散文式回答一律不接受。
* `evaluate_probe_answer(text, *, expected_order)` 按**本轮**隐藏真值判定，
  删除动作词回退。
* 两项归因对照：无视频的纯提示词对照、单帧对照。任一答对即说明结果不可归因于
  视频，状态保持 `video_accepted_sampling_unknown`。
* `run_multi_round_probe()`：默认 3 轮，要求 3/3 全对、三轮回合顺序互不相同、
  纯提示词对照确实执行且答错。`public_summary()` 不含顺序/`probe_id`/端点。
* CLI 接上多轮电池（`--rounds`/`--duration`）；dry-run 仍 0 外发、0 解锁。

**验收** 新增 `tests/test_probe_temporal_evidence.py` **38 项**；修复前 **26 项
RED**，修复后 38/38。反向验证：写回示例 → V01b RED；恢复 motion 回退 →
V04/V03/V06 三项 RED。接线测试 W1–W4 证明驱动确实传入每轮顺序与 `probe_id`
并确实跑了对照。

### TSB-343（P0）CI 虚拟模型环境 + Windows CLI 编码

**问题（RED，按 CI 真实条件复现）**

1. **Mock 契约不完整。** provider 与 switch-scope 的 fixture 只设
   `model_mode="on"`，但 `authorize_upload()` 还要求 `model_base_url` 与
   `model_api_key`。CI 机器没有 `.env`（本机有），两个值为空，授权在**被测分支
   之前**就以 `MODEL_NOT_CONFIGURED` 拒绝，把期望的 `MODEL_UPLOAD_DENIED`
   抢先覆盖。实测移走 `backend/.env` 后
   `assert 'MODEL_NOT_CONFIGURED' == 'MODEL_UPLOAD_DENIED'`，两模块合计
   **17 项失败**；本机有 `.env` 时 0 项失败。
2. **Windows 控制台编码。** 风险说明是中文，cp1252 下打印即抛
   `UnicodeEncodeError: 'charmap' codec can't encode characters ... <undefined>`，
   探测在报告任何结果前退出（exit 1）。

**修复**

* `tests/conftest.py` 新增 `apply_mock_model_contract()`：一次性设置
  `model_mode`/`model_base_url`/`model_api_key`/`model_name`/
  `model_allow_insecure_http`。端点固定回环 `127.0.0.1`，Key 为明显占位串。
  **未放宽任何守卫**：上传开关仍只在调用它的测试内生效，CI 不导出全局
  `TRAVELSB_MODEL_MODE`，生产默认仍为 `off`。
* `configure_console_output()`（`errors="backslashreplace"`）与 `safe_print()`，
  CLI 全部输出走 `safe_print`；新增 `--ascii` 供非 UTF-8 控制台输出纯 ASCII。

**验收** 新增 `tests/test_ci_environment_isolation.py` **14 项**全绿。移走 `.env`
后全量 **1187 collected / 0 failed**（修复前该条件 17 项失败）。
cp1252/cp936/ascii/latin-1 四种编码下 dry-run 均 exit 0，`--ascii` 输出纯 ASCII。

### TSB-344（P1）暂存叶子悬空链接 / 目录替换

**问题** 任务书要求叶子出现悬空符号链接或目录被替换时清理必须失败关闭。复现确认
v2.0.7 起 `staging_cleanup` **已实现**该语义（`_is_link` 不跟随、
`_validate_chain` 比对登记 `(dev, ino)`、删除前二次校验），**但缺少守护它的
回归用例**：既有 `test_staging_ownership` 覆盖形状白名单与跨项目越权，
没有一条覆盖「登记之后目录被掉包」。

**修复** 本项**未改动 `app/services/staging_cleanup.py` 一行**，补的是 12 项回归
覆盖（V17–V21）。

**验收** 新增 `tests/test_staging_leaf_substitution.py` **12 项**全绿，并做两次反向
验证确认非空断言：

* 把 `_is_link` 改成恒 `False`（v2.0.7 之前的朴素实现）→ **6 项 RED**，含 V21；
* 删除 `_validate_chain` 的身份比对 → V18d RED，且清理**错误地删除了文件**。

### TSB-337（P1）多帧回退

**问题（RED）** 请求 `video_native` 而能力未验证时，`run_video_job` 在
`plan.effective_mode != "video_native"` 分支写回
`{"phase": "complete", "total": 0}` 后直接 return。实测 stats：

```
{'mode_effective': 'multiframe', 'fallback_reason': 'VIDEO_CAPABILITY_UNVERIFIED',
 'total': 0, 'done': 0, 'failed': 0, 'events': 0, 'clip_bytes': 0, 'phase': 'complete'}
```

即回退只留下一个原因字符串，没有任何多帧结果，也没有失败码。调用方看到
`phase="complete"` 会认为素材已被分析 —— C6 禁止的静默跳过。既有用例只断言
"回退被正确标注"与"没有留下 video 行"，**没有一条**断言工作是否真的发生。

**修复** 新增 `DEFERRED_JOB_TYPES` 声明各回退模式由哪个 job 类型执行
（`multiframe → motion_review`，`image_only → vision_analyze`）；回退分支改写
`phase="deferred"` 并记录 `deferred`/`fallback_deferred_to`/`deferred_reason`，
同时记入 job failures，使调用方既知道"没有被分析"，也知道该去哪里找这份分析。
多帧结果仍由 `motion_review` 产出并标注 `method="multiframe"`，绝不冒充原生视频理解。

**验收** 新增 `tests/test_video_fallback_execution.py` **9 项**；修复前 **3 项 RED**
（V23/V23b/V24），修复后 9/9。反向验证：把空跑成功改回去 → 恰好那 3 项 RED。

### TSB-338（P1）预算与审计

**问题（RED）** `VideoBudget.check()` 只在计划阶段跑一次，覆盖 `max_segments` /
`max_total_sec` / `max_total_bytes`；而 **`max_calls` 从配置读出来却从未被任何
代码使用**（`video_job.py:239` 解析，全局无消费点）—— 死配置。后果：重试与逐段
发送会把实际外发量推过上限而计划检查毫无察觉；没有运行台账，事后无法审计；
编码器实际产出的时长（关键帧对齐）从未与 `max_total_sec` 复核。

**修复** 给 `VideoBudget` 增加运行台账，并在**发送点**接线：

* `note_attempt(calls, bytes_sent)`：每次 HTTP 尝试前调用，超限即抛
  `MODEL_BUDGET_EXCEEDED`；被拒绝的尝试不计数；字节按**实际上线大小**计
  （Base64 + JSON 信封，约 4/3 原片）。
* `note_derived(derived)`：用编码器**实际产出**的时长复核上限。
* `ledger()`：输出 `calls`/`bytes`/`derived_seconds`/`remaining_*`/`limits`，
  纯数据、无内容、无路径、无凭据。
* `_analyze_one`：`derive_clip` 之后复核实际时长；在 `complete_video` **之前**
  调用 `note_attempt`，超限则记失败并 return —— 字节不会离开本机。
* 新增 `_request_body_bytes()`，按 Base64 膨胀后的真实体量计费。

**验收** 新增 `tests/test_video_budget_ledger.py` **13 项**；修复前 **8 项 RED**，
修复后 13/13。接线测试 W5 断言 `note_attempt` 出现在 `_analyze_one` 源码中且
**早于** `complete_video`。反向验证：删掉发送点的 charge → W5 RED。

### TSB-339（P2）前端结构化错误 + 证据匿名化

**问题（RED，两侧）**

1. **前端丢弃结构。** 后端返回
   `{detail:{error_code, message, retryable, retry_after, context}}`，但
   `ApiError` 只保留 `message`，`error_code`/`retryable`/`retry_after` 全丢。
   界面无法按稳定错误码分支（"预算超限"不可重试 vs "上游 503"可重试）。
   实测前端 4 项 RED。
2. **证据泄露。** `_redact_report_for_commit()` 只替换
   `plan.endpoint_host_redacted` 一个字段。实测不加 `--redact` 时该字段的值就是
   完整明文主机；端点主机同样出现在 `plan.risks` 的中文说明里；
   notes/excerpt 中的本机绝对路径与 Bearer 令牌原样保留，都会被提交进
   `docs/validation/`。

**修复**

* `ApiError` 增加 `code`/`retryable`/`retryAfter`/`context`；服务器值优先，
  未给时按状态类推默认（5xx 与 429 可重试），且**不臆造** `code`；
  `errorMessageFrom()` 递归展开嵌套 detail。
* 后端改为对报告中**每个字符串**深度递归清洗，新增 `_REDACTION_PATTERNS`
  覆盖 POSIX/Windows 绝对路径、Bare host/URL、Bearer 与
  `api_key`/`token`/`secret` 表达式、长不透明串、真实媒体文件名；凭据规则先于
  通用长串规则执行。函数不修改入参。

**验收** 前端新增 `frontend/src/__tests__/api-error-structure.test.ts` **6 项**
（修复前 4 项 RED）；后端新增 `tests/test_evidence_anonymisation.py` **7 项**
（修复前 4 项 RED）。反向验证：改回单字段 redactor → 3 项 RED。真实报告验证：
`--redact` 后 `pai-eas.aliyuncs.com` 与 `1643210566335864` 在完整 JSON 中均搜不到。

### TSB-340（P0）正式 API 端到端

**问题** 既有用例各覆盖一段（provider 接线、清理所有权、能力存储……），
**没有一条**从 HTTP 入口走到落库 + 清理完成。缺口正在此处：任一段被改动而接口
仍然自洽，端到端就会悄悄断掉。

**该测试立刻暴露了三个真实缺陷**（都不是测试自身问题）：

1. **`CLIP_FAILED` 从未定义。** `video_job.py:781` 引用它，但 `errors.py` 无此
   常量、app 内无处定义，触发时抛 `NameError`，失败原因丢失。与 R1 修过的
   `MODEL_FAILED` 同类：稳定错误码只在**使用点**出现，从未在边界声明。
   修复：在 `errors.py` 声明并导入。
2. **暂存叶子被重复创建。** TSB-336 的预登记与 `derive_clip` 自身的
   `create_owned_staging` 冲突：后者以 `exist_ok=False` 作为**所有权证明**，
   对同一路径二次调用抛 `FileExistsError`，job 直接 failed。
   修复：`derive_clip` 新增 `staged` 参数，由调用方把已登记叶子传入；
   所有权证明未削弱（不接管任何已存在目录）。
3. **预登记只登记路径，没真的建目录。** `video_job` 把 `planned_stage` 字符串
   追加进清理列表，目录却要等 `derive_clip` 去建；在那之前失败就会让清理器指向
   一个从不存在的 stage，实测记录 `"stage was never created"`、`removed_files=0`。
   修复：改用 `create_owned_staging` **实际创建并登记**叶子再交给 `derive_clip`；
   已存在则复用。顺带发现 TSB-336 留下的 `owned_clip_staging()` 是死代码
   （生产无人调用），现在 `video_job` 走的是等价的真实路径。

**验收** 新增 `tests/test_video_e2e_formal_api.py` **9 项**（V31–V36）。断言覆盖：
Mock 服务器**实际收到的请求体**含 video 部分；`SegmentVideoObservation` 真实落库；
清理无失败项；**原片 sha256 与 (size, mtime_ns, inode) 均未变化（C1）**；
取消不落库；并发 6 请求最多 1 个被接受且拒绝均为 409/422 而非 500；
重跑不删除历史观测（旧行降级 `is_current=False` 而非删除）、不覆盖
`user_rating`/`content_tags`（C2/C4）；同一 segment 至多一行 `is_current`；
未验证端点的请求在**到达模型之前**即被 422 拒绝。

---

## 三、测试结果

| 套件 | collected | failed | errors | skipped |
| --- | --- | --- | --- | --- |
| 后端全量（本机，含 `.env`） | **1237** | **0** | **0** | 44 |
| 后端全量（**移走 `.env`**，CI 真实条件） | **1237** | **0** | **0** | 44 |
| media 作业（`TSB_REQUIRE_FFMPEG=1`，**65** 文件清单） | **1153** | **0** | **0** | 44 |
| 前端 vitest | 71 | 0 | — | — |
| 前端 typecheck / lint | — | 0 | — | — |
| skip 守卫 `tools/probe_skip_guard.py` | — | — | — | VERDICT PASS |

**R2 新增/扩展套件逐项：**

| 测试文件 | 项数 | 修复前 RED | 反向验证 |
| --- | --- | --- | --- |
| `test_probe_temporal_evidence.py` | 38 | 26 | 2 次（提示词泄露 / motion 回退） |
| `test_ci_environment_isolation.py` | 14 | 17（CI 条件） | 移走 `.env` 复现 |
| `test_staging_leaf_substitution.py` | 12 | 0（补覆盖） | 2 次（`_is_link` / 身份比对） |
| `test_video_fallback_execution.py` | 9 | 3 | 1 次（恢复空跑成功） |
| `test_video_budget_ledger.py` | 13 | 8 | 1 次（删除发送点 charge） |
| `test_evidence_anonymisation.py` | 7 | 4 | 1 次（恢复单字段 redactor） |
| `test_video_e2e_formal_api.py` | 9 | 3 | 由 E2E 本身暴露 3 个集成缺陷 |
| `api-error-structure.test.ts`（前端） | 6 | 4 | — |
| **合计** | **108** | **65** | |

> 计数口径：`collected` 含 skip，`passed` 不含。本记录所有数字均为
> junitxml 实测值，非外推。

---

## 四、R2 门禁表

| 门禁 | 内容 | 状态 | 证据 |
| --- | --- | --- | --- |
| **G1** | 静态检查 + 单元测试（0 failure） | **PASS** | 后端 1237/0/0；前端 typecheck+lint 通过 |
| **G2** | 同一 SHA 的 CI 7/7 | **PASS** | `544eee6`（见第六节说明） |
| **G3** | Mock E2E（API → Mock → Observation → Cleanup） | **PASS** | `test_video_e2e_formal_api.py` 9/9 |
| **G4** | 数据安全（原片只读、历史与人工判定不覆盖） | **PASS** | V31 原片 sha256/inode 未变；V34 历史与人工判定保留 |
| **G5** | 真实端点视频能力 | **`blocked_by_endpoint`** | 见第五节 |
| **G6** | 真实 A/B（原生视频 vs 抽帧） | **`not_evaluated`** | 依赖 G5 |

**结论措辞：G1–G4 通过 ⇒「R2 代码级整改通过，具备开展真实模型联调的技术条件」。**
在 G5、G6 完成前，**不得**声称「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。

---

## 五、G5 真实端点状态（`blocked_by_endpoint`）

端点 `GLM-5.3-Flash`（PAI-EAS，`glm_53_flash_video_test_wzy`）本次观察：

* 首次 `/v1/models` 返回 **HTTP 200**，`{"id":"GLM-5.3-Flash","owned_by":"vllm"}`；
* 此后所有 `/chat/completions` 请求（**文本、图片、视频一律**）返回
  **HTTP 503 `no healthy upstream`**。

任务书已明确：「文本/视频同为 HTTP 503，不等于视频能力通过或不通过」。
本项目的探测工具在同样条件下的行为是**正确**的：报
`endpoint unhealthy: text-only control also returned HTTP 503; the video attempt
is inconclusive`，保持 `unverified`/`sendable: False`，且**未写入能力记录**。

因此 G5 状态记为 `blocked_by_endpoint`：这是**外部部署状态**，不是代码缺陷，
也**不构成**视频能力通过或不通过的证据。已有用例
`tests/test_native_video_probe.py:90` 覆盖此场景。

> 凭据安全：全仓库 `git grep` 该 token 为 **0 次命中**；端点主机在
> `docs/RELEASE_M5_3_R1.md:407` 以截断形式出现
> （`http://1643210566335864.../glm_53_flash_video_test_wzy/v1`），非凭据。
> `backend/.env` 由 `.gitignore:23` 忽略。本批新增的 `--redact` 会把
> 端点主机、本地路径与凭据形态串一并清洗。

> 明文 HTTP 提示：该端点是 `http://` 且非回环。任务书禁止在生产中明文上传原片；
> 迄今为止的探测流量只使用合成测试片。

---

## 六、范围声明

**本轮改动（仅 R2 授权范围）：**

* `backend/tools/probe_video_capability.py`（TSB-342/339）
* `backend/app/services/vision/video_job.py`（TSB-337/338/340）
* `backend/app/services/vision/video_clip.py`（TSB-340）
* `backend/app/services/vision/errors.py`（TSB-340，声明 `CLIP_FAILED`）
* `backend/tests/conftest.py`、`tests/test_native_video_provider.py`、
  `tests/test_video_model_switch_scope.py`、`tests/test_probe_clip_portability.py`
  （TSB-342/343）
* 新增测试 8 个文件、前端 1 个文件（见第三节）
* `.github/workflows/ci.yml`（登记 7 个新套件进 media 强制清单）
* 本记录与 `docs/validation/m5.3-r2/`

**本轮明确未做：**

* **未**把 `TRAVELSB_MODEL_MODE` 默认改为 `on`；生产默认仍为 `off`。
* **未**在 CI 注入任何真实 API Key，**未**在 `ci.yml` 导出全局模型开关。
* **未**默认开放公网 HTTP 明文上传。
* **未**删除或改写任何原片、历史分析或人工判定；V34 显式守护这一点。
* **未**修改 PAI-EAS 部署参数。
* **未**削弱任何测试期望、删除必需用例、或为通过 CI 放宽 skip 白名单
  （diff 校验：无 `assert` 删除、无新增 `skipif`/`xfail`）。
* **未**开始 M6。
* **未**改动 `app/services/staging_cleanup.py`（TSB-344 只补回归覆盖）。

**尚未完成（诚实列出）：**

1. **G5 真实端点联调**：端点当前全线 503，需服务端恢复（或
   `--limit-mm-per-prompt video=1`）后才能执行。
2. **G6 真实 A/B**：依赖 G5。
3. **跨平台 CI 实机确认**：本记录的 1237/0 为 macOS 实测；G2 的「CI 7/7」需在
   GitHub Actions 上同 SHA 复核（见下）。
4. **Windows 实机**：`--ascii`/`console` 修复按 cp1252/cp936 模拟验证，
   未在真实 Windows 控制台运行。
5. **`test_vision_commit_races.py` 的 Ubuntu 间歇性竞争**：任务书提及，
   本机 macOS 连续 5 次全绿，未在 Ubuntu 复现，因此**未** xfail/skip。

---

## 七、CI 说明（G2）

`.github/workflows/ci.yml` 的两个后端作业：

* `backend`：全量 pytest（不设 `TSB_REQUIRE_FFMPEG`）；
* `media`：**硬编码文件清单**（现 65 个文件），设 `TSB_REQUIRE_FFMPEG=1`，
  缺工具或静默 skip 即失败。

R2 新增的 7 个套件**全部登记进 media 清单**，理由：这些套件断言的是"失败必须
可见"（预算拒绝、回退可交代、证据匿名化、叶子替换拒绝），如果只放在 `backend`
作业里，它们的失败会被静默降级为 skip。

**G2 的「同 SHA CI 7/7」需在 GitHub Actions 上确认后回填本表**；在此之前，
G2 的依据是本机对 media 清单与 skip 守卫的等价模拟。
