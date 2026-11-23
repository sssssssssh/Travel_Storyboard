# TSB-372-R2 工程与隔离验证

基线 `d699259352c42753f198a3514a0eed4e70516534`。基线的前端、四组后端以及两组强制 FFmpeg 作业均成功。 [基线 CI #162](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/45101094447)

## 本机与真实验收

| 检查 | Windows 实测 |
| --- | --- |
| 证据契约、导演 API、计划输入及 A/B 往返 | 167 通过。新增断言在旧代码上 13 失败，3 项已有保护通过 |
| 原生视频探测、协议及 FFmpeg 合成短片 | 79 通过。实际调用另见独立回执 |
| 盲评区间与缓存时效 | 34 通过。基线红测发现有效子区间已生成即 stale |
| 前端 typecheck、ESLint、test、build | 四项通过，19 个文件、192 个用例通过。ESLint 有既有警告 |
| Ruff `app tests` | 基线与本轮各 851 项。忽略行列后问题清单一致。文字验收工具单独检查通过 |
| 真实文字策划 | 1 次有效回执，61/61，11 个镜头、60 秒。真实结果通过 Validator |
| 真实原生探测 | 2 次预算内请求。文字 200，视频 400，服务端 `video=0` |
| 人工盲评 HTTP | POST 201，令牌 GET 200，无令牌 GET 401。来源保持隐藏，评分 0 条 |
| 真素材播放 | 5/5 区间准确，5/5 Range 206，5/5 ffprobe 时长符合容差 |
| 隔离数据 | 正式库中没有本轮回执或盲评会话。素材、镜头、故事板与版本表对照一致。抽查五段的源文件大小和修改时间一致 |

5 个样本修复前有 2 个子区间播放返回 409，修复后同一批次全部播放成功。实际可见内容、剪辑点和叙事价值由真人核对。

隔离归档中的反向接线验证分别移除预算记账和父区间落盘，两个对应回归均失败。恢复接线后两项通过。

## 后端全量与同提交 CI

使用仓库的 pytest 配置和强制 FFmpeg 门禁执行 Windows 全量。R1 已单独证明 Windows 主机 `test_package_in_a_deep_workspace_contains_its_frames` 会原生退出 `0xC0000417`。R2 的本机退出、完整矩阵与最终 SHA 以发布时随本文件交付的现场验证副本为准，现场副本保留 GitHub Actions 每项计数与 Run ID。

CI 验收要求同一提交的前端、Windows 与 Ubuntu 的四组后端，以及两组强制 FFmpeg 全部成功。 [查看 main 的 CI 运行](https://github.com/sssssssssh/Travel_Storyboard/actions?query=branch%3Amain)
