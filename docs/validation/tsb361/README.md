# TSB-361 证据：Windows 全量测试隔离整改

- 基线 SHA：`119531effc8041a8b545d34f603faefe9308708f`
- 主机：Windows 10 19045（`LongPathsEnabled=0`）、Python 3.13.7（`backend/.venv`）、
  FFmpeg 9.0.2（仓库自带，**不在 PATH**）、Node 22
- 运行条件：`TSB_REQUIRE_FFMPEG=1`，工作目录 `backend/`
- 临时目录：取证时把 `TMPDIR`/`TEMP`/`TMP` 指向**仓库之外**的独立目录
  （默认 `%TEMP%` 会被宿主安全删除钩子在 `sessionfinish` 拦下，退出码变 1 而没有
  失败用例；指进仓库内则会踩到 `test_video_capability_store::test_a6b` 的
  "记录不得落在仓库里"守卫）

---

## 一、RED：修复前可复现

两个问题都不是"单独运行偶发"的结果，而是"前序用例 + 目标用例"两段组合下的
**确定性失败**。下面两条命令在屏障关闭时 100% 复现（本轮各跑两次，两次都红）。

### 问题 A：`test_b01_no_upload_consent_means_no_frames_and_no_model_request`

```bash
cd backend && TSB_REQUIRE_FFMPEG=1 python -m pytest \
  tests/test_video_fallback_execution.py tests/test_video_job.py \
  tests/test_video_fallback_runner_e2e.py::test_b01_no_upload_consent_means_no_frames_and_no_model_request
```

```
>       assert e.extracted == []
E       AssertionError: assert [(('C:\\Users...583D692480>})] == []
E         Left contains 2 more items, first extra item:
E         (('C:\\Users\\changan\\AppData\\Local\\Temp\\pytest-of-changan\\pytest-619\\
             test_a_fallback_does_not_produ0\\src.mp4',...
FAILED tests/test_video_fallback_runner_e2e.py::test_b01_no_upload_consent_means_no_frames_and_no_model_request
```

失败值里出现的是**别的用例的 `src.mp4`**——正是任务书 §1.1 记录的现象。

### 问题 B：`test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free[motion]`

```bash
cd backend && TSB_REQUIRE_FFMPEG=1 python -m pytest \
  tests/test_video_fallback_execution.py \
  "tests/test_vision_cleanup_status.py::test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free[motion]"
```

```
>       assert len(calls)==paid
E       assert 2 == 1
E        +  where 2 = len([1, 1])
FAILED tests/test_vision_cleanup_status.py::test_successful_model_with_failed_cleanup_is_partial_and_retry_is_model_free[motion]
```

`calls` 在 cleanup-retry **之前**就已经多了一次，断言把"重试不重新请求模型"读成了
"重试又请求了一次"。

---

## 二、根因证据

### 问题 B 的直接证据：谁调用了第二次 `infer`

在 `test_vision_cleanup_status.py` 的 `infer` stub 上临时打印调用栈（取证后已移除）：

```
[infer] calls=2 thread=tsb-job_1 args=(..., {'segment_id': '70f31d08-...',
        'source_path': 'C:\\Users\\changan\\AppData\\Local\\Temp\\pytest-of-changan\\
                        pytest-619\\test_v23c_a_fallback_does_not_0\\src.mp4', ...})
  File "app\services\runner.py", line 480, in _run
    task(job_id)
  File "app\services\vision\motion.py", line 278, in motion_review_task
    result, usage = _analyze_motion(
  File "tests\test_vision_cleanup_status.py", line 37, in infer
```

第二次调用**不是**本用例的 retry 发出的，而是 `tsb-job_1` 线程里一个真实 Job：
它的素材路径属于 `test_v23c_*` 用例。同一次调试里 `runner._futures` 的三条记录
（`motion_review/partial_failed`、`video_analyze/succeeded`、`motion_review/partial_failed`）
全部属于 v23c 的项目。

### 两条泄漏路径

1. **已提交、未等待**：`test_video_fallback_execution.py` 里的用例直接调
   `vj.video_analyze_task(job_id)`；回退分支经 `_schedule_fallback` →
   `job_service._create_locked` → `runner.submit(...)` 把 `motion_review` 子任务
   提交到**模块级 `runner`**。用例返回时子任务还在跑，晚到的
   `motion.extract_frame` 落进下一个用例 monkeypatch 的 tracker（问题 A）。
2. **未派发、被恢复**：同一批用例从不经过 `JobRunner._run`，行永远停在 `pending`。
   下一个用例的 `client` fixture 启动 lifespan 时
   `app/main.py::lifespan → resume_interrupted_jobs()` 把它当作"上次进程崩溃遗留"
   重新派发，于是它**在那个用例体内**执行（问题 B 的第二次 `infer`）。

两条路径都不是产品缺陷：生产进程只启动一次，`resume_interrupted_jobs()` 的语义
在那时是正确的。缺口在测试：用例之间共享同一个进程、同一个 `runner`、同一个数据库。

---

## 三、GREEN：同一组命令，屏障打开

`backend/tests/conftest.py` 新增 autouse 屏障 `_no_background_jobs_left_behind`，
teardown 依次执行：

1. `drain_background_jobs()` —— 等 `runner._futures` 全部完成；每轮重新快照，
   因为完成的父任务可能又提交了后继；超时返回残留 id 并 `pytest.fail`（不静默）；
2. `settle_unfinished_jobs()` —— 把仍是 `pending/running/cancelling` 的行落成终态，
   使下一次启动的恢复扫描不再选中它们。

上面两条 RED 命令在屏障打开后均为 `50 passed, 1 skipped` / `19 passed, 1 skipped`，
无失败（见 `full-windows.log`、`mixed-1.log`）。

### 归因实验（两半各自的作用）

| 屏障组合 | 问题 A 命令 | 问题 B 命令 |
|---|---|---|
| 两半都关闭 | **RED**（`e.extracted == []`） | **RED**（`assert 2 == 1`） |
| 只 drain（settle 关） | 绿 | 绿（本轮时序）／历史运行曾 RED |
| 只 settle（drain 关） | 绿 | 绿 |

单半在某些时序下也能通过，正说明两条路径都会造成同类污染、且触发依赖时序——
所以两半都必须实现，"单半通过"不能当结论。定点回归见
`tests/test_tsb361_job_lifecycle.py`（路径 1：A01–A08；路径 2：A09）。

---

## 四、顺序与重复

| 场景 | 命令要点 | 结果 |
|---|---|---|
| A → B 混合（含前序视频/回退/staging/cancel/stability 用例） | `test_video_fallback_execution.py test_video_job.py test_b01... test_tsb361_job_lifecycle.py test_vision_cleanup_status.py` | 全绿 |
| B → A 反向 | `cleanup[motion] test_tsb361_job_lifecycle.py test_b01... test_video_job.py test_video_fallback_execution.py` | 全绿 |
| 相关后台 Job 用例混跑 | `mixed-1.log` | **131 passed, 1 skipped**（199.95s） |
| 同一组连续 3 轮 | `repeat-3x.log` | 3 轮均 `131 passed, 1 skipped` |
| Windows 严格全量 | `--junitxml=tsb361-windows.xml` | 见下节 |

`repeat-3x.log` 里第 1、3 轮的 `exit=1` 来自宿主安全删除钩子中断 `sessionfinish`，
不是用例失败——两轮的点号行都没有 `F`，第 2 轮给出了 `131 passed, 1 skipped`。

## 五、Windows 严格全量

见 `full-windows.log`（本轮最终结论）与 `junit-summary.txt`。

## 六、文件清单

| 文件 | 内容 |
|---|---|
| `full-windows.log` | `TSB_REQUIRE_FFMPEG=1 python -m pytest --junitxml=tsb361-windows.xml` 的输出 |
| `junit-summary.txt` | 从 junit XML 统计的 tests/failures/errors/skipped |
| `mixed-1.log` | 相关用例混跑（A → B） |
| `repeat-3x.log` | 同组连续 3 轮 |
| `frontend.log` | 前端 typecheck / lint / test / build |
