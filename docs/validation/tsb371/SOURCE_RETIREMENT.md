# TSB-371：废弃源素材的处理

## 背景

你的素材库 6 段里有 1 段文件不在了：

```
DJI_20261003164352_0007_D.MP4   1.9 GB   38 个镜头
```

你确认「不要了」。

---

## 做了什么：只打标记，不删数据

新增 `backend/app/services/evaluation_retire.py`，在 `assets` 表上加了三个字段：

| 字段 | 用途 |
|---|---|
| `source_state` | `active` / `retired` |
| `source_retired_at` | 标记时间 |
| `source_retired_note` | 标记原因 |

**执行前后数据完全一致：**

| 表 | 前 | 后 |
|---|---:|---:|
| assets | 37 | 37 |
| segments | 1676 | 1676 |
| segment_semantic_results | 786 | 786 |
| user_annotations | 3 | 3 |
| storyboard_source_refs | 14 | 14 |

只有 `assets.source_state` 变了 1 行。

---

## 为什么不能删

那 38 个镜头里有 **2 条模型读数**。那是真实付费调用产生的证据，
也是「这里曾经被推荐过」的审计记录。文件没了不代表这些判断没发生过。

删除会让报告里「为什么这批镜头没被评」变成一个无法回答的问题。

---

## 效果

| 指标 | 前 | 后 |
|---|---:|---:|
| 全部候选（含排除） | 99 | 99 |
| 默认可策划 | 99 | **61** |
| 因源废弃被排除 | 0 | **38** |

排除原因是 **`source_retired_by_user`**，与 `human_excluded` **严格区分**：

- `human_excluded` = 你看过并否掉了
- `source_retired_by_user` = 文件没了，**没人评判过这些镜头**

混在一起会污染评分统计——所以有测试专门断言两者不混淆（实测 0 个误标）。

---

## 可撤销

如果文件找回来了：

```
DELETE /api/projects/<id>/evaluation/assets/<asset_id>/retire
```

或页面上再点一次。38 个镜头立刻恢复可策划。

---

## 测试（`tests/test_tsb371_retire.py`，10 项）

| 用例 | 守护行为 |
|---|---|
| `test_retiring_deletes_nothing` | **行数一行不少**，且回报保留了什么 |
| `test_the_asset_is_marked_retired` | 状态、时间、备注都记下 |
| `test_the_shots_become_excluded_with_a_distinct_reason` | 原因独立，不混 `human_excluded` |
| `test_a_retired_shot_is_not_offered_by_default` | 默认列表里消失 |
| `test_an_active_shot_is_unaffected` | 其他素材不受影响 |
| `test_a_human_exclusion_is_not_overwritten_by_retirement` | **人工判断优先**，不被文件消失覆盖 |
| `test_a_human_preference_is_never_rewritten` | `user_rating` 一字不改 |
| `test_a_retirement_can_be_undone` | **可逆** |
| `test_retiring_an_unknown_asset_is_404` | 不存在即拒绝 |
| `test_another_projects_asset_cannot_be_retired` | 跨项目拒绝 |

---

## 事前备份

执行前已备份：`backend/data/travel_storyboard.pre-retire.db`（在本机，未提交）。
