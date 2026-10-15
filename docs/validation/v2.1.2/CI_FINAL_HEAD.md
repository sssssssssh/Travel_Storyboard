# v2.1.2 同 HEAD CI 核对

基线：main@f68f95446d8ada734843218fc974b321a905e1b4。只在 main 开发，不 force、不另建分支。

| 批次 | SHA / Run | 结果 |
|---|---|---|
| A / TSB-320 | 30c3635ed0041cfd32a6938e94860343d8bac915 / [38761580532](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38761580532) | 7/7；A_CI.json |
| B 首次 | 7d0e07962499851f71ca0a39d5b0132b5edcd5f5 / [38765334673](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38765334673) | Windows 三作业失败；默认 cp1252 解码中文 README，未冒充通过 |
| B UTF-8 补救 | 44acadc6e7cae8b46d73f17349822740da182b1c / [38767070281](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38767070281) | 7/7；B_CI.json / B_WINDOWS_ENCODING.md |
| C / TSB-323、324 | 包含本文件的最终 main HEAD；提交后运行 | 以最终交付回复的 SHA/Run URL/七个作业核对为准，生成本地 CI_FINAL_HEAD.local.json |

本文在最终 C 提交内，不能预先声称未来 CI 通过，也不能在自身内容写入自身 Git SHA。按任务书 §7.3，最终 CI 完成后在交付回复给出同 HEAD 的确切 SHA、Run URL 和逐作业数字，不为“证据提交”再次改变已验证 SHA。本地详细核验 JSON 被精确忽略，可直接查看，不含凭据。

最终 CI 必须逐项核对（不能用 B 的成功替代）：

| 作业 | 本轮相同后端代码的预期 collected / passed / failed / errors / skipped |
|---|---|
| Ubuntu Python 3.11 | 807 / 763 / 0 / 0 / 44 |
| Ubuntu Python 3.13 | 807 / 763 / 0 / 0 / 44 |
| Windows Python 3.11 | 807 / 807 / 0 / 0 / 0 |
| Windows Python 3.13 | 807 / 807 / 0 / 0 / 0 |
| Ubuntu mandatory media | 744 / 700 / 0 / 0 / 44 |
| Windows mandatory media | 744 / 744 / 0 / 0 / 0，LongPathsEnabled=0 |
| Frontend | 四项检查；48 tests passed（最终实际日志核对） |

backend CI 原样忽略两文件共 21 项，所以 807 与本地全量 828 不混淆。mandatory media 是显式 744 项清单，新增媒体矩阵/版本测试已登记。Ubuntu/Mac 的 44 个 skip 均为已登记的 Windows 平台事实，逐项见 C_LOCAL.json；Windows 无 skip。未放宽工具缺失或 skip 守卫。

C 本地普通和严格全量各 828 collected / 784 passed / 44 skipped / 0 failed / 0 errors；前端 typecheck/lint/test/build 全通过，48 passed。原始脱敏 JUnit：backend-junit.xml、C-backend-strict-junit.xml；环境/逐项 skips：C_LOCAL.json。真人工、素材类型、播放/HDR 门禁不由这些数字替代。
