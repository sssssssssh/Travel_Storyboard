# TSB-362 — 严格全量结果与一个基线既有偶发的归因

执行：2026-11-03　命令（从 `backend/`，仓库外 TMPDIR）：

```powershell
$env:TSB_REQUIRE_FFMPEG = "1"
$env:TMPDIR/$env:TEMP/$env:TMP = <仓库外的独立临时目录>
python -u -m pytest --junitxml=..\docs\validation\tsb362\backend-full.local.xml
```

## 结果

```
tests=1385 failures=1 errors=0 skipped=5
1 failed, 1379 passed, 5 skipped in 691.08s (0:11:31)   exit=1
```

基线（TSB-361）为 `collected=1383`；本轮 +2 即新增的 W05/W06，**数量完全对得上**。
5 个 skip 全部是 TSB-361 记录过的「link creation is a platform fact」，未新增 skip。

## 唯一失败的用例

```
FAILED tests/test_job_cancel_kills_process.py::test_cancelling_one_run_does_not_kill_a_concurrent_run

>       bystander_pid = int(open(pid_file).read().strip())
E       ValueError: invalid literal for int() with base 10: ''
```

### 根因（与 R3 无关）

`os.path.isfile(pid_file)`（L427）在子进程 `open(pid_file,'w')` **刚创建文件、
`write()` 尚未落盘**的窗口内就已经返回真。主线程在 `time.sleep(0.02)` 轮询里撞中
该窗口后，L430 立刻读到空字符串 → `int('')` 抛错。

这正是 **TSB-361 §八·附** 在 `test_media_process_tree.py` 里修掉过的同一类缺陷
（当时的表现是 "the tree was never observable"）。TSB-361 引入的 `_pids_ready()`
只装在 `test_media_process_tree.py`，**本文件漏掉了**，仍沿用 `os.path.isfile` 作就绪判定。

为什么「单跑通过、连跑 8/8 失败」：单跑时子解释器冷启动慢，主线程的 20 ms 轮询有富余；
连跑时子解释器启动更快，更容易恰好落在"文件已建、内容为空"的窗口里。

### 归因证据（同一台机、同一命令，仅切换代码版本）

| 代码版本 | 连续 8 轮 | 说明 |
|---|---|---|
| **纯基线**（`git stash` 收起 R3 后） | **8/8 FAIL** | 无需 R3 即可复现 |
| **R3 版本** | **8/8 FAIL** | 与基线完全一致 |

两者失败率**完全相同（8/8 对 8/8）**，证明该偶发**不由 R3 引入，也不被 R3 改变**。
测试文件 `git status` 显示**未被 R3 触碰**。

### 是否影响本轮交付

- **不影响 R3**：R3 专项 `test_probe_temporal_evidence.py` **40 passed**；相关 5 套件 **79 passed**。
- **不影响阶段 B**：探测器调用 `run()` / `extract_frame()` **均不传 `cancel_check`**
  （`probe_video_capability.py:143`、`:185`），走 `subprocess.run` 快路径；失败用例走的是
  `Popen` + `cancel_check` 轮询路径。**两条路径无交集。**

## 待用户决定

这不是本轮范围内引入的缺陷，且不在阶段 A/B 的必需路径上。可选：

- **A**：按 TSB-361 同一处方修掉——把本文件的就绪判定换成「文件可读且内容为非空整数」。
- **B**：记录为「基线既有、尚未修复」的已知问题，本轮不动代码，留待后续批次。

### ✅ 用户裁决（2026-11-03）：**选 B**

用户在获知两项选择后回复「B 授权B」，即：

- **B**：该偶发**本轮不修**，作为「基线既有、尚未修复」记录在案，留待后续批次；
- **授权 B（阶段 B）**：放行真实端点探测，已执行，见
  [native-video-probe-summary.md](native-video-probe-summary.md)。

因此本文件的结论保持为：**严格全量 1385 collected / 1 failed / 5 skipped**，
该 1 failed 是**基线既有**、**明确决定不在本轮修复**的问题。
交付报告中不得表述为"全绿"或"全部通过"。
