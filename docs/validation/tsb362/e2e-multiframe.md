# TSB-362 阶段 C — 真实旅行素材正式链路 E2E（多帧模式）

执行：2026-11-04　用户明示授权：**允许把抽帧 JPEG 外发**、选定素材并确认继续
隔离根目录：`<隔离盘>\tsb-e2e-362`（**仓库之外**，不入 Git；本机绝对路径不写入文档）

> 本轮外发的是 **35 张抽帧 JPEG**（7 镜头 × 5 帧），**原视频文件未外发**。
> 数据写入**独立 SQLite**，正式库 `backend/data/travel_storyboard.db` **未被触碰**。

## 一、素材（用户提供，只读）

| 项 | 值 |
|---|---|
| 文件 | `<user-clip>.MP4`（真实文件名含设备与拍摄时间信息，**不写入文档**） |
| 时长 | **10.76 s** |
| 规格 | 3840×2160 HEVC **120 fps**，含 AAC 音轨 |
| 大小 | 153 943 257 B |
| **SHA-256** | `836bee08fd66b61a90b49b8cde1fb379b8236325b8024fdddf6543fb35229678` |

## 二、链路（全部走正式 API，未调用 Worker 私有函数）

```
POST /api/projects                    → 建隔离项目（source_roots=素材目录, output_dir=隔离工作区）
POST .../jobs/scan                    → 1 素材入库
POST .../jobs/detect                  → 7 镜头
POST .../jobs/package / proxies / roughcut → 打包/代理/粗剪
POST .../jobs/motion_review           → 多帧 AI 分析（upload_acknowledged=true, frame_count=5）
GET  .../motion-reviews               → 核验入库
```

服务端：`uvicorn app.main:app`，`TRAVELSB_DATA_DIR=<隔离盘>\tsb-e2e-362\data`（**独立库**）。

## 三、各阶段结果

| 阶段 | 状态 | 关键数字 |
|---|---|---|
| scan | **succeeded** | 1 asset |
| detect | **succeeded** | **7 segments** |
| package | **succeeded** | 1 package |
| proxies | succeeded | total=0（本项目无需代理） |
| roughcut | **succeeded** | 7/7，下载 HTTP 200 / 6 461 032 B |
| **motion_review** | **succeeded** | **7/7 analyzed, 0 failed** |

`e2e_drive.py` 的 C1 校验与最终判定：

```
== 7. verify originals unchanged (C1) ==
  OK — 1 files byte-identical, no additions/deletions
VERDICT: PASS
```

## 四、motion_review Job 终态（真实 Runner 推进）

```json
{"status":"succeeded",
 "stats":{"total":7,"done":7,"failed":0,"analyzed":7,
          "frames_sampled":35,"method":"multiframe","calls":7,"images_sent":35,
          "usage":{"prompt_tokens":19608,"completion_tokens":7981,"total_tokens":27589},
          "cleanup":{"ok":true,"removed_files":35,"removed_bytes":1833771,
                     "residual_count":0,"residual_bytes":0}},
 "failures":{"count":0,"items":[]},
 "fallback":{"state":"none"}}
```

- Job 状态由 **pending → running(sampling) → running(analyzing) → succeeded**，非伪造。
- **暂存清理：35 个临时帧全部删除，残留 0**。

## 五、入库核验（V10：精确匹配本次素材/镜头/区间）

7 条 `SegmentMotionReview`，`method=multiframe`、`is_current=true`，
每条的 5 个帧时间戳**全部落在该镜头自己的区间内**：

| 镜头（前 8 位） | 区间 | 帧时间戳 |
|---|---|---|
| `bd60a16b` | 0.000–2.000 | 0.333, 0.667, 1.000, 1.333, 1.667 |
| `75b0417d` | 2.000–3.000 | 2.167, 2.333, 2.500, 2.667, 2.833 |
| `4c8576b4` | 3.000–7.000 | 3.667, 4.333, 5.000, 5.667, 6.333 |
| `4c8a408c` | 7.000–8.000 | 7.167, 7.333, 7.500, 7.667, 7.833 |
| `ac0691e5` | 8.000–9.000 | 8.167, 8.333, 8.500, 8.667, 8.833 |
| `a6fb3e29` | 9.000–10.000 | 9.167, 9.333, 9.500, 9.667, 9.833 |
| `17c0c615` | 10.000–10.761 | 10.127, 10.254, 10.380, 10.507, 10.634 |

全部 7 条共享同一 `source_signature`，指向本次唯一素材，
**未混入任何历史记录**（隔离库中本就无历史）。

## 六、模型输出的内容质量（人工抽查）

模型**没有幻觉**，且**主动声明能力边界**——每条都带：

- `"video_analyzed": false`（明确标注这不是原生视频分析）
- `"audio_analyzed": false`（音频未分析）
- `limitations` 里写明"帧间运动是推断的，不是观测到的"

抽查几条（**脱敏摘要，不含完整画面描述**）：

| 镜头 | 模型判断 | 是否合理 |
|---|---|---|
| `17c0c615` | 手持/步行下台阶的移动感，无法从静帧确认是否平稳 | ✅ 诚实 |
| `ac0691e5` | 密集人群中的手持移动 | ✅ 与素材场景一致 |
| `4c8576b4` | 区分"跟拍"与"摇镜"依据是静帧视差，**可能误判**；主体动作（脱鞋、数票）与相机运动重叠，主导运动类型**不明确** | ✅ 极诚实 |
| `75b0417d` | **`needs_human_review: true`**，因为 2.33–2.50 s 有前景遮挡（头巾/肩部）挡住部分画面 | ✅ **正确识别出真实遮挡** |
| `bd60a16b` | t=0.33 s 被背包严重遮挡 | ✅ **正确** |

> **关键观察**：模型在两处标出了**真实存在的遮挡**，说明它确实在"看"画面，
> 而不是套模板作答。

## 七、安全与不可变核验

| 检查点 | 结果 |
|---|---|
| V11 原片 SHA-256 | ✅ **完全一致** `836bee08…229678` |
| 原片大小 / mtime | ✅ 未变（153 943 257 / 10-03 16:50:22） |
| 素材目录新增文件 | ✅ **无** |
| 正式库记录数 | ✅ **7/9/102/48，与运行前完全一致** |
| 隔离库 | 1 project / 1 asset / 7 segments / 6 jobs / 7 motion reviews |
| 暂存残留 | ✅ 0 B / 0 文件 |
| 人工状态（评分/收藏/备注/裁剪） | 全部 `pending`，未被自动改写 |

## 八、与 G5 的区分（不要混淆）

| 问题 | 本轮结论 |
|---|---|
| 正式链路能否跑通？ | ✅ **能**（阶段 C 完成） |
| 结果能否入库？ | ✅ **能**（7/7 精确匹配） |
| 前端/API 能否读取？ | ✅ **能**（`GET /motion-reviews` 返回 7 条） |
| **是否用了原生视频？** | ❌ **没有**——`method=multiframe`，模型明确 `video_analyzed:false` |
| **G5 结论有变化吗？** | ❌ **没有**，仍是 `blocked_by_endpoint` |

**这是多帧抽帧方案的验收通过，不是原生视频能力的验收通过。**

## 九、成本

| 项 | 数量 |
|---|---|
| 模型调用 | **7 次**（1 次/镜头） |
| 发送图片 | **35 张** JPEG |
| tokens | 27 589（prompt 19 608 + completion 7 981） |
| 外发视频字节 | **0**（原视频未发） |

## 十、局限

1. 只有**1 段素材**、7 个镜头，不构成统计意义上的验收（任务书要求 8–10 段）。
2. 模型输出**未做人工真值对照**——本轮只核验"链路与入库"，未做内容准确率评估
   （那需要你逐条确认画面真实内容，属 G6 范畴）。
3. 120 fps 4K 素材检测出 7 个镜头，镜头粒度与实拍节奏是否合理未做人工复核。
