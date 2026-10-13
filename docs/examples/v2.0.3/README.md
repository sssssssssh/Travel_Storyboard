# v2.0.3 JSON 字段示例

这些是说明用的虚拟 ID / 路径和统计，不是用户素材或真实运行结果。导入时请换成当前项目分析包里的真实 project_id、asset_id、segment_id，时间必须在当前有效裁剪内；不可直接导入示例 ID。

- review_requests.json：合法 0.6 秒请求格式。
- import_result.json：部分成功、错误定位及重复反馈格式（对应一个三行导入批次）。
- manifest_selection.json：selection 子对象及依次排除统计口径，需与实际 ZIP/JSON/帧核对。
- review_disposition.json：POST /api/projects/{project_id}/review-requests/{request_id}/disposition 的显式替代操作。
- review_request_lineage.json：历史与新请求的关联字段；GET 列表还附带当前候选、错误和计划引用上下文。

RT-10 的实际 FFprobe 输出时长、输出字节数与生成原片 SHA 见 validation/v2.0.3/local-automated.json。Windows 实机记录另列为未执行。
