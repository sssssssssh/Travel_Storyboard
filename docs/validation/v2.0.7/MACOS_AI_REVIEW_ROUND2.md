# macOS 真实素材 · 第二轮：AI 复核回路与 C2/C4 回归

**日期**：2026-10-15
**机器**：macOS 15.7.7 arm64，FFmpeg 9.0.2
**项目**：`14913db3-cf6c-4252-b6c9-c9e1945899ce`
**素材**：`/Users/shush/Downloads/video`（6 段 DJI 4K HEVC，4.4 GB，只读）
**上一轮**：[MACOS_REAL_FOOTAGE.md](MACOS_REAL_FOOTAGE.md)
**结论**：**上一轮遗留的主要缺口（人工复核回路）已补齐并通过；
C2/C4 在强制重检测后确认保留。发布门禁仍未完成（见第七节）。**

---

## 一、本轮补的缺口

上一轮明确列出的"尚未完成"里，第 2 项是：

> 人工复核回路未跑 —— 未导入 review requests，因此 proxies 编码路径、
> 复核处置（disposition）、计划引用稳定性均**未**在真实素材上验证。

本轮把这条链路跑完了。

---

## 二、AI 复核是怎么做的（与产品内置路径的区别）

产品的内置 AI 分析入口是 `POST /api/projects/{id}/story/analyze` →
`direct_analyze()`，它需要一个**已配置的 OpenAI 兼容视觉模型**
（`TRAVELSB_MODEL_BASE_URL` + `TRAVELSB_MODEL_API_KEY`）。

**本机未配置该模型**（`/api/system/config` 返回 `model_configured: false`，
且 `backend/.env` 不存在），因此内置视觉路径**未被本轮覆盖**。

替代做法：`backend/tools/ai_review.py` 用**可测量的信号**代替主观描述——
从每个镜头真实解码的代表帧计算帧间差异，再结合检测阶段已测得的曝光/清晰度指标：

| 信号 | 来源 | 用途 |
| --- | --- | --- |
| `motion_median` | 同镜头代表帧两两 `cv2.absdiff` 均值 / 255 | 疑似抖动或快速甩镜 |
| `overexposed_ratio` / `underexposed_ratio` | 检测阶段的 `quality_metrics` | 高光/暗部是否有可用细节 |
| `blur_avg` | 检测阶段的拉普拉斯方差 | 是否失焦 |

判据（区间刻意取宽，宁可多送审也不漏）：

- `motion_median ≥ 0.20` → high；`≥ 0.12` → medium
- `overexposed_ratio ≥ 0.15` → high；`underexposed_ratio ≥ 0.35` → high
- `blur_avg < 120` → high

**为什么这样更可靠**：AI 复核的价值在于"指出该看哪里"，而这个判断必须可复算。
帧间差异是确定性的，任何人重跑都得同一结论；纯文字推理则不可复现。

### 覆盖度与已知局限（诚实声明）

- 46 个时长 ≥2s 的候选镜头，**24 个可分析**（有 ≥2 张可读帧），22 个只有 1 张帧。
- 帧数分布实测：**1 帧 22 个 / 2 帧 21 个 / 3 帧 3 个**。
  这是检测阶段的采样策略（短片抽 1 帧，长片抽 2–3 帧），**不是缺陷**。
- 因此**本轮的抖动判据对那 22 个单帧镜头不可用**——单帧无法测运动。
  这是本工具的局限，不是产品的。若要覆盖它们，需在检测阶段提高采样上限。
- 24 个的 motion 分布：`min=0.085 p50=0.184 max=0.297`。

---

## 三、复核请求导入

`POST /api/projects/{id}/review-requests/import`，来源标记 `source=ai`。

```
HTTP 200  {"created": 12, "errors": [], "duplicates": []}
```

**12 条全部通过服务端校验**（`validate_review` 会拒绝不存在的
asset/segment、越界区间、失效镜头）。0 错误、0 重复。

原始文件：[ai-review-requests.json](ai-review-requests.json)
（另存导入后的状态快照：[ai-review-requests-after.json](ai-review-requests-after.json)）

标的（按 motion 降序取前 12）：

| motion | 区间 | 优先级 | 理由 |
| --- | --- | --- | --- |
| 0.297 | 15.00–20.50 | high | 疑似抖动或快速甩镜 |
| 0.296 | 15.00–23.00 | high | 同上 |
| 0.290 | 93.00–101.00 | high | 同上 |
| 0.274 | 103.00–112.00 | high | 同上 |
| 0.252 | 68.00–80.00 | high | 同上 |
| 0.226 | 3.00–9.00 | high | 同上 |
| 0.218 | 85.00–91.00 | high | 同上 |
| 0.213 | 113.00–137.00 | high | 同上 |
| 0.212 | 51.00–66.00 | high | 同上 |
| 0.202 | 0.00–16.00 | high | 同上 |
| 0.201 | 19.00–24.00 | high | 同上 |
| 0.184 | 44.00–51.81 | medium | 运动幅度需确认 |

---

## 四、代理生成（上一轮完全没覆盖的路径）

上一轮 `proxies` 的统计是 `total: 0`——因为当时没有任何 ReviewRequest。
本轮有 12 条待处理请求，重跑：

```
POST /api/projects/<id>/jobs/proxies  {"max_total_mb": 300}
→ succeeded  12/12  failed=0
```

### 产物实测（不是"文件存在"）

**12 个文件全部可读**（`ffprobe` 逐个校验），规格 **480×270 h264 + aac**，
与 `TRAVELSB_PROXY_WIDTH=480` 一致。

**时长逐条比对**（AGENTS.md §5.5：容差 `max(0.25s, 10%)`）：

| 请求时长 | 实测 | 容差 | 结果 |
| --- | --- | --- | --- |
| 5.00 | 5.00 | 0.50 | OK |
| 5.50 | 5.51 | 0.55 | OK |
| 6.00 | 6.01 | 0.60 | OK |
| 6.00 | 6.01 | 0.60 | OK |
| 7.81 | 7.81 | 0.78 | OK |
| 8.00 | 8.01 | 0.80 | OK |
| 8.00 | 8.01 | 0.80 | OK |
| 9.00 | 9.00 | 0.90 | OK |
| 12.00 | 12.00 | 1.20 | OK |
| 15.00 | 15.01 | 1.50 | OK |
| 16.00 | 16.01 | 1.60 | OK |
| 24.00 | 24.01 | 2.40 | OK |

**12/12 通过，0 例超差。**

**HTTP 可达性**：12 个全部经产品自身路由
`GET /api/projects/{pid}/review-requests/{rid}/proxy` 返回 **200**，
字节数与磁盘文件一致——即 §5.1 提到的"Starlette FileResponse 长路径 404 变 500"
陷阱在真实产物上**未复现**。

> 说明：`proxy_ref` 字段是**绝对文件系统路径**，不是 HTTP 路由。
> 我一开始拿它直接拼 URL，得到 12 个 404——那是**我的用法错误**，
> 不是缺陷。产品正确路由见上。（此坑记下，避免后来者重踩。）

---

## 五、复核处置（disposition）四条分支

`POST /api/projects/{pid}/review-requests/{rid}/disposition`

| action | 请求 | HTTP | 结果状态 |
| --- | --- | --- | --- |
| `dismiss` | 27e67afa | 200 | `dismissed` |
| `defer` | 3c99322e | 200 | `needs_review` |
| `retry` | 3d552b07 | 200 | `pending` |
| `replace` | 529cdcfa | 200 | 新请求 `b64f838a`，原请求 `superseded` |

**四条分支全部走通。** 逐项状态统计：

```
total: 13
  proxied       8
  pending       2
  dismissed     1
  needs_review  1
  superseded    1
```

**替代血缘双向一致**（C3 稳定映射的关键）：

```
b64f838a  status=pending     supersedes=529cdcfa  superseded_by=-
529cdcfa  status=superseded  supersedes=-         superseded_by=b64f838a
```

`replace` 选中的是**不同**镜头区间（若相同会被
`UNCHANGED_REQUEST` 拒绝），确认服务端确实在防呆。

---

## 六、C2 / C4 回归（本轮最有价值的一项）

AGENTS.md 的 C2（检测边界不可变）与 C4（技术分与人工判断分离）是最容易
在重构中被悄悄破坏的承诺。本轮做了破坏性验证：

### 步骤

1. 对镜头 `abd7bfd5-715f-4c01-af01-bd4a47bbf86b` 写入人工判断：
   `rating=favorite`, `story_role=opening`, `note=开场最佳镜头，保留`
2. 人工裁剪到 **16.0–19.0**（原检测区间 15.0–20.504）
3. 发起**强制重检测** `POST .../jobs/detect {"force": true}` → `succeeded 6/6`
4. 复查同一 `segment_id`

### 结果

```
              重检测前              重检测后
detected      15.0 – 20.503817      15.0 – 20.503817   ← 未被覆盖
current       16.0 – 19.0           16.0 – 19.0        ← 人工裁剪保留
is_trimmed    True                  True
rating        favorite              favorite           ← 保留
story_role    opening               opening            ← 保留
note          开场最佳镜头，保留       开场最佳镜头，保留      ← 保留
```

**C2 PASS · C4 PASS。**

同一 `segment_id` 在强制重检测后仍精确对应，人工字段一个都没丢——
这正是"重新检测按检测区间匹配，因此裁剪、评分、备注必须保留"所要求的。

同时 13 条 ReviewRequest 与 12 个 proxy 引用在重检测后**全部保留**
（`proxies still referenced: 12`，血缘 intact）。

---

## 七、C1 原片只读 —— 再次确认

本轮又跑了检测、代理、粗剪、强制重检测，事后再比对：

```
cde3e08a5c2188f7b7bf5913cdf9763d3d806882149eb26aad39658f040c1e59  DJI_..._0001_D.MP4
b61afd2b73e204ea9c55e5993bc156a4e457b80934338579ad1a2d15b6e409cd  DJI_..._0002_D.MP4
107f65b066b11a55f27fda1e3fe220d938fe143661236dfffa35e906e6df2f3b  DJI_..._0004_D.MP4
7ca5f98031e6107e29399744bc25b24200fe558f9f6185cfd80b764c2d0f1553  DJI_..._0005_D.MP4
78fbe8717df8ed2657205baa9636a0bc142a69c9fd57fbc10cdebffa0d83a867  DJI_..._0007_D.MP4
836bee08fd66b61a90b49b8cde1fb379b8236325b8024fdddf6543fb35229678  DJI_..._0008_D.MP4
```

**与第一轮基线逐字节一致。** 经过扫描→检测→分析包→粗剪→AI复核→代理→
处置→强制重检测这一整套流程，原片零改动。

暂存目录（staging）均为空，清理正常。

---

## 八、自动化回归

| 套件 | 结果 |
| --- | --- |
| 后端 `pytest -q` | **469 passed, 44 skipped（共收集 513）** |
| 前端 vitest | 21 passed（上一轮） |

与本轮手动跑通**互相印证**，无回归。

---

## 九、范围声明

**未改动产品代码。**
**未删除或改写**任何原片、既有测试、既有文档数字。
**新增**：`backend/tools/ai_review.py`（复核分析工具）、本记录、
两份 evidence JSON。

---

## 十、尚未完成（诚实列出）

1. **内置视觉模型路径未覆盖** —— `direct_analyze()` 需要配置 OpenAI 兼容
   视觉模型，本机未配置。本轮的 AI 复核是脚本化的可测量实现，
   **不等于**验证了内置视觉分析。
2. **Windows 实机门禁未执行** —— 仍是 macOS。`LongPathsEnabled=0`、
   Windows Job Object 终止、UNC 长路径均未测。
   结论措辞仍为**「代码级整改通过，发布门禁未完成」**。
3. **取消/并发边界未测** —— 取消时延、进程树回收、409 互斥、
   清理失败可见性、升级前残留展示，本轮仍未覆盖。
4. **单帧镜头的运动判据缺失** —— 22/46 个镜头只有 1 张代表帧，无法测抖动。
5. **素材维度不全** —— 无竖屏、无无音轨、无 HDR；6 段 < 要求的 8～10 段。
6. **60+ 段 / 200GB 全量性能未测**。
7. **~~文档漂移~~ —— 复核后不成立，已更正**。
   第一轮曾判 `local-backend.txt`(469 passed) 与实测(513) 不一致；
   实测两者口径不同且都对：**513 collected = 469 passed + 44 skipped**。
   详见第一轮记录 P2 的更正说明。教训：报测试数字必须写明口径。
