# TSB-330 A/B 评价结果

- 状态: `not_annotated`
- 是否已评价: **False**
- 镜头数: 0，来源视频数: 0
- arm C 可用数: 0
- 能力状态: `unsupported`

## 各臂指标

| 臂 | 已评价 | event_recall | event_precision | IoU≥0.5 比例 | 备注 |
| --- | --- | --- | --- | --- | --- |
| A_image_only | False | None | None | None | not_annotated |
| B_multiframe | False | None | None | None | not_annotated |
| C_video_native | False | None | None | None | not_annotated |

召回增益（C 相对 A）: None 个百分点

## 采纳判定

- 已判定: False
- 采纳视频为首选: False

## 诚实声明

- 没有人工标注时 per-arm 指标为 null，只报告覆盖率，不填造准确率
- arm C 不可用时状态为 blocked_by_endpoint，其数值保持 null
- Mock/合成素材通过不等于真实视频质量验收通过

> arms A/B are billed per token; arm C sends clip_seconds and clip_bytes as well. Token counts are NOT comparable across these shapes and no unit-cost conclusion is drawn.
