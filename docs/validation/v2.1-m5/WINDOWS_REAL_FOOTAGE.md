# TSB-308 · Windows 真实素材验收记录（M5 视觉理解）

**日期**：2026-10-15
**机器**：Windows，`LongPathsEnabled=0`（Windows 默认）
**基线 commit**：`main` @ `de9b5e78a1f6d14e5d8d5a79ff63c2cb40012f82`
**素材**：`F:\work\project\video`（6 段 DJI 4K HEVC，4.4 GB，**只读**）
**模型**：`GLM-5.3-Flash`（阿里云 PAI-EAS，OpenAI 兼容，**真实联网调用**）

**结论**：**代码级整改通过；真实素材发布门禁未完成。**

> 按 AGENTS.md 第六节（C8），本记录**不**构成真实素材发布门禁。
> 它证明的是：在 Windows + 真实 4K HEVC 素材上，M5 视觉理解与多帧动态复核
> 全链路可跑通，且原片未被改动。
> 门禁要求的**竖屏 / 无音轨 / HDR** 维度与 **8～10 段**规模**均未覆盖**。

---

## 一、素材

| 文件 | 大小 | 规格 |
| --- | --- | --- |
| DJI_20261002163755_0001_D.MP4 | 295.6 MB | 3840×2160 HEVC |
| DJI_20261003134833_0002_D.MP4 | 678.7 MB | 同上 |
| DJI_20261003153602_0004_D.MP4 | 883.0 MB | 同上 |
| DJI_20261003161452_0005_D.MP4 | 760.3 MB | 同上 |
| DJI_20261003164352_0007_D.MP4 | 1988.4 MB | 同上 |
| DJI_20261003165009_0008_D.MP4 | 153.9 MB | 同上 |

合计 **4.4 GB / 6 段**，全部横屏 4K HEVC、含音轨。

**未覆盖**：竖屏 ❌、无音轨 ❌、HDR ❌、6 段 < 要求的 8～10 段 ❌。

---

## 二、驱动方式

`backend/tools/acceptance_m5.py`（测试工具，非产品代码）：

```powershell
cd backend
.\.venv\Scripts\python.exe tools\acceptance_m5.py `
    --source F:\work\project\video --limit 5 --motion-limit 2 --frames 4 `
    --out ..\docs\validation\v2.1-m5\acceptance-report.json
```

它强制两项断言：

- **C1**：所有原片的前后 SHA-256 / 大小 / mtime_ns 必须完全一致；
- **C6**：任何 `failed`/`partial_failed` 的步骤都会让脚本返回非零，不允许"安静通过"。

并且**拒绝在 `TRAVELSB_MODEL_MODE != on` 时运行**——验收工具不得替用户绕过隐私开关。

---

## 三、一个被验收工具自身暴露的问题（已修）

首轮运行**长时间停在 detect 阶段**。排查分两层，两个原因都必须记录：

### 3.1 驱动脚本绕过了调度器（已修）

脚本最初直接调用任务函数 `detect_task(job_id)`，**绕过了 `runner._run`**。
而 `runner._run` 正是负责「取项目锁 + `pending → running` 状态迁移」的那一层。
结果：任务真的干了活，Job 行却**永远停在 `pending`**，下一个任务随之阻塞。

这也解释了为什么第一次跑出的是 `status=pending` 而 stats 却已填满——**两者矛盾**，
正是「绕过状态机」的特征。修复为通过 `runner.submit(job_id)` 派发并等待 future
（与 REST API 完全同一条路径）。

**教训**：验收工具必须走与生产相同的调度入口。自己手搓调用顺序，
测到的往往是自己的错误，而不是产品的问题。

### 3.2 本机 4K HEVC 解码远慢于实时（环境事实，非缺陷）

修好调度后 detect 仍迟迟不结束。用 `ffmpeg -f null -` 直接测量本机解码吞吐：

| 命令 | 素材时长 | 实测耗时 | 倍率 |
| --- | --- | --- | --- |
| 4K 原始解码（无输出） | 10.79s | 73.8s | **6.8× 慢于实时** |
| 4K → 768px | 10.79s | 72.8s | 6.7× |
| 4K → 160px @1fps | 10.79s | 78.0s | 7.2× |

机器为 **Intel Xeon E5-1620 v4（4 核 8 线程）**，素材为 **3840×2160 HEVC @ 119.9fps**
（`r_frame_rate=120000/1001`）。缩放几乎不影响耗时——瓶颈是**解码本身**，不是滤镜。

按 333.5 秒总时长推算，这 6 段的 detect 阶段需要 **约 38～40 分钟**纯 CPU 时间。
首轮验收脚本的等待上限（25 分钟）小于该值，因此被误判为"卡死"。

**这是环境算力事实，不是产品缺陷**：`extract_frame`（M5.2 真正使用的路径，
基于 `-ss` 定位单帧）在两种文件上均为 **6.7～7.9 秒/帧**，全部成功，见第四节。

**对真实素材门禁的含义**：门禁要求 8～10 段，若同样是 4K 高帧率 HEVC，
单是检测阶段就需要约 1 小时量级。这也说明 **60+ 段 / 200GB 的全量性能在本机
不具可行性**，必须换更强算力或降低采样率，不能靠耗时外推得出结论。

---

## 四、结果

见 [`acceptance-report.json`](acceptance-report.json)（含逐步状态、耗时、统计与样本）。

| 阶段 | 状态 | 统计 |
| --- | --- | --- |
| scan | `succeeded` | 6/6，`skipped_unchanged=6`（素材未变，正确跳过） |
| detect | `succeeded` | 6/6，**99 个候选片段**（首次全量 1579.5s） |
| **vision_analyze** | `succeeded` | **8/8**，12 张图，17,760 tokens，59.7s |
| **motion_review** | `succeeded` | **4/4**，采样 20 帧，`method=multiframe`，133.1s |
| **C1 原片** | **通过** | `c1_originals_unchanged: true`，`c1_problems: []` |
| **C6 无静默失败** | **通过** | `c6_no_quiet_failures: true` |

### 覆盖度如实报告

```
coverage: {library_segments: 99, eligible: 99, excluded: 0, invalid: 0,
           considered: 8, not_covered: 91, images_sent: 12}
```

库内 99 个镜头、本次分析 8 个——**没有把"分析 8 个"说成"理解全库"**。

### 图片语义理解（TSB-303/304）

模型对真实画面的描述经人工核对**准确**，举两例：

```
['nature','water','sky','transport'] | daylight
  ['lake with many small boats', 'long wooden walkway/pier over water',
   'waterfowl perched on railing']

['architecture','indoor','religious'] | indoor_artificial
  ['domed ceiling', 'stained glass windows', 'hanging lamps strung on cables',
   'large painted columns and arches']
```

未见杜撰地点名或人物身份。

### M5.2 多帧抽样路径（`extract_frame`）—— 逐个实测通过

用产品自身的 `extract_frame(width=768)` 对两种文件各取 3 帧：

| 文件 | 时间点 | 结果 |
| --- | --- | --- |
| 0008（10.8s / 147 MB） | 1.0s / 5.0s / 9.0s | ✅ 57 KB / 78 KB / 52 KB，6.8–7.9s |
| 0007（140.8s / 1.9 GB） | 25s / 60s / 120s | ✅ 68 KB / 58 KB / 52 KB，6.7–7.0s |

**6/6 成功**，大文件与小文件耗时相近（约 7s/帧），说明 `-ss` 定位不受文件长度影响。

### 多帧动态理解（TSB-307）

```
6f1ace17 method=multiframe pan             risk=medium at=[5.17, 6.33, 7.5, 8.67, 9.83]
6f6a7226 method=multiframe pan             risk=medium at=[2.67, 5.33, 8.0, 10.67, 13.33]
838ec03a method=multiframe tracking        risk=medium at=[53.5, 56.0, 58.5, 61.0, 63.5]
bcda9841 method=multiframe subject_action  risk=medium at=[122.0, 125.0, 128.0, 131.0, 134.0]
```

每个结论都带**实际采样时间戳**，且 `evidence.audio_analyzed=false`、
`video_analyzed=false`。模型的措辞也守住了边界，例如
"…suggesting possible panning or handheld movement that stills cannot confirm"。

### 动态复核候选（TSB-306）

98 个候选，来源分别标注 `semantic` / `rule`。规则来源的理由串固定为
「疑似画面变化，需核验；帧差本身不能证明抖动或甩镜」，
单帧镜头标记为无法判断运动而非排除。

---

## 五、验收过程中发现并修复的两处产品问题

### 5.1 任务未注册（驱动脚本接线缺口）

首轮 `vision_analyze` 报 `Unknown job type: vision_analyze`。
原因是任务注册是 `app.main` 里的**显式调用**，而验收脚本没有 import `app.main`。
REST 路径不受影响。修复：脚本显式调用 `register_*_tasks()` 并在开跑前校验
四个类型都在 `TASK_REGISTRY` 中；另加两个回归用例断言类型同时存在于
`VALID_JOB_TYPES` / `TASK_REGISTRY` / `EXCLUSIVE_JOB_TYPES`。

### 5.2 推理模型的输出预算过小

`GLM-5.3-Flash` 把 1500 token 的预算耗在独立的 `reasoning` 字段上，
使 `content` 返回 `null`，报
`[MODEL_INVALID_OUTPUT] model returned reasoning without content (raise max_tokens)`。

**适配器没有把它当成成功**——否则会持久化一条空观察。
修复：预算改为可配置（默认 4000），并新增两个接线用例断言任务把**配置值**
传给 provider 而非硬编码。修复后 motion_review 由 `partial_failed` 转为
`succeeded` 4/4。

---

## 六、C1 原片只读

全部 6 个原片在完整流程（扫描 → 检测 → 语义分析 → 多帧复核）前后，
SHA-256 / 大小 / mtime_ns **完全一致**：

```
c1_originals_unchanged: true
c1_problems: []
```

---

## 七、诚实边界

1. **本记录是 Windows，不是 macOS**。任务书假设 macOS 为主测平台；
   本次在 Windows 上执行，两者环境不同（AGENTS.md 4.1 与 macOS 记录第一节的差异）。
2. **素材维度不全**：无竖屏、无无音轨、无 HDR，6 段少于门禁要求的 8～10 段。
3. **原生视频理解未实现**：本轮按任务书只做"时间戳多帧"，
   `supports_native_video` 默认 `false`，多帧推断**不等于**连续视频分析。
4. **音频语义完全未分析**，`audio_analyzed` 恒为 `false`（即使素材带音轨）。
5. **60+ 段 / 200GB 全量性能未测**。本机 4K120 HEVC 解码仅约 1/7 实时速度，
   这些文件的全量 detect 已需 26 分钟，200GB 在本机**不具可行性**，
   必须换更强算力或降低采样率；不依据样本耗时外推结论。
6. **模型端点为明文 `http://`**，令牌与图片在网络上不加密。
7. **人脸/车牌未脱敏**：EXIF/GPS 去除是强制的，但不遮蔽画面内容。
8. 本轮的 `vision_analyze` 只覆盖 **8 / 99** 个镜头，其余 91 个
   `not_covered` 是**如实报告**的覆盖缺口，不是已完成项。
