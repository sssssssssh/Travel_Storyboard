# M5.3-R3 稳定性与安全收尾发布记录（TSB-345 / 346 / 347 / 348 / 349 / 350）

**审阅基线 commit：** `main@65fd605`（R3 任务书基线，与本批起点一致）
**本批终态 commit：** `main@f6dcc3c`（代码终态 `46c720f`，其后仅追加本记录）
**机器（本机测量）：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**CI：** GitHub Actions **7/7 success**，两个 SHA 各跑一次：
[run 39009705787](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39009705787) @ 代码终态 `46c720f`；
[run 39010176106](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39010176106) @ 文档终态 `f6dcc3c`
**批次：** R3-A（TSB-345、TSB-346）→ R3-B（TSB-347）→ R3-C（TSB-348）→ R3-D（TSB-349、TSB-350）

---

## 〇、结论（严格按门禁）

**「R3 代码级整改通过，同 SHA CI 7/7，跨平台暂存安全缺陷已关闭。」**

| 门禁 | 内容 | 状态 |
| --- | --- | --- |
| **G1** | 静态检查 + 单元测试（0 failure） | **PASS** — 后端 1266/0/0/44；前端 typecheck+lint+71 测试 |
| **G2** | 同一 SHA 的 CI 7/7 | **PASS** — `46c720f` [run 39009705787](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39009705787) 与 `f6dcc3c` [run 39010176106](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39010176106)，均 7/7 |
| **G3** | Mock E2E（API → Mock → Observation → Cleanup） | **PASS** — 9/9 |
| **G4** | 数据安全（原片只读、历史与人工判定不覆盖） | **PASS** |
| **G5** | 真实端点视频能力 | **`blocked_by_endpoint`** — 见第五节 |
| **G6** | 真实 A/B（原生视频 vs 抽帧） | **`not_evaluated`**（依赖 G5） |

在 G5、G6 完成前**不得**声称「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。

---

## 一、提交清单

| # | Commit | 任务 | 摘要 |
| --- | --- | --- | --- |
| 1 | `c448922` | TSB-345、TSB-346 | 暂存叶子的代际身份与防接管，清理不得静默跳过链接 |
| 2 | `9292ce1` | TSB-347 | 预算在每次真实 HTTP 尝试前预留，派生时长逐镜头累计 |
| 3 | `46c720f` | TSB-348 | `allow_fallback` 真正调度多帧任务 |
| 4 | `f6dcc3c` | TSB-349、TSB-350 | 发布记录、回归清单与 CI 证据（仅文档） |

起点 `65fd605` 与任务书基线逐字符一致，工作树干净。

---

## 二、逐任务：问题 → 修复 → 验收

### TSB-345（P0）inode 复用导致的暂存误删 —— Ubuntu CI 真实失败

**问题（RED，已确定性复现）**

Ubuntu `test_v18_a_replaced_leaf_directory_is_refused` 真实失败：
`rmtree` + `mkdir` 之后 ext4/overlayfs **立即复用**刚释放的 inode，
`_identity()` 返回与登记时**完全相同**的 `(st_dev, st_ino)`，只比较 dev/ino 的
守卫遂认为"还是我们那个目录"，把替换目录里的 `someone_elses.bin` **删掉了**。

**macOS 的 APFS 不复用 inode，所以这条缺陷在开发机上永远测不出来** ——
这正是 C8「CI 与合成素材通过不等于真机验收」与「仅本机通过不能作为验收」的实例。
R2 记录中 G2 未能回填，根因就是它。

复现不依赖 inode 复用是否恰好发生：**直接把登记身份改写成与替换目录一致**，
即可在任意平台确定性触发。修复前实测：

```
recorded identity forced to live identity (16777231, 11217751)
result.ok=True
someone_elses.bin survived: False     <- 文件被删除
```

**修复**

引入**创建代际身份（generation identity）**：

* 登记时在叶子内写入高熵 nonce 标记 `.tsb-generation`
  （`secrets.token_hex(32)`，以 `O_CREAT|O_EXCL|O_NOFOLLOW` 打开）。
* 理由：路径、`st_dev`、`st_ino` 都是**位置**的属性，会被 inode 复用伪造；
  nonce 属于**创建事件**，重建目录无法复现。
* `_verify_generation()` 在删除前的每个关口校验，任何缺失 / 不可读 / 不符一律
  `STAGING_NOT_OWNED` **失败关闭**，**绝不**退回路径或 inode 匹配。
* **关闭 TOCTOU**：校验锚定在已固定的父/子目录 fd 上
  （`O_DIRECTORY|O_NOFOLLOW`、`dir_fd`），而非路径查找；标记在所有内容删除后
  **最后**移除，移除后改以 fd 身份做终局确认。
* Windows 的 reparse point 仍按 `FILE_ATTRIBUTE_REPARSE_POINT` 拒绝。

**`FileExistsError` 不得裸 `pass`（任务书 C 项）**

原实现直接 `pass`，等于把确定性路径上**任何**已存在目录（陈旧残留、他人叶子、
运维误放）当作本任务暂存并授予删除权。改为 `is_registered_leaf()`：仅当注册表
仍持有**同一 job** 且代际校验通过的登记时才复用；否则记录稳定错误码并报告。

**验收**（代际身份对两种伪造向量都成立）

| 向量 | 结果 |
| --- | --- |
| 叶子 inode 被复用（身份强制相等） | `ok=False`、`STAGING_NOT_OWNED`、文件**存活** |
| 攻击者自建同名标记文件 | 拒绝，受害者文件**存活**（猜不出 64 hex nonce） |
| `_verify_generation` 是否参考 inode | **否** —— 所有权只依赖创建 nonce |

新增 5 项回归（V21–V22e）+ 4 项 TSB-346 回归。正常路径**未退化**：
V21f 断言合法清理仍成功、`removed_files==1`、重复清理幂等。

> 修复过程中自查出两处自身引入的副作用并一并修正：清理回执不再保留 nonce
> （它已完成使命，保留只是冗余持有唯一的删除授权凭据）；代际标记不计入残留
> 字节（那是清理器自己的记账，计入会虚报 64 字节泄漏）。

### TSB-346（P1）悬空链接被当作「从未创建」而静默跳过

**问题（RED）** 清理点用 `Path(stage).exists()`，它**跟随链接**：指向不存在目标
的悬空链接返回 `False`，于是被归类为「路径从未出现」，记为
`{'ok': True, 'note': 'stage was never created'}`。既是 C6 的静默跳过，
也让该残留链接永远无人清理、无人报告。

实测：

```
link exists()   : False   <- 被当成"从未创建"
link lexists    : True
link is_symlink : True
```

**修复** 新增 `path_occupies_disk()`，用 `os.lstat`（lexists 语义、**不跟随**）判断：
凡是占用着路径的东西（含悬空链接）一律交给清理器，由清理器对链接失败关闭；
`OSError`（不可读）也视为"存在"，因为**不可读不等于不存在**。

**验收** 新增 4 项（V37–V37d）。反向验证：恢复 `Path.exists()` → V37d RED。

### TSB-347（P1）Provider 重试绕过预算 + 派生时长失真

**问题（RED，两个）**

1. **Provider 重试绕过预算。** Job 层只在调用 `complete_video()` **之前**记一次账，
   而 Provider 内部有 `for attempt in range(1, max_retries + 2)` 重试循环。
   `max_retries=3` 时单个镜头最多发出 **4 个** HTTP 请求，台账却只记 **1 次** ——
   `max_calls` 与 `max_total_bytes` 对重试完全无效。修复前代码注释自己就承认了：
   *"the charge is per logical call"*。
2. **派生时长不累计。** `note_derived()` 用 `self.derived_seconds = seconds`
   **覆盖**而非累加，调用点又只传当前镜头。十个 5 秒镜头记录为 **5 秒**，
   `max_total_sec` 永远不可能被触及 —— 限制形同虚设，台账还谎报真实用量。

**修复**

* Provider 新增 `attempt_guard: Callable[[int], None]`，在**每次** `httpx.post`
  **之前**调用；guard 抛错即以 `ModelError` 传播，**字节不会离开本机**。
* Job 层把计费放进 `charge_attempt()` 并作为 `attempt_guard` 传入，
  同时记录 `stats["http_attempts"]`；保留一次前置检查，使已耗尽的预算在做任何
  工作前就失败。中途被否决时标记 `budget_exceeded` 并附台账，
  避免看起来像上游故障。
* `note_derived()` 改为**累加**，每加完一次即校验上限；台账新增
  `derived_bytes` 与 `remaining_seconds`。

**验收** 新增 8 项（V38–V38h）。**反向验证 2 次**：①恢复覆盖式
`derived_seconds` → V38f/V38g RED；②移除 Provider 的 guard 调用 → V38d RED。

### TSB-348（P1）`allow_fallback` 只记 deferred，未实际分析

**问题（RED）** 回退分支记录 `phase="deferred"` 与 `fallback_deferred_to` 之后
**直接 return**，没有任何东西被排队。TSB-337 只做到"让回退可见"，但一个可见却
永远无人执行的 deferred，从用户视角仍是**静默空转**：项目永远不会被分析，
也没有任何地方说得出接下来会发生什么（C6）。

**修复**

* 新增 `_schedule_fallback()`：把回退工作真正排给拥有它的 job 类型
  （`multiframe → motion_review`）。返回 `(scheduled, error)`，**从不抛异常**，
  失败原因写入 `stats["fallback_schedule_error"]` 并记入 failures，
  使"回退没发生"始终可见。
* 排队走 `_create_locked`（无锁入口）而非 `create_project_job`：worker **已经
  持有本项目锁**，再走公开入口会拿同一把非可重入锁并报出假冲突。
* `_create_locked` 新增 `allow_own_job_id`：回退是由"它要回退的那个 job"排出的，
  那一刻唯一的活跃互斥任务就是它自己的前驱，不能被当成冲突 —— 否则
  "同意回退"永远排不出任何东西。豁免**精确到单个 job id**
  （`j.id != allow_own_job_id`），其他任何活跃互斥任务仍照常 `JobConflict`。
* 排出的任务沿用计划选中的 `segment_ids` / `limit`，分析同一批镜头而非整个项目。

**验收** 新增 6 项（V39–V39f）。反向验证：改回"记录即返回" → V39f RED。
手工确认：回退真实产生 `motion_review` 行，config 为
`{"segment_ids":["seg-1"],"limit":7,...}`；插入外来 `vision_analyze` 活跃任务后
回退被正确拒绝且**不留残留行**（互斥语义未被削弱）。

### TSB-349（P0）同 SHA CI 7/7

**问题** 上一轮 CI（run `38993163807` @ `65fd605`）7 个作业中 **5 成功 2 失败**：
Ubuntu 后端 Python 3.11/3.13 均失败于
`tests/test_staging_leaf_substitution.py::test_v18_a_replaced_leaf_directory_is_refused`，
各为 1171 passed / 1 failed / 44 skipped。

**修复** 即 TSB-345 的代际身份（根因见上）。

**验收（本机测量 + CI 实证）**

| 项目 | 计数 |
| --- | --- |
| 后端全量（本机，含 `.env`） | **1266 collected / 0 failed / 0 error / 44 skipped** |
| 后端全量（**移走 `.env`**，CI 真实条件） | **1266 / 0 / 0 / 44** |
| CI @ `46c720f`（代码终态） | **7/7 success** |
| CI @ `f6dcc3c`（文档终态） | **7/7 success** |

CI 作业明细（[run 39009705787](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39009705787)）：

| 作业 | 结果 |
| --- | --- |
| Backend (Python 3.11) — **ubuntu-latest** | ✅ success（**此前失败**） |
| Backend (Python 3.13) — **ubuntu-latest** | ✅ success（**此前失败**） |
| Backend (Python 3.11) — windows-latest | ✅ success |
| Backend (Python 3.13) — windows-latest | ✅ success |
| Required FFmpeg (ubuntu-latest) | ✅ success |
| Required FFmpeg (windows-latest) | ✅ success |
| Frontend | ✅ success |

`backend` 作业跑**全量套件**（未忽略该文件），且 `46c720f` 同时包含
`_verify_generation` 实现与 `test_v21_reused_inode_identity_cannot_authorise_deletion`
用例，因此 Ubuntu 上的通过是**对该修复的真实跨平台验证**，不是本地外推。

> 未做：**没有** xfail、**没有**新增 skip、**没有**删除断言、**没有**设置 CI 全局
> 模型开关、**没有**只跑部分套件。skip 仍为登记在册的 44 项平台事实。

### TSB-350（P1）发布门禁与能力状态收口

本记录即为交付物。门禁表见第〇节；G5/G6 按独立门禁处理，措辞不使用
「已修复」「已通过」以外的夸大表述。`docs/REGRESSION.md` 已同步用例映射。

---

## 三、验证结果

| 套件 | 计数 |
| --- | --- |
| 后端全量（本机） | 1266 / 0 failed / 0 error / 44 skipped |
| 后端全量（无 `.env`） | 1266 / 0 / 0 / 44 |
| `test_staging_leaf_substitution.py` | 23（R2 为 12） |
| `test_video_fallback_execution.py` | 19（R2 为 9） |
| `test_video_budget_ledger.py` | 21（R2 为 13） |

**R3 新增/扩展用例：** 23 项（V21–V22e 五项 + V37–V37d 四项 + V38–V38h 八项 +
V39–V39f 六项）。

**反向验证：** 5 次（TSB-346 ×1、TSB-347 ×2、TSB-348 ×1、TSB-345 身份伪造
前后对照 ×1），全部确认用例非空断言。

> 计数口径：`collected` 含 skip，`passed` 不含。全部数字来自 junitxml 实测。

---

## 四、范围声明

**本轮改动：**

* `backend/app/services/staging_cleanup.py`（TSB-345/346）
* `backend/app/services/vision/video_job.py`（TSB-345/346/347/348）
* `backend/app/services/vision/providers/native_video.py`（TSB-347）
* `backend/app/services/job_service.py`（TSB-348，`allow_own_job_id`）
* `backend/tests/`：`test_staging_leaf_substitution.py`、
  `test_video_fallback_execution.py`、`test_video_budget_ledger.py`
* 本记录与 `docs/validation/m5.3-r3/`

**本轮明确未做：**

* **未**修改 `TRAVELSB_MODEL_MODE` 生产默认（仍为 `off`）。
* **未**在 CI 注入真实 Key，**未**导出全局模型开关。
* **未**删除或改写任何原片、历史 `SegmentSemanticResult`、
  `SegmentMotionReview`、人工评分、历史 `EditPlanShot`。
* **未**修改 M5.1/M5.2 的图像语义与动态复核 Prompt/Schema。
* **未**变更 PAI-EAS 部署参数、密钥、代理与公网权限。
* **未**为了通过测试删除断言、添加 `xfail`/`skip`、放宽 skip 白名单
  或设置 CI 全局模型开关。
* **未**开始 M6，**未**重做 UI 布局，**未**提升产品正式版本号。

**尚未完成（诚实列出）：**

1. **G5 真实端点联调**：端点此前全线 HTTP 503 `no healthy upstream`
   （文本/图片/视频一律），属外部部署状态，需服务端恢复后执行。
2. **G6 真实 A/B（20–30 镜头）**：依赖 G5。
3. **Windows 实机手工确认**：CI 的 Windows 作业通过，但本记录未做真机人工走查。
4. **`test_vision_commit_races.py` 的 Ubuntu 间歇竞争**：本批 CI 全绿，
   未复现，因此**未** xfail/skip；仍作为观察项保留。

---

## 五、G5 真实端点状态（`blocked_by_endpoint`）

端点 `GLM-5.3-Flash`（PAI-EAS）观察结果：`/v1/models` 曾返回 HTTP 200
`{"id":"GLM-5.3-Flash","owned_by":"vllm"}`；此后所有 `/chat/completions`
（**文本、图片、视频一律**）返回 HTTP 503 `no healthy upstream`。

任务书明确：**文本/视频同为 503 不等于视频能力通过或不通过**。本项目探测工具在
同样条件下行为正确：报 `endpoint unhealthy: text-only control also returned
HTTP 503; the video attempt is inconclusive`，保持 `unverified`/`sendable: False`，
且**未写入能力记录**。

因此 G5 = `blocked_by_endpoint`（外部部署状态，非代码缺陷，且**不构成**视频能力的
任何证据）。G6 依赖 G5 ⇒ `not_evaluated`。

> 凭据安全：全仓库 `git grep` 该 token **0 次命中**；`backend/.env` 由
> `.gitignore:23` 忽略，CI 无该文件。本轮新增的 `--redact` 会清洗端点主机、
> 本机路径与凭据形态串。
>
> 明文 HTTP 提示：该端点为 `http://` 且非回环。任务书禁止生产中明文上传原片；
> 迄今探测流量只使用合成测试片。

---

## 六、证据文件

| 文件 | 内容 |
| --- | --- |
| `docs/validation/m5.3-r3/backend-full-46c720f.xml` | 本机全量 junitxml |
| `docs/validation/m5.3-r3/backend-full-CI-conditions-no-env.xml` | 无 `.env`（CI 真实条件） |
| `docs/validation/m5.3-r3/ci-run-39009705787.json` | CI 7/7 作业明细（`46c720f`） |
| `docs/validation/m5.3-r3/ci-run-39010176106.json` | CI 7/7 作业明细（`f6dcc3c`） |

详见 [docs/validation/m5.3-r3/README.md](validation/m5.3-r3/README.md)。
