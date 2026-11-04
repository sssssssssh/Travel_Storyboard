# TSB-362 / R3 — 单帧负对照接线修复（RED → GREEN → 反向验证）

基线：`main@0da77a19725a6af5963672a84d6f6464d4be7ac8`　执行：2026-11-03
修改文件：`backend/tools/probe_video_capability.py`、`backend/tests/test_probe_temporal_evidence.py`
**未发出任何真实模型请求**；一切测试均为本地回环（monkeypatch 拦截 `httpx.post`）。

## 一、问题

`run_probe` 声明了 `frame_only: bool = False`，但**函数体从未读取它**；视频内容部件
只由 `include_video` 决定。静态证明（AST，修复前）：

```
--- run_probe parameters vs actual use ---
  clip             used=True
  model_id         used=True
  authorization    used=True
  timeout_sec      used=True
  max_bytes        used=True
  round_order      used=True
  probe_id         used=True
  include_video    used=True
  frame_only       used=False      ← 声明了，从未使用
```

调用点传的是 `include_video=True`（默认）+ `frame_only=True`，所以
`run_multi_round_probe` 的"单帧负对照"实际发送**完整 MP4**。

## 二、后果链

```
单帧对照实发整段视频 → 模型答对 → frame_control_answered_correctly = True
  → classify_probe_outcome() 命中 L540-545
  → video_accepted_sampling_unknown
  → SENDABLE_STATES 仅含 verified_native_video（video_capabilities.py:58）
  → capability_gate() 拒绝 → 视频通道永远打不开
```

即：**真能读时序的端点会被这个假对照判死**，且报告会错误地写成"单帧对照也答对了"。
不影响 `unsupported`(415/422/400) 与 `blocked_by_endpoint`(5xx) 两档——它们在更早的
分支返回。

## 三、RED（修复前，真实失败）

```
cd backend
.\.venv\Scripts\python.exe -m pytest tests\test_probe_temporal_evidence.py -k "w05 or w06" -v
```

```
tests\test_probe_temporal_evidence.py F.                    [100%]

>       assert "video_url" not in types, (
E       AssertionError: the still-frame control sent a video part; it is not a
        control at all -- parts were ['text', 'video_url']
E       assert 'video_url' not in ['text', 'video_url']

FAILED ...::test_w05_the_still_frame_control_sends_no_video
================= 1 failed, 1 passed, 38 deselected in 6.02s =================
```

W06（正常轮次必须发视频）在修复前**就是绿的**——它是防"修过头"的反向守卫，不是复现项。
证据：`RED-r3-w05-w06.local.txt`。

## 四、修复（最小改动）

1. 新增 `build_still_frame(clip)`：用 `app.ffmpeg.extract_frame` 从合成短片取**末段 1 帧**
   JPEG（`time_sec=2.5`）。取末段是有意的：短片由三个纯色相位组成，单帧只能说出一种
   颜色，因此**不可能**在 `parse_probe_order` 下产出合法的三色顺序——这正是它作为
   **真阴性对照**的依据，而不是第 4 个视频轮次。
2. `run_probe` 真正接线 `frame_only`：
   - `frame_only=True` → 发 1 个 `image_url`（`data:image/jpeg;base64,`），**无** `video_url`；
   - 正常轮次 → 仍发 `video_url`（`data:video/mp4;base64,`）；
   - `include_video=False` → 无媒体部件（提示词对照，行为不变）。
3. 抽帧失败 → `error_code="FRAME_CONTROL_UNAVAILABLE"` 并**不发送**，遵循 C6
   「不静默跳过」；`finally` 中清理该帧的临时目录。

## 五、GREEN

```
tests\test_probe_temporal_evidence.py ..                    [100%]
====================== 2 passed, 38 deselected in 5.19s ======================
```

专项全套：`40 passed in 4.70s`（exit 0）。
相关套件（`test_native_video_probe` / `test_probe_clip_portability` /
`test_evidence_anonymisation` / `test_ci_environment_isolation` /
`test_video_capability_store`）：**79 passed in 51.23s**（exit 0）。

| 运行 | 结果 | 证据文件 |
|---|---|---|
| W05+W06（RED） | 1 failed / 1 passed | `RED-r3-w05-w06.local.txt` |
| W05+W06（GREEN） | 2 passed | `GREEN-r3-w05-w06.local.txt` |
| 反向验证（删接线） | 1 failed / 1 passed | `REVERSE-r3-w05-w06.local.txt` |
| 专项全套 | 40 passed | `GREEN-r3-full-suite.local.txt` |
| 相关 5 套件 | 79 passed | `GREEN-r3-related-suites.local.txt` |

## 六、反向验证（证明断言非空转）

把 `if frame_only:` 临时改成 `if False:`（保留 `image_url` 分支代码），重新运行：

```
>       assert "video_url" not in types, (
E       AssertionError: the still-frame control sent a video part ...
E       assert 'video_url' not in ['text', 'video_url']
FAILED ...::test_w05_the_still_frame_control_sends_no_video
================= 1 failed, 1 passed, 38 deselected in 6.45s =================
```

**W05 立刻变红，W06 仍绿**（说明只有帧对照接线被破坏、轮次未受影响）。恢复后 W05/W06
双双转绿。修复后的静态确认：

```
frame_only used = True
include_video used = True
```

## 七、为什么之前没抓到

`test_probe_temporal_evidence.py` 的 `_fake_probe_factory` **整体替换**了 `run_probe`，
假探测自己遵守 `frame_only`（其 `behaviour(order, has_video, frame_only)` 就带这个参数），
所以真实实现丢弃该参数的行为从未被覆盖；`test_w1` 对帧对照只断言
`frame_control_attempted is True`，不检查 `frame_only` 是否真的生效。新用例改为拦截
`httpx.post`、**运行真的 `run_probe`**，直接检查线上会出现的 payload。

## 八、范围声明

- 只改 `backend/tools/probe_video_capability.py` 与 `backend/tests/test_probe_temporal_evidence.py`；
  **未改** `backend/app/` 任何生产代码。
- **未改** `TRAVELSB_MODEL_SUPPORTS_NATIVE_VIDEO`（仍为 `false`）、`MODEL_MODE`、
  `ALLOW_INSECURE_HTTP` 或任何能力记录。
- **未**新增 skip / xfail；**未**删除或弱化任何既有断言（原 W1–W4 一行未改）。
- **未**发出真实网络请求。

## 九、尚未完成

- 完整的 Windows 严格全量**已补跑**：`tests=1385 failures=1 errors=0 skipped=5`
  （新增 W05/W06 后 collected 1383→1385，数量对得上）。唯一失败是**基线既有**的
  `test_job_cancel_kills_process.py` 偶发，已用「基线 8/8 与 R3 版本 8/8 失败率相同」
  证明与 R3 无关；详见 [full-suite-result.md](full-suite-result.md)。**该失败未修复**，
  等用户决定是否按 TSB-361 同一处方处理。
- 阶段 B（真实端点探测）**未执行**，等待授权；R1（明文 HTTP）已由用户书面接受风险。
- G5 = `blocked_by_endpoint`（历史 503，当前未验证）；G6 = `not_evaluated`。
