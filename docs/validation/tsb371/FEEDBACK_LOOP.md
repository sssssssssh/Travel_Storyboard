# TSB-371-E：把人工判断接回策划（闭环）

## 问题：评价曾经是死胡同

在你问「就是单纯输出一个文档吗」之前，这个工具确实是。

我核对了代码：`evaluation_ratings` 表**只被报告读取**。策划流程
（`director_candidates._eligibility`）只看 `segment.user_rating`，那是另一套
字段，评价表里的判断对策划**没有任何影响**。

后果很具体：你可以把模型的每一句都判成错的，策划器照旧拿同样的读数去排片，
一个字都不会变。你花几十分钟做的事，只产出一个文档。

**这是真实缺口，不是设计取舍。**

---

## 修复：一个窄而可逆的规则

新增 `backend/app/services/evaluation_feedback.py`。

### 判定规则

| 该镜头的逐句判断 | 状态 | 对策划的影响 |
|---|---|---|
| 有任意一句被判**错** | `wrong` | 追加理由 `review_judged_wrong` |
| 有判断且全部判**对** | `trusted` | 无 |
| **只有**「不确定」 | `unreviewed` | 无 |
| **没有任何判断** | `unreviewed` | 无 |

### 三条刻意的克制

1. **不把沉默当否决。** 没评过就是没评过，不会降级。
2. **不把「不确定」当「错」。** 它表达的是评价者没把握，不是模型说错了。
3. **不把旧判断安到新读数上。** 读数被重新跑过（`is_current=False`）后，
   针对旧读数的判断自动失效，不迁移。

### 不改写任何东西

评价**从不**修改：
- 模型读数 `analysis_json`（仍是 `is_current=True`，一条字节都不变）
- 镜头 `user_rating` / `status` / 起止时间
- `UserAnnotation`
- Storyboard / 版本

状态是**每次读取时现算的**。所以把某句从「错」改成「对」，
镜头立刻恢复；删掉判断也立刻恢复。**没有不可逆的写入。**

---

## 你会在哪里看到它

导演候选接口 `GET /api/projects/{id}/director/candidates` 的每一行现在带：

```json
{
  "segment_id": "...",
  "review_state": "wrong",
  "review_reasons": ["review_judged_wrong"]
}
```

同时顶层 `limitations` 会出现
`some_shots_were_judged_wrong_by_a_human_reviewer` —— 让这件事可见，
而不是悄悄藏在某一行字段里（C6 不静默）。

---

## 测试（`tests/test_tsb371_feedback.py`，10 项）

| 用例 | 守护行为 |
|---|---|
| `test_a_shot_judged_wrong_is_downgraded_for_planning` | 判错即降级并给出理由 |
| `test_a_shot_judged_right_is_not_downgraded` | 判对不受影响 |
| `test_an_unreviewed_shot_is_left_alone` | **沉默不是否决** |
| `test_unsure_alone_does_not_downgrade` | **不确定不是错** |
| `test_reviewing_never_rewrites_the_model_reading` | 读数逐字节不变 |
| `test_reviewing_never_rewrites_the_segment_or_annotation` | 镜头与标注不变 |
| `test_reviewing_never_touches_the_storyboard` | 分镜板不变 |
| `test_changing_a_verdict_to_right_restores_the_shot` | **可逆** |
| `test_a_superseded_rating_stops_counting` | 旧判断不迁移到新读数 |
| `test_the_candidate_list_surfaces_the_review_state` | 策划接口真实暴露 |

### 过程中发现的一个真实缺陷

`CandidateOut`（Pydantic 响应模型）没有声明新字段，**FastAPI 会静默丢弃**。
第一次跑测试时 `review_state` 就是不存在，但接口本身返回 200 —— 没有任何报错。
这正是「接线要单独测」那条约定存在的原因。

---

## 仍然没做到的

`review_state=wrong` 目前**只是标记，不自动排除**该镜头。策划器会知道它被判错过，
但不会自己跳过。

我认为这应该由你来决定，而不是我替你定：判错一句，是否就该整镜头不进片单？
更合理的位置是**在剪辑草案页显示这个标记，由你决定用不用**。
这一轮的实现只保证「信息不会丢」，不替你下结论。

如果要自动排除，告诉我，我加一个开关，默认关闭。
