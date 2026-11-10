# TSB-370 发布记录：真实素材 × 自建 PAI-EAS 模型

| 字段 | 内容 |
|---|---|
| 基线 | `4b156e4d915a4876a129e2f7ee5f5f6b4885643e`（与任务书核对一致） |
| 证据目录 | [docs/validation/tsb370/](validation/tsb370/) |
| **代码提交** | `252771514be22413000ff44cd1ce1b924a84cad1` |
| **最终 HEAD** | `d3378fdab3fca0307d1af2e14c4ee020f0375327` |
| **CI** | run #134，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42791564920 |
| 执行账本 | [REAL_MODEL_RUN_LEDGER.md](validation/tsb370/REAL_MODEL_RUN_LEDGER.md) |

---

## 一、最终门禁状态（分开陈述，不合并）

| 门禁 | 状态 | 依据 |
|---|---|---|
| **G0_CODE** | ✅ **PASS** | digest 绑定修复、授权/预算账本、真实 model adapter、全量回归 |
| **G1_STATIC_REAL** | ✅ **PASS** | **11 段真实静帧成功**，可追踪、内容具体 |
| **G2_MOTION_REAL** | ✅ **PASS** | **2 段真实多帧成功**，`motion_type=tracking`，摘要具体 |
| **G3_NATIVE_REAL** | ⚠️ **BLOCKED** | 探测遭遇端点 503；**文字对照也 503** → 端点故障，非能力结论 |
| **G4_MODEL_PLAN** | 🔴 **BLOCKED** | 真实请求已发出 2 次，但**因我的实现缺陷与 token 预算不足未产出草案** |
| **G5_QUALITY** | 🔒 **MANUAL_REVIEW_PENDING** | 未有人工逐帧观看 |
| **G6_RELEASE** | ⛔ **不关闭** | 本轮不做发布门禁 |

> **不得把 G1/G2 的成功推广为"AI 导演已可用"。**
> G4 未通过，所以「真正的模型驱动导演」在本轮**没有被验证**。

---

## 二、370-0：两个 P0 修复

### 2.1 前端 digest 从未传递（任务书 §2.2）

**修复前**：

```tsx
expected_input_digest: ctx.data?.coverage ? undefined : undefined   // 两个分支都是 undefined
```

**修复**：

1. 新增只读 `GET /api/projects/{id}/director/plans/input-status`，复用
   `build_planning_input()` 的同一套组装——返回的 digest 与下一次 preview 比对的
   **是同一个量**（用例 `test_input_status_digest_equals_the_one_the_preview_uses`）。
   该端点不创建 Storyboard/版本/Job。
2. 前端从该端点取 digest；**取不到时禁用生成**而不是无保护提交。
3. `max_candidates` 进入 digest，因此做成可见选项；input-status 查询 key 含
   `max_candidates`，保证读到的摘要与将要提交的参数一致。
4. 409 时**清除旧草案**（不让旧方案伪装成新方案）并给出"重新加载证据"入口。
5. 切项目改为 **keyed remount**，使"草案绝不跨项目"成为结构保证而非时序保证。

**反向验证**：把 `expected_input_digest` 改回 `undefined` →
`the preview request carries the digest from input-status` **精确失败**，恢复后通过。

### 2.2 端点从明文 HTTP 改为 HTTPS（额外发现）

配置原本是 `http://...pai-eas.aliyuncs.com`，且设了
`TRAVELSB_MODEL_ALLOW_INSECURE_HTTP=1`。

实测该端点 **443 端口支持 TLS 1.3**，`https://` 的 `/models` 返回 200。
已改为 HTTPS 并关闭不安全 HTTP 开关。

**这是本轮的安全改进**：素材不再经过明文通道。

---

## 三、370-A：隔离与预算

### 3.1 隔离库

| 项 | 值 |
|---|---|
| 位置 | 仓库外临时目录 |
| 复制内容 | Project / Asset / Segment / Frame **元数据** + 帧图副本 |
| 不复制 | 任何模型结果、人工标注、Storyboard 版本 |
| 初始证据 | **0**（静态/多帧/原生视频/Storyboard/Job/标注 均校验为 0） |
| 原片 | 以**只读绝对路径**引用，未复制、未改写 |
| 规模 | 6 Asset / 99 Segment / 128 Frame，5/6 源可访问 |

### 3.2 预算账本（新增）

`app/services/model_call_ledger.py`：

- 持久化到**仓库外**文件，进程重启不复位；
- **发送前**原子扣减，超时/500/取消都算一次；
- 单渠道上限与总上限一起检查，不能互相借用额度。

接入点：`post_model_request()` —— **唯一的 HTTP 发送口**。
另有 `native_video` 的 `attempt_guard` 走同一账本。

**渠道归属用 `ContextVar`**：各任务入口
（`vision_analyze_task` / `motion_review_task` / `video_analyze_task` / 策划器）
各自声明，避免一个进程里三种任务互相记错账。

---

## 四、370-B：真实静帧与多帧

### 4.1 实际调用

| 渠道 | 上限 | 实际成功 | 结果质量 |
|---|---:|---:|---|
| 静帧语义 | 12 | **11** | 具体主体与动作，非模板 |
| 多帧运动 | 4 | **2** | `tracking`，含时间点与路边物体 |

静帧样例（模型原文，脱敏）：

> `indoor, transport, urban, architecture` ·
> "woman in blue denim jacket walking toward camera" ·
> "travelers with rolling suitcases" ·
> "woman walking toward the camera with mouth open as if speaking or singing"

多帧样例：

> "The camera appears to be mounted in a vehicle moving forward along a city
> street. At 5.75s the view shows a roadside with a blue truck, vans, a dark SUV
> and a yellow bus"

### 4.2 失败如实记录

| 失败 | 次数 | 原因 |
|---|---:|---|
| `SOURCE_CHANGED` | 1 | 源文件不可访问（6 个源中缺失 1 个）——**正确拒绝** |
| `INSUFFICIENT_FRAMES` | 数段 | 多数镜头只有 1 帧，不足以判断运动 |

**多帧 2/4 是素材的真实上限**，不是额度不足。

### 4.3 覆盖率变化

| 项 | 隔离库前 | 隔离库后 | 原库 |
|---|---:|---:|---:|
| 有任意读数 | 0 | **13** | **10（未变）** |
| 静态读数 | 0 | 11 | 14（未变） |
| 多帧读数 | 0 | 2 | 10（未变） |
| 原生视频 | 0 | 0 | 0（未变） |

**原库计数完全未变**，隔离库的增长**不冒充**原库覆盖率变化。

---

## 五、370-C：原生视频（BLOCKED）

### 5.1 探测结果

| 调用 | 结果 |
|---|---|
| 视频探测（`video_url` + base64 data URI，14,340 B） | **503** `upstream connect error` |
| **对照**：同端点纯文字 | **503** `no healthy upstream` |
| `/models` 控制面 ×6（3 分钟） | 全部 **503** |

### 5.2 为什么这是"端点故障"而不是"不支持视频"

**对照实验是关键**：端点的纯文字请求同时 503。
若只看视频探测就下"模型不支持 native video"的结论，那是**错误归因**。

因此 `G3_NATIVE_REAL = BLOCKED_UNAVAILABLE`，不是 `BLOCKED_CAPABILITY`。

### 5.3 未做的事

- 未用抽帧冒充 native
- 未因 BLOCKED 而修改代码假装支持
- 未切换端点或重启 EAS 实例

---

## 六、370-D：模型策划（BLOCKED，但我有责任）

### 6.1 已完成的实现

| 交付 | 说明 |
|---|---|
| `director_plan_model.py` | 白名单投影 + 回复解析 + 服务端权威字段填充 |
| `ExternalPlannerProvider` | 从拒绝桩改为**真实受控 adapter**，无授权仍拒绝 |
| API | `planning_mode=model` 需要 `upload_acknowledged=true` + 全局开关 + 端点检查 |
| 错误分类 | 403 未授权 / 409 输入变化 / 422 输出非法或方案被拒 / 502 传输失败 |
| `VisionRequest.text_only` | 新增显式纯文字意图，使能力检查能区分"忘给图"和"文字调用" |

### 6.2 白名单保证（有测试）

| 不发 | 验证 |
|---|---|
| 图片 | `test_no_images_are_sent`：`sends_images=False` |
| 用户自由备注 | `test_user_notes_and_personal_data_never_leave`（备注含人名与电话，均不出现） |
| 绝对路径 | `test_paths_are_never_in_the_prompt` |
| 原始 provider payload | 不可达 |

### 6.3 服务端权威字段不可被模型覆盖

`test_server_fields_cannot_be_overridden_by_the_model`：模型试图设置
`project_id`/`input_digest`/`planning_mode`/`coverage` 全部被忽略。

### 6.4 全部既有守卫在模型路径上有效

| 守卫 | 用例 |
|---|---|
| `UNKNOWN_SEGMENT`（幻觉镜头） | `test_an_invented_shot_from_the_model_is_rejected` |
| `AUDIO_CLAIM_WITHOUT_ANALYSIS` | `test_a_sound_claim_from_the_model_is_rejected` |
| `SEMANTIC_UPGRADE` | `test_an_uncertain_upgrade_from_the_model_is_rejected` |
| `VARIANTS_NOT_DISTINCT` | `test_two_identical_model_variants_are_rejected` |
| `UNSUPPORTED_VISUAL_CLAIM` | `test_evidence_refs_for_ids_the_shot_lacks_are_dropped` |

### 6.5 为什么 BLOCKED —— 我的责任

真实调用共尝试 **2 次**，均未产出草案：

| 次 | 结果 | 原因 | 归属 |
|---|---|---|---|
| 1 | `MODEL_CAPABILITY_UNSUPPORTED` | Provider 要求至少一张图 | 架构约束，已用 `text_only` 合理扩展 |
| 2 | `MODEL_INVALID_OUTPUT`（reasoning 耗尽） | `max_tokens=4000` 不足 | 配置判断失误 |
| 3 | 请求已发出并计费 4,647 B | **`NameError: build_evidence_text`** | **我的实现缺陷** |
| 4 | `BUDGET_EXHAUSTED` | 上次误记到 `static` 桶 | 记账缺陷（已修） |

**第 3 次最严重**：真实请求发出了、token 消耗了，但结果因我的 bug 丢失。
这是本轮最直接的技术失误。

### 6.6 代码路径已用 Mock 验证正确

明确标注为 **Mock，不是真实模型产出**：

```
provider call  : has_auth=true, text_only=true, images=0, prompt_chars=3843
draft mode     : model
validation     : PASS (0 problems)
digest matches : True
```

**这说明代码是通的，但不构成 G4 通过。**

---

## 七、测试与反向验证

### 7.1 新增测试

| 文件 | 数量 | 覆盖 |
|---|---:|---|
| `test_tsb370_input_status.py` | 9 | digest 单一事实源、只读、409 |
| `test_tsb370_model_plan.py` | 19 | 白名单、解析、权威字段、守卫 |
| `test_tsb370_model_api.py` | 10 | 403/409/422/502、零外发 |
| 前端 `DirectorPlans.test.tsx` | +4 | digest 传递、禁用生成、409 清草案 |
| **合计新增** | **42** | |

### 7.2 反向验证

| # | 注入缺陷 | 失败用例 |
|---|---|---|
| RV-1 | `expected_input_digest` 改回 `undefined` | `the preview request carries the digest from input-status` |
| RV-2 | 去掉每请求授权检查 | `test_model_mode_without_a_grant_is_403_and_sends_nothing` |
| RV-3 | 把用户备注投影进 prompt | `test_user_notes_and_personal_data_never_leave` |

全部精确命中，恢复后全绿。

### 7.3 关于既有测试失败

首轮全量出现 **4 个既有测试失败**（`test_native_video_provider`、
`test_video_budget_http_contract`）。

**根因是我的问题**：为实验零重试，把 `TRAVELSB_MODEL_MAX_RETRIES=0` 写进了
`.env`，破坏了产品的重试合同。

**修复**：全局恢复默认（`model_max_retries=2`）；零重试只在本轮实验进程里
用环境变量控制。既有测试恢复通过，**未删改任何测试**。

---

## 八、范围声明

**修改**：
- `backend/app/api/director.py`（input-status + model 分支）
- `backend/app/services/director_plan_providers.py`（真实 adapter）
- `backend/app/services/vision/providers/{base,openai_compatible}.py`（text_only + ledger hook）
- `backend/app/services/vision/{semantic,motion,video_job}.py`（channel 声明）
- `backend/app/services/vision/capabilities.py`（注释）
- `frontend/src/pages/DirectorPlans.tsx`、`lib/{api,types}.ts`（digest 接线）
- `.env`（HTTPS、移除全局零重试）—— **未被 Git 跟踪**

**新增**：
- `backend/app/services/model_call_ledger.py`
- `backend/app/services/director_plan_model.py`
- `backend/tools/tsb370_*.py`（实验驱动，不含凭据）
- 3 个测试文件 + 4 个前端用例
- 本目录文档

**未改动**：原片、人工标注、Storyboard、Alembic、既有 API 契约、
TSB-366/367/368/369 的全部修复。

**未做**：写入正式 Storyboard、自动渲染、音乐推荐、全库批量分析。

---

## 九、遗留与下一步

| # | 遗留 | 建议 |
|---|---|---|
| 1 | **G4 模型策划未验证** | 需**新一轮授权额度**（≥2 次 plan_text）。代码已修好 |
| 2 | **G3 原生视频未验证** | 端点恢复后用 2 次额度重探测；注意先用文字对照区分故障与能力 |
| 3 | **G5 人工观看未做** | 需人看 5–12 段并填 `HUMAN_REVIEW_SCORECARD.md` |
| 4 | 89/99 仍无读数 | 属后续扩容任务，需单独审批 |
| 5 | 多帧受限于单帧镜头 | 需重新抽帧才能做运动判断 |

### 建议的下一轮

1. **先扩额度再重试 G4** —— 这是把"可信生成器"变成"AI 导演"的唯一阻断项。
2. **人工观看 5 段** —— 验证静帧模型准不准，与 G4 是两件事。
3. **端点恢复后重探测 G3** —— 用文字对照法，避免再次错误归因。

**不建议**：在证据覆盖率 13/99 时做自动渲染或音乐推荐。


---

## 十、CI 结果与过程中的两次红灯

| run | head | 结论 |
|---|---|---|
| #132 | `2527715` | ❌ 4 个 Backend 作业失败 |
| #134 | `d3378fd` | ✅ **7/7 success** |

链接：https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42791564920

| 作业 | 结论 |
|---|---|
| `Frontend` | ✅ |
| `Required FFmpeg (windows-latest)` | ✅ |
| `Required FFmpeg (ubuntu-latest)` | ✅ |
| `Backend (Python 3.11)` ×2 | ✅ |
| `Backend (Python 3.13)` ×2 | ✅ |

### 10.1 #132 失败的根因（我的缺陷）

**现象**：本地全量 2000 项全绿，CI 4 个 Backend 作业失败。

**根因**：`test_tsb370_model_api.py` 中 4 个用例**隐式依赖开发者本机 `.env`
的 `TRAVELSB_MODEL_MODE=on`**。CI 默认 `off`，于是 `authorize_upload()` 提前
返回 403，断言 502/422 失败。

**判断**：CI 默认关闭模型是**正确**的（CI 不应连真实模型）。
错的是我的测试依赖了环境。

**修复**：新增 `model_enabled` fixture，由需要的用例显式打开开关并给
`example.invalid` 占位端点（零网络）。同时把"无授权即 403"的用例也**显式要求
模型已开启**——否则它可能因"模式关闭"而非"缺少授权"这种**错误原因**通过。

**验证方式**：在本地**模拟 CI 条件**（`MODEL_MODE=off`、无 BASE_URL、无 API_KEY）
下重跑，通过后才提交。

### 10.2 一个操作失误

提交时误把 `.git-commit-msg.txt` 纳入版本控制，已在下一个提交中移除并加入
`.gitignore`。

---

## 十一、最终交付清单

### 代码

| 文件 | 作用 |
|---|---|
| `backend/app/services/model_call_ledger.py` | 持久化 attempt/byte 预算账本 |
| `backend/app/services/director_plan_model.py` | 白名单投影 + 回复解析 |
| `backend/app/services/director_plan_providers.py` | 真实受控 model adapter |
| `backend/app/api/director.py` | `input-status` 只读端点 + model 分支 |
| `frontend/src/pages/DirectorPlans.tsx` | digest 接线、禁用生成、409 清草案 |
| `backend/tools/tsb370_*.py` | 实验驱动（无凭据） |

### 测试

| 文件 | 数量 |
|---|---:|
| `test_tsb370_input_status.py` | 9 |
| `test_tsb370_model_plan.py` | 19 |
| `test_tsb370_model_api.py` | 10 |
| 前端 `DirectorPlans.test.tsx` | +4（共 18） |
| **本轮新增** | **42** |

### 回归

| 项 | 结果 |
|---|---|
| 后端全量 | **2000 collected / 0 failed / 0 errors / 6 skipped** |
| 前端四项 | typecheck / lint / **166 passed** / build 全部通过 |
| 真实 Chrome UAT | **12 项功能全部通过** |

---

## 十二、原库与源文件未变（可核对）

| 表 | 实验前 | 实验后 |
|---|---:|---:|
| Storyboards | 1 | 1 |
| StoryboardVersions | 1 | 1 |
| EditPlanShots | 0 | 0 |
| PlanOperations | 0 | 0 |
| Jobs | 23 | 23 |
| UserAnnotations | 0 | 0 |

源文件：**只读引用，未复制、未改写、未上传**。

---

## 十三、一句话总结

> **G0 合同与接线 PASS；真实静帧 (11) 与多帧 (2) 模型调用 PASS 且产出具体可核对；
> 原生视频因端点故障 BLOCKED（非能力结论）；真实模型策划 BLOCKED（因我的实现缺陷
> 与 token 预算不足）；人工质量验收 MANUAL_REVIEW_PENDING；发布门禁不关闭。**

**本轮验证了"真实模型确实可用且数据能正确穿透"，但没有验证"AI 导演的创作质量"。**
这两件事必须分开陈述。
