# v2.1.2 发布门禁

| 门禁 | 当前判定 | 证据 / 限制 |
|---|---|---|
| G1 CI | C 批同 HEAD 推送后核对；最终交付回复及本地 CI_FINAL_HEAD.local.json 为权威 | A/B 各自 7/7 已验证，不能替代 C；CI_FINAL_HEAD.md / A_CI.json / B_CI.json |
| G2 状态 | 代码/自动化及真实 UI 故障注入通过 | CLEANUP_STATUS.md、UI_CLEANUP_RETRY.json、故障/补救截图；不改终态/CAS，不重跑模型 |
| G3 版本/兼容 | 代码与隔离旧库启动通过 | VERSION=2.1.2、版本 5 回归+前端页脚、SOURCE_PRESERVATION.json 旧 269 行；算法/Prompt 不变 |
| G4 安全 | 本轮已验证的边界通过 | 默认/结束 off，真实 HTTPS、证书校验；UI_SECURITY.json；原片只读/未知目录不接管/任务 scoped 清理；没有全局删除或终止 |
| G5 格式 | L1 编解码路径通过；HDR 色彩验收未完成；L2 不足 | FORMAT_MATRIX.json；PQ/HLG 元数据/编码可验证，不声明 HDR 色彩准确 |
| G6 真人工 UI | **未完成** | MAC_UI_E2E.md：实际界面全链路生成/下载及解码已验证；浏览器安全策略阻止媒体导航，实际播放未完成；真人点选/审阅待反馈，不能以自动化点选替代 |
| G7 真实素材 | **未完成** | 只有 6 个不同的同类型 HEVC MP4；本轮 UI 用其中 2 个/17 镜头，用户暂无额外素材。缺 2～4 个独立文件和补充类型 |
| G8 范围/证据 | 本轮范围声明与缺口已记录 | RELEASE_V2_1_2.md、MAC_UI_E2E.md、SOURCE_PRESERVATION.json、CI_FINAL_HEAD.md |

放行结论：待 C 批同 SHA CI 通过后，可判定 **代码级整改通过，正式发布门禁未完成**。G6/G7、HDR 人工色彩验收不可由 CI、合成片、ffprobe 或 8 个镜头代替。不宣称正式发布、M6 完成、Windows 真机旅行素材验收或 60+ 段/200GB 性能达标。

用户补充事项：2～4 个不同真实视频，优先 MOV/H.264、竖屏、无音轨、HDR；一次真人实际审阅/点选、真实成品播放及音画观感记录。当前没有额外素材不是授权去网上取版权不明的视频，也不能复制同一原片充数。
