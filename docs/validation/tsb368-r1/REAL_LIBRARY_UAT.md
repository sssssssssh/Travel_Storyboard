# TSB-368-R1 真实素材库只读验收

**性质**：真实素材、真实数据库、真实浏览器；**全程只读**。
**未上传**任何视频，**未调用**外部模型，**未启动**任何分析任务。

---

## 1. 素材库事实（来自数据库直接统计）

| 项 | 值 |
|---|---|
| 项目 | `M5-real-acceptance` |
| Asset | 6（DJI 4K 航拍） |
| Segment | 99 |
| 源文件可访问 | 5 / 6（1 个缺失） |
| 静帧语义结果 | 14 总 / **8 当前有效** |
| 多帧运动结果 | 10 总 / **4 当前有效** |
| 原生视频观察 | **0** |
| 人工标注 | **0** |
| Segment 状态 | 全部 `candidate` |
| 人工评分 | 全部 `pending` |

> 表内为汇总统计。源文件路径、完整文件名与画面内容不写入仓库。

---

## 2. API 验收（12/12 PASS）

### 2.1 覆盖率一致性

```
DB  truth : segments=99 assets=6 semantic_current=8 motion_current=4 video_current=0
APPLICATION coverage:
  library_segments=99  eligible=99  needs_review=0  excluded=0
  static_valid=8  multiframe_valid=4  video_native_valid=0
  segments_with_any_valid_reading=10  no_valid_model_reading=89
  coverage_complete=false
```

| 检查 | 结果 | 证据 |
|---|---|---|
| 1. library == DB Segment | PASS | 99 == 99 |
| 2. 三资格之和 == library | PASS | 99 + 0 + 0 = 99 |
| 3. 分渠道有效 == DB 当前 | PASS | 8/4/0 == 8/4/0 |
| 4. 分渠道 ≤ library 且去重正确 | PASS | any=10 ≤ 99 |
| 5. no_valid + any_valid == library | PASS | 89 + 10 = 99 |

### 2.2 新鲜度诚实性

页面/API 上 8 条静帧 + 4 条多帧全部标为 `freshness_unverified`，
**没有任何一条**被标成 `current`。

这是因为：
- 静帧的 `source_signature` 需要重算才能确认（默认 `verify_freshness=false`，不读源文件）；
- 多帧的 `input_signature` 无法离线重算。

**这是如实反映，不是缺陷**。

### 2.3 其余检查

| 检查 | 结果 |
|---|---|
| 6. 新鲜度如实 | PASS |
| 7. 人工评分与资格一致 | PASS（99 全 pending，excluded=0） |
| 8. needs_review/excluded 不进 context | PASS（泄漏 0） |
| 9. digest 重复请求一致 | PASS |
| 10. 分页无重复、total 一致 | PASS（30+30，重叠 0，total 99） |
| 11. 无路径/令牌/原始 payload | PASS（命中 0） |
| 12. 未创建 Job/版本、未改人工数据 | PASS（前后完全一致） |

### 2.4 实测延迟（99 镜头，冷启动后）

| 请求 | 耗时 |
|---|---|
| `eligibility=all&limit=100` | **124 ms** |
| `eligibility=eligible` | 24 ms |
| `eligibility=needs_review` | 80 ms |
| `eligibility=excluded` | 15 ms |
| `context?max_candidates=40` | 17 ms |

SQL 语句数保持恒定（见 `test_tsb368_query_cost.py`：4→1000 镜头均为 7 条）。

---

## 3. 页面验收（8/8 PASS，真实 Chrome）

| 检查 | 结果 | 细节 |
|---|---|---|
| 页面覆盖率 == API | PASS | 99 / 10 / 89 三处一致 |
| 三渠道独立徽标 | PASS | 静帧 8 未验证+12 未分析；多帧 2+18；原生 20 全未分析 |
| 未分析真实展示 | PASS | `current` 计数 **0**，无镜头被伪称已验证 |
| 资格筛选不改变分母 | PASS | all/eligible/needs_review/excluded 分母均为 99 |
| 分页 | PASS | `1–20 / 99` → `21–40 / 99` |
| 跨素材多样性 | PASS | 首页覆盖 **6** 个不同素材 |
| 零写请求 | PASS | 全程仅 GET |
| 无应用异常 | PASS | 仅 favicon 404（项目本无图标） |

**UI 限制（如实记录，非缺陷）**：素材筛选下拉只列出当前页出现的素材，
不是全局 Asset 列表。任务书 §5.3 明确允许将其记录为 UI 限制。

---

## 4. 真实内容抽样核对（25 个镜头）

抽样规则：优先覆盖有读数的镜头，再用未分析镜头补足；每类数量如实统计。

### 4.1 多帧结果样例（真实模型输出）

> "Scenic lakeside shot with paddle boats. Across the frames the camera steadily
> pans left: the white pagoda on the right drifts rightward in frame (t=5.17s to
> t=8.67s) and exits by t=9.83s, while willow branches, a stone balustrade and a
> sign enter from the left. ... **The reframing between t=8.67s and t=9.83s is
> noticeably larger than between earlier frames; the sampling cannot confirm
> whether this was a fast continuation of the pan or a cut.**"

**评价**：描述了具体可见对象（宝塔、柳枝、石栏、踏船）、给出逐帧时间点、
并在证据不足处**主动声明无法确认**。没有编造地点名或人物身份。

其他 3 条当前有效的多帧结果同样具体：清真寺内部（吊灯、彩窗、红毯、穹顶）、
手持穿行（追踪人物）、主体动作（举手→放下后双手持手机）。

当前有效的多帧结果共 **4 条**，对应 4 个不同镜头（`6f6a7226`、`6f1ace17`、
`838ec03a`、`bcda9841`），已逐条核对。

### 4.2 `content_mismatch` 记录

| 类别 | 数量 |
|---|---|
| 描述与时间窗错配 | **0** |
| 静帧描述被误当连续运动 | **0** |
| 事件越出镜头窗口 | **0**（越界事件已在聚合层丢弃） |
| 未分析镜头出现 AI 描述 | **0** |

### 4.3 发现的真实缺陷（详见 RELEASE §5.5）

抽样过程中发现 8 条真实静帧结果的 `scene_tags`/`subjects`/`visible_actions`
全部为空。**经查证为代码缺陷，不是数据缺失**：

- 生产写入格式：`analysis_json` = **扁平**（`semantic._store_result` 存 `validated.visual`）
- 代码读取格式：**嵌套** `analysis_json["visual"][...]`
- 全库分布：嵌套 772 条、扁平 14 条 —— **两种格式并存**

修复后真实内容立即恢复（样例见 RELEASE §5.5 表格）。

---

## 5. 只读性证明

查询前后数据库快照完全一致：

```
before: segments=99 semantic=14 motion=10 video=0 annotations=0 versions=22 jobs=23
after : segments=99 semantic=14 motion=10 video=0 annotations=0 versions=22 jobs=23
```

- 未创建任何 Job；
- 未创建任何 StoryboardVersion；
- 未新增/修改任何 `UserAnnotation`；
- 未改动任何 Segment/Asset 行；
- 浏览器全程仅发出 GET 请求。

---

## 6. 无法验收的维度（BLOCKED）

| 维度 | 原因 |
|---|---|
| **原生视频渠道真实效果** | 本库 `SegmentVideoObservation` 为 **0 条**。需要一次真实视频分析才能验证。 |
| **人工标注真实效果** | 本库 `UserAnnotation` 为 **0 条**。`favorite`/`exclude`/备注的真实行为只能靠单元测试验证。 |
| **`verify_freshness=true` 的 I/O 成本** | 任务书要求先征得用户同意并对受控小批评估；本轮未执行，默认关闭。 |
| **人工观看比对（画面 vs 摘要）** | 需要人在本地逐条观看原片。本轮以多帧结果的自洽性交叉验证替代，**不等于**人工逐条确认。 |

**这些维度不能声称通过。**
