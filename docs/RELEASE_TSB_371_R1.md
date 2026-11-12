# TSB-371-R1 发布记录

| 字段 | 内容 |
|---|---|
| 前置版本 | TSB-371 |
| 前置 HEAD | `46f8f36fc7012515ae8cc145320ec604790a53c5` |
| 任务书 | [docs/tasks/TSB-371-R1.md](tasks/TSB-371-R1.md) |
| 验收记录 | [docs/acceptance/TSB-371-R1-ACCEPTANCE.md](acceptance/TSB-371-R1-ACCEPTANCE.md) |
| 最终 HEAD | `e62bb71` |
| CI | run #154，**7/7 success** — https://github.com/sssssssssh/Travel_Storyboard/actions/runs/43180317552 |

---

## 一、本轮解决了什么

TSB-371 做完了评价界面，但**评价结果没有真正影响任何决策**：

- `build_planning_input()` 从不读评价表——判错的句子照旧进入故事线；
- 只有静帧能逐句评价，多帧与原生视频的读数无人可评；
- 服务端不校验 claim_key 是否存在；
- A/B 的内容指纹把 `planning_mode` 算进去，**内容相同的方案被判为不同**；
- A/B 回执只查字段非空，**客户端可以自己填**；
- 已提交的 A/B 刷新后**丢回盲评视图**；
- 媒体接口**不检查时效**，过期片段照常播放；
- 每次查评价是 N+1（100 镜头 300 次查询）；
- G3/G4 状态是**硬编码字符串**，写完之后永远不变。

本轮把这些逐个修掉，并为每条建立了回归测试。

---

## 二、每个 P1 的修改前后

### P1-01 人工评价进入策划

| | 修改前 | 修改后 |
|---|---|---|
| 读取评价 | 不读 | 构建 PlanningInput 时批量读取 |
| 判错的断言 | 照常进入方案 | 从证据中移除 |
| 正确断言 | — | 保留（部分判错不影响兄弟断言） |
| 无法判断 | — | 不作为错误 |
| 未评价 | — | 完全沿用原行为 |
| 镜头本体 | — | **不排除、不缩窗口**（§2：描述错误≠镜头没价值） |
| 可追溯 | 无 | 每条证据带 `human_review_*`；review 摘要折入 digest |

### P1-02 三渠道评价

| 渠道 | 修改前 | 修改后 |
|---|---|---|
| 静帧 | ✅ | ✅（不变） |
| 多帧 | ❌ 无评价项 | motion_type / stability_risk / action_summary / 帧时间 |
| 原生视频 | ❌ 无评价项 | summary / events[].description / 事件窗口 |

字段全部取自**真实存储结构**，模型没输出的字段不产生断言。

### P1-04 claim 完整性

| | 修改前 | 修改后 |
|---|---|---|
| 未知 key | 静默接受 | `422 EVAL_CLAIM_UNKNOWN` |
| 跨项目 key | 静默接受 | 404 |
| 重复 key | 双倍计数 | 去重，后者覆盖 |
| 报告 | 只有一个总准确率 | 每条断言的原文+判断+渠道+是否计入 |

### P1-03 A/B 可信度

| | 修改前 | 修改后 |
|---|---|---|
| 内容指纹 | 含 planning_mode 等元数据 | 只含剪辑内容 |
| 伪造回执 | 通过 | 拒绝（客户端回执=无证据） |
| 服务端记录 | 不存在 | `model_call_records` 表 |
| 失败调用 | — | 不算证据 |
| 跨项目记录 | — | 不算证据 |

### P1-05 已提交恢复

| | 修改前 | 修改后 |
|---|---|---|
| 提交后刷新 | 回盲评，揭示丢失 | 服务端返回揭示视图 |
| 客户端请求揭示 | — | 只记录，不覆盖真相 |
| 重复提交 | — | 409 |

### P1-06 媒体时效

| | 修改前 | 修改后 |
|---|---|---|
| 源文件变化 | 照常播放 | `409 EVAL_CLIP_STALE` |
| 镜头窗口变化 | 照常播放 | 同上 |
| 重新生成 | — | 清除过期状态 |

---

## 三、CI 结果

| run | 结论 | 说明 |
|---|---|---|
| #153 | ❌ | `test_no_absolute_path_is_ever_returned` 在 Linux 失败：测试断言临时目录名不出现，而实现**故意**返回文件夹名作提示 |
| **#154** | ✅ **7/7** | 改为断言结构（不以 `/` 开头、无盘符、无分隔符），语义正确且跨平台 |

| 作业 | #154 |
|---|---|
| `Frontend` | ✅ |
| `Required FFmpeg (windows-latest)` | ✅ |
| `Required FFmpeg (ubuntu-latest)` | ✅ |
| `Backend (Python 3.11)` ×2 | ✅ |
| `Backend (Python 3.13)` ×2 | ✅ |

### 本轮第二次「本地通过、CI 失败」

两次都是**测试依赖本机环境**：

1. `KeyError: 'LOCALAPPDATA'` —— 模块导入期读 Windows 专有变量
2. 路径分隔符语义 —— 断言子串在 Windows 恰好不命中，在 Linux 命中

第 2 次尤其说明问题：原断言在 Windows 上"正确"是**巧合**，
它检验的不是「没有绝对路径」，而是「没有出现某个临时目录名」。

## 四、测试与 CI

### 新增测试

| 文件 | 数量 | 覆盖 |
|---|---:|---|
| `test_tsb371r1_planning_feedback.py` | 14 | 判错进策划、部分正确、不确定性、过期、不污染 |
| `test_tsb371r1_channels.py` | 9 | 三渠道断言、无编造字段、保存恢复 |
| `test_tsb371r1_integrity.py` | 12 | 非法/重复/跨项目 key、报告定位 |
| `test_tsb371r1_ab_trust.py` | 15 | 内容指纹、伪造回执、服务端记录 |
| `test_tsb371r1_ab_resume.py` | 10 | 提交后刷新、只读、不可重提 |
| `test_tsb371r1_clip_staleness.py` | 12 | 过期、穿越、跨项目、Range/HEAD、源只读 |
| `test_tsb371r1_perf.py` | 6 | 100/1000 查询数不增长 |
| `test_tsb371r1_concurrency.py` | 7 | 409、快速连点、乱序、多线程 |
| `test_tsb371r1_gates.py` | 11 | NOT_RUN/PASS/BLOCKED 由证据决定 |
| **合计** | **96** | |

### 修正的旧测试（非降低断言）

| 文件 | 处数 | 原因 |
|---|---:|---|
| `test_tsb371_ratings.py` | 6 | 用了不存在的 claim key；以前能过是因为**没有校验** |
| `test_tsb371_feedback.py` | 1 | 同上 |
| `test_tsb371_ab.py` | 1 | `_with_receipt` 凭空造回执，现改为写真实记录 |
| `test_tsb371_report.py` | 2 | CSV 单位改为 claim；G4 新增 `NOT_RUN` |

---

## 四、数据库迁移

全部为**前向增量**，`ADDITIVE_MIGRATIONS` 自动执行，不重建表：

| 表 | 列 | 用途 |
|---|---|---|
| `model_call_records` | 新表 | 服务端调用证据 |
| `assets` | `source_state` / `source_retired_at` / `source_retired_note` | 废弃源标记 |
| `evaluation_items` | `frame_ids` / `readings_json` | 冻结断言与代表帧 |
| `evaluation_ratings` | `claim_verdicts` | 逐句判断 |

**历史数据兼容**：无 `claim_verdicts` 的旧评价按维度口径统计，
并在报告中标 `metric_basis: "dimensions"`。

---

## 五、三层验收（不可混谈）

| 层级 | 状态 |
|---|---|
| 代码测试通过 | ✅ |
| 模型真实调用通过 | ⬜ `NOT_EXERCISED` |
| 人工素材验证通过 | 🔒 `MANUAL_REVIEW_PENDING` |

**本轮完成不代表 AI 剪辑系统通过真实素材验收。**

---

## 六、遗留

| # | 项 | 性质 |
|---|---|---|
| 1 | 真实模型调用未发起 | 端点可用，本轮无此要求 |
| 2 | 评审样本因 1 段素材缺失而收窄（99→61） | 已标记并披露 |
| 3 | 单人 A/B 不构成人群胜率 | 设计限制，报告已明示 |
| 4 | ~~并发用例间歇失败~~ | ✅ 已定位并修复：并发首次保存撞 UNIQUE 约束返回 500 |


---

## 七、最终状态

| 项 | 值 |
|---|---|
| 最终 HEAD | `e62bb71` |
| CI | run #154 **7/7** |
| 后端全量 | **2241 collected / 0 failed / 0 errors / 6 skipped** |
| 前端 | **182 passed**，typecheck / lint / build 全绿 |
| 新增测试 | **96 项** |

**本轮可宣称**：人工判错能真实改变策划输入；三渠道可逐句评价；
不可信 A/B 被拒绝；过期素材不播放；查询不随规模增长；状态由证据决定。

**本轮不可宣称**：AI 识别准确率、模型方案优于离线方案、系统通过真实素材验收。
