# TSB-330 — A/B 评价方案（预注册）

**状态**：脚手架就绪，**真实评价未完成（`blocked_by_endpoint`）**。
**结论措辞**：本轮**没有任何 A/B 结果**，不得引用为"视频理解更准"的证据。

---

## 1. 三臂定义

| 臂 | 输入 | 本轮状态 |
| --- | --- | --- |
| **A** `A_image_only` | 1 张代表帧的语义结果 | 可用（既有 M5.1 结果） |
| **B** `B_multiframe` | 现有 5 帧带时间戳动态复核 | 可用（既有 M5.2 结果） |
| **C** `C_video_native` | 派生短片原生视频输入 | **不可用** —— 端点当前拒绝视频 |

**C 组缺失的原因**见 [`CAPABILITY_REPORT.json`](CAPABILITY_REPORT.json)：
vLLM 报 `At most 0 video(s) may be provided in one prompt`，
即该部署的 `--limit-mm-per-prompt` 把视频上限设为 0。这是**服务端配置**，
不是协议错误、也不是模型不支持。

---

## 2. 实验设计

- **同一批镜头**跑三臂。差异必须归因于**输入方式**，而不是抽样不同。
- 目标样本 **20～30 个不同 Segment**，跨场景/镜头长度/静态-动态类型分层。
- **必须记录来源视频数**：同一源片的不同镜头**不是**独立样本。
  报告中的 `sampling_note` 会自动写明"N 个镜头来自 M 个源视频"。
- 可另加"高密度本地抽帧"作为对照，以区分**增加时间采样**与**改变传输格式**的作用。

### 人工标注口径

标注文件为 JSON：`{segment_id: [{start_abs_sec, end_abs_sec, description}]}`，
时间一律用**源视频绝对秒**（与存储层一致，避免两套坐标）。
评价者**尽量不知道**该条来自哪一臂。

标注缺失时，harness **不产生任何准确率数字**，只报告覆盖率并把状态标为
`not_annotated`。这是刻意的：编造一个看起来合理的数字，比没有数字危害大得多。

---

## 3. 指标

| 指标 | 定义 |
| --- | --- |
| `event_recall` | 被匹配上的标注事件 / 全部标注事件 |
| `event_precision` | 被匹配上的预测事件 / 全部预测事件 |
| `temporal_IoU ≥ 0.5` 比例 | 时间窗重合度达标的比例 |
| 时间边界误差（秒） | 匹配事件起点差的绝对值均值 |
| 端到端延迟 p50/p95 | 需要真实运行才有 |
| 单位可用候选成本 | 需要真实用量才有 |

匹配采用**贪心一对一**（重叠优先，每个标注至多用一次）。
用两遍同一个标注会抬高召回，所以代码层面禁止（有用例守护）。

---

## 4. 采纳标准（预注册，执行前固定）

| 条件 | 阈值 |
| --- | --- |
| 事件召回提升 | **≥ 10 个百分点**（C 相对 A） |
| 时间 IoU≥0.5 比例 | **不下降** |
| 误报率 | 不劣于基线 **5 个百分点**内 |
| 人工标注 | **必须有真人标注** |
| 单位成本 | 必须有真实数据 |
| 隐私/性能 | 无 P0 故障 |

阈值写在 `video_ab.ADOPTION_CRITERIA` 里，**执行前固定**——
这不是形式主义：事后挑阈值会让任何结果都"通过"。

**样本量小只能判定"是否值得扩大测试"，不能据此推断所有旅行视频表现。**

---

## 5. 两处必须分开陈述的事

1. **无法比较不同计费形态。** A/B 按 token 计费；C 还要发送
   `clip_seconds` 与 `clip_bytes`。报告中的 `cost_note` 明确写"NOT comparable"，
   不给出单位成本结论。
2. **Mock 通过 ≠ 真实评价通过。** 本轮所有断言都是脚手架自检与合成数据，
   **不构成**任何关于 GLM-5.3-Flash 视频理解质量的证据。

---

## 6. 解除阻塞后怎么跑

```powershell
cd backend

# 1) 服务端加 --limit-mm-per-prompt video=1 并重启部署
# 2) 重跑能力探测，确认 state 变为视频可用
.\.venv\Scripts\python.exe tools\probe_video_capability.py `
    --execute --i-authorize-real-upload `
    --json ..\docs\validation\v2.1.3\CAPABILITY_REPORT.json --redact

# 3) 对选定镜头跑 video_analyze 任务，产生真实的 arm C 结果
#    （经 REST: POST /api/projects/{id}/jobs/video_analyze）

# 4) 人工标注后出报告
.\.venv\Scripts\python.exe tools\evaluate_video_ab.py `
    --project <project_id> --labels labels.json `
    --json ..\docs\validation\v2.1.3\AB_RESULTS.json `
    --markdown ..\docs\validation\v2.1.3\AB_RESULTS.md
```

harness 是**只读**的：它不自己发起模型调用，arm C 是否存在取决于是否已有
`video_analyze` 结果。这样"谁花了钱"始终可回答。
