# v2.0.7 CI 证据

最终 HEAD 的 7 个作业运行待推送后核对。交付回复将给出精确完整 SHA 和 run URL。
媒体作业上传 `media-results.xml` 及 `media-platform.json`：包含 GitHub SHA、run ID、
平台、Python、真实 FFmpeg/FFprobe 版本；Windows 另含 LongPathsEnabled=0。
不使用上一轮或不同 HEAD 的 CI 冒充最终 HEAD。
