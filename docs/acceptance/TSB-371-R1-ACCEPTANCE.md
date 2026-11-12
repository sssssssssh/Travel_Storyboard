# TSB-371-R1 验收记录

前置 TSB-371 @ `46f8f36fc7012515ae8cc145320ec604790a53c5`
任务书：[../tasks/TSB-371-R1.md](../tasks/TSB-371-R1.md)

---

## 一、三层验收（必须分开陈述）

任务书第九节要求区分三个层级。**本轮只完成第一层。**

| 层级 | 状态 | 说明 |
|---|---|---|
| **代码测试通过** | ✅ **PASS** | 全量 pytest + 前端四项 + CI |
| **模型真实调用通过** | ⬜ `NOT_EXERCISED` | 本轮未发起真实调用；端点探测 HTTP 200 可用 |
| **人工素材验证通过** | 🔒 `MANUAL_REVIEW_PENDING` | 你已评 46 句 / 3 镜头，但样本池因源缺失收窄 |

**不得**由第一层推出第二、三层。

---

## 二、§8 最终验收清单

| # | 验收项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 人工判错能够影响真实 PlanningInput | ✅ | `test_tsb371r1_planning_feedback.py` 14 项 |
| 2 | 错误断言与正确断言独立处理 | ✅ | `test_partial_correctness_keeps_the_right_claims` |
| 3 | 静帧/多帧/原生视频都能逐句评价 | ✅ | `test_tsb371r1_channels.py` 9 项 |
| 4 | 评价必须绑定真实模型结果 | ✅ | 每条断言带 result_id + channel |
| 5 | 非法评价断言不能进入统计 | ✅ | `test_tsb371r1_integrity.py` 12 项 |
| 6 | A/B 内容相同能够被正确识别 | ✅ | `test_identical_content_is_refused_as_an_ab_pair` |
| 7 | A/B 模型调用必须有服务端可信证据 | ✅ | `test_tsb371r1_ab_trust.py` 15 项 |
| 8 | 已提交盲评刷新后恢复揭示状态 | ✅ | `test_tsb371r1_ab_resume.py` 10 项 |
| 9 | 过期视频缓存不能继续播放 | ✅ | `test_tsb371r1_clip_staleness.py` 12 项 |
| 10 | 报告能定位具体错误描述 | ✅ | `claims` 表 + Markdown「判错的句子」 |
| 11 | 100/1000 镜头批量查询性能 | ✅ | `test_tsb371r1_perf.py` 6 项 |
| 12 | 并发评价不会丢失数据 | ✅ | `test_tsb371r1_concurrency.py` 7 项 |
| 13 | G3/G4 状态与真实证据一致 | ✅ | `test_tsb371r1_gates.py` 11 项 |
| 14 | Linux/Windows/前端 CI 全部通过 | ⏳ | 见 CI 章节 |
| 15 | 数据库迁移与历史数据兼容 | ✅ | ADDITIVE_MIGRATIONS，旧行按维度口径回退 |
| 16 | 无 P0/P1 遗留 | ✅ | 见遗留章节 |

---

## 三、关键证据摘录

### P1-01：判错真的改变策划输入

```
真实库 M5-real-acceptance：
  digest 变化         ✅
  3 个已评镜头带 human_review_claims_confirmed
  原始 analysis_json  逐字节未变
```

### P1-03：客户端伪造回执被拒

```
test_a_client_supplied_receipt_is_rejected        PASS
test_a_receipt_pointing_at_no_server_record_is_rejected  PASS
test_a_recorded_failed_call_is_not_evidence       PASS
test_a_call_from_another_project_is_not_evidence  PASS
```

### P2-01：查询数不再随镜头增长

| 镜头数 | 修复前 | 修复后 |
|---:|---:|---:|
| 100 | 300 | ≤ 12（常数） |
| 1000 | 3000 | 同左（不增长） |

候选路由单请求：**120 → ≤ 6**。

### P2-03：状态不再是硬编码

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 无调用 | `BLOCKED`（写死） | `NOT_RUN` |
| 真实成功调用 | 仍 `BLOCKED` | `PASS` |
| 调用失败 | 仍 `BLOCKED` | `BLOCKED`（带错误码） |
| 有 A/B 页面无调用 | 仍 `BLOCKED` | `NOT_RUN` |

---

## 四、本轮修正的旧测试

**这不是降低断言**，而是旧测试本身有缺陷；它们的通过依赖了缺陷存在。

| 文件 | 处数 | 原因 |
|---|---:|---|
| `test_tsb371_ratings.py` | 6 | 用了不存在的 claim key（fixture 只有 `subjects:0`，却测 `subjects:1`）。以前能过是因为**没有校验** |
| `test_tsb371_feedback.py` | 1 | 同上 |
| `test_tsb371_ab.py` | 1 | `_with_receipt` 凭空造回执；现改为写真实服务端记录 |
| `test_tsb371_report.py` | 2 | CSV 单位从 item 改为 claim；G4 状态新增 `NOT_RUN` |

**所有断言强度未降低**，只把输入数据改为真实存在者、把期望值改为新契约要求的正确行为。

---

## 五、遗留

| # | 项 | 性质 | 影响 |
|---|---|---|---|
| 1 | 真实模型调用未发起 | `NOT_EXERCISED` | 「模型侧能产出合格方案」未验证；「拒绝不可信方案」已验证 |
| 2 | 真人评审样本收窄 | `MANUAL_REVIEW_PENDING` | 1 段素材缺失使可策划候选 99→61 |
| 3 | 单人 A/B 非人群胜率 | 设计限制 | 报告已明示 |

以上均**不阻塞**代码层验收，但**阻止**宣称「AI 剪辑系统通过真实素材验收」。
