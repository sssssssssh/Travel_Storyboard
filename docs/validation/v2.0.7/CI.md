# v2.0.7 CI 证据

运行代码 SHA：`a28820da537e4707a2d0aea701ddc5385db3013b`。
验证过的运行：https://github.com/sssssssssh/Travel_Storyboard/actions/runs/38583888355
**7/7 成功**：Ubuntu/Windows × Python 3.11/3.13、两套强制媒体、前端。

| 作业 | 结果 |
|---|---|
| 四个 Backend 矩阵 | 全部 success，JUnit artifacts 保存实际计数 |
| Required FFmpeg Windows | 442 passed / 0 failed / 0 skipped；FFmpeg/FFprobe 9.0.2，LongPathsEnabled=0 |
| Required FFmpeg Ubuntu | 398 passed / 0 failed / 44 Windows 专属 skipped |
| Frontend | typecheck、lint、20 tests、build 全部 success |

索引见 ci-code-index.json；逐用例媒体证据见 windows-media.txt / ubuntu-media.txt。
媒体作业另上传 media-results.xml / media-platform.json，包含精确 SHA、run ID、
平台、Python、工具版本和 Windows 策略；四个 Backend 均上传 JUnit。

首轮 b71dbca 的运行 38583030723：Windows 媒体 437 passed / 1 failed，旧测试查找
固定粗剪名，其他 6 作业成功。更新为精确提交引用后，并加强时长/HTTP Range 断言，
上述 a28820d 全部转绿。失败证据见 windows-first-run.txt，不删除或弱化用例。

此记录在随后文档提交中保存，因此记录的代码 SHA 与文档 HEAD 区分。
文档提交不改变 runtime 或 tests，且同样触发 CI。最终文档 HEAD 自身的精确 run URL
在交付回复给出；该 run 的 media-platform.json 包含对应最终完整 SHA。
不得用此处的 a28820d 冒充最终 HEAD。TSB-277 和 200GB 性能仍未执行。
