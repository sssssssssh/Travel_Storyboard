# TSB-368-R2 开工基线

## 1. Git 状态

```
$ git status --short            →  (空)
$ git branch --show-current     →  main
$ git rev-parse HEAD            →  15f4cefaf24884ca68592c79c8a2f83bcb9772fd
$ git rev-parse @{upstream}     →  15f4cefaf24884ca68592c79c8a2f83bcb9772fd
```

| 项 | 值 |
|---|---|
| HEAD | `15f4cef`（TSB-368-R1 文档收口） |
| upstream | `origin/main`，与本地一致 |
| 工作区 | 干净 |

**HEAD 与任务书参考基线一致**，未超越 `15f4cef`，下文两个缺陷均已重新确认真实存在。

最近提交：

```
15f4cef docs(TSB-368-R1): 回填 CI run #122 的 7/7 结果与关闭标准对照
9b7c874 fix(TSB-368-R1): 收口人工排除优先级、策划资格与 input_digest…
4520636 docs(TSB-368): 回填 CI run #120 的 7/7 结果与遗留项
2bd0529 feat(TSB-368-A/B): 只读导演候选聚合层、查询 API 与策划输入合同
68477a4 fix(TSB-368-0): 切换镜头或操作类型时隔离旧提案与目标显示（UAT-07）
```

## 2. 环境

| 项 | 值 |
|---|---|
| OS | Windows 11 |
| Python | 3.13.7（`backend/.venv`） |
| pytest | 9.1.1 |
| FFmpeg | 9.0.2-essentials（项目自带，含 libx264/libx265） |
| Node | 18.19.0（CI 用 20） |
| LongPathsEnabled | 0 |

## 3. 受限范围

| 项 | 状态 |
|---|---|
| 真实素材 | 可访问（5/6 源文件） |
| **真实模型调用** | **未授权，不得执行** |
| 外部上传 | 禁止 |
| 真实截图入仓 | 禁止 |

## 4. 预期 RED node id

| 缺陷 | 预期失败 node id（修复前） |
|---|---|
| P0-01 事件字段不匹配 | `test_tsb368r2_video_event_contract.py::test_production_event_shape_keeps_times_type_and_description` |
| P0-01 相对/绝对混淆 | `…::test_relative_times_are_never_used_as_absolute` |
| P0-01 双事件 | `…::test_two_valid_production_events_are_both_accepted` |
| P0-01 原因可见 | `…::test_malformed_events_are_dropped_with_a_reason[*]`（8 组） |
| P0-01 digest | `…::test_changing_a_video_event_description_changes_the_digest` |
| P1-02 标签不在 digest | `test_tsb368r2_director_digest_contract.py::test_changing_a_label_outside_the_candidates_changes_the_digest` |
| P1-02 字段覆盖 | `…::test_digest_covers_every_field_the_context_returns` |

## 5. 生产端 / 读取端字段对比（开工实测）

| 项 | 生产 `TemporalEvent.to_dict()` | 读取端 `_video_channel()` |
|---|---|---|
| 起始时间 | `start_abs_sec` | `start_sec` / `start` ❌ |
| 结束时间 | `end_abs_sec` | `end_sec` / `end` ❌ |
| 类型 | `event_type` | `type` ❌ |
| 描述 | `description` | `label` ❌ |
| 相对时间 | `start_rel_sec` / `end_rel_sec` | （未使用） |

**结论**：生产事件的时间与文案字段**全部对不上**，所有真实事件会被丢弃。
