# TSB-368-R2 根因分析

## P0-01：原生视频时序事件全部丢失

### 现象

真实原生视频模型产生的事件，在导演候选里**一条也看不到**。
`summary` 可见，`events` 恒为空。

### 根因

生产写入端与读取端的**字段名完全不同**：

| 语义 | 生产端写出（`TemporalEvent.to_dict()`） | 读取端查找（`_video_channel()`） |
|---|---|---|
| 事件起点 | `start_abs_sec` | `start_sec` → `start` |
| 事件终点 | `end_abs_sec` | `end_sec` → `end` |
| 事件类型 | `event_type` | `type` |
| 事件描述 | `description` | `label` |

读取端对每个事件执行：

```python
start = ev.get("start_sec", ev.get("start"))   # → None
end   = ev.get("end_sec", ev.get("end"))       # → None
if not isinstance(start, (int, float)) ...:
    dropped += 1
    continue                                    # 每个真实事件都走到这里
```

**每个真实事件都在第一个 `isinstance` 检查处被丢弃。**

### 为什么测试没发现

`test_tsb368_candidates.py::_video()` 手工构造的事件正是**读取端想象的样子**：

```python
{"start_sec": ..., "end_sec": ..., "label": ..., "confidence": ...}
```

测试数据编码了读取方的假设，于是「读取方能读自己造的格式」被误当成「读取正确」。

这与 TSB-368-R1 的 `analysis_json` 缺陷是**同一类错误**，只是发生在不同字段上。

### 隐藏的第二层风险

生产端同时写出 `start_rel_sec` / `end_rel_sec`（**相对 clip 起点**）。
若修复时「顺手」用它们兜底，会把每个事件的时间平移一个 clip 偏移量——
在 clip 从 0 开始时完全看不出来，在 clip 从 10 秒开始时错 10 秒。

因此修复**明确拒绝**把相对时间当绝对时间：绝对时间缺失就丢弃并记录原因。

---

## P1-02：`input_digest` 漏掉 `important_human_labels`

### 现象

`max_candidates` 小于库规模时，一个**不在候选摘要内**的镜头的人工备注改变，
`/director/context` 的返回内容变了，但 `input_digest` 不变。

### 根因

```python
chosen = usable[:max_candidates]          # 只到 max_candidates
...
"important_human_labels": [...]           # 来自 usable 全集，而非 chosen
"input_digest": input_digest(pid, chosen, brief)   # 只算 chosen
```

返回给下游的内容包含 `important_human_labels`，但 digest 只覆盖了 `chosen`。

### 修复思路

**不再枚举"应该进 digest 的字段"**，而是对**构造完成的整个返回文档**做规范化
SHA-256（排除 digest 自身，并附加 `_digest_version`）。

理由：手工枚举正是漏掉 `important_human_labels` 的原因。改为「digest 返回的文档」
之后，将来给 context 加字段会自动进入 digest 范围，不需要有人记得同步修改。

---

## 附带发现：文档与真实 API 不符

TSB-368-R1 的 `CONTEXT_CONSUMPTION_CONTRACT.md` 写作 `analysis.summary`，
真实响应是顶层 `summary`。已在 R2 修正文档，**未改动 API**。

另外，原生端点的行 id 字段名是 `id`，而导演 DTO 里叫 `result_id`——
两者对应关系已在映射表中写明。
