# M5.3-R3.2 验证索引（TSB-356～359）

冻结基线：main@8c05afa15e01d5fab185d63310ee3de62980e5fa。全部本地命令在独立测试 SQLite/工作区运行，不读用户真实旅行视频，不发送真实模型请求，不包含真实 Key。VERSION=2.1.2。

## 环境与命令

macOS 15.7.7 arm64，Python 3.12.14，FFmpeg/FFprobe 9.0.2，Node 26.4.0 / npm 11.17.0。本地 npm 执行 package.json 的同名脚本，CI 使用 pnpm；没有安装新依赖。后端始终从 backend/ 运行，显式 MODEL_MODE=off；需要模型的测试使用完整回环假配置与 httpx Mock，覆盖本机私有配置后才开启测试模式，明文公网例外关闭。

```sh
cd backend
TRAVELSB_MODEL_MODE=off TSB_REQUIRE_FFMPEG=1 .venv/bin/python -m pytest -q --junitxml=/tmp/r32-b-backend.xml
TRAVELSB_MODEL_MODE=off TSB_REQUIRE_FFMPEG=1 .venv/bin/python -m pytest -q tests/test_video_fallback_runner_e2e.py tests/test_fallback_result_contract.py tests/test_fallback_job_tracking.py
TRAVELSB_MODEL_MODE=off TSB_REQUIRE_FFMPEG=1 .venv/bin/python -m pytest -q tests/test_video_fallback_execution.py tests/test_video_fallback_runner_e2e.py tests/test_fallback_result_contract.py tests/test_fallback_job_tracking.py
cd ../frontend
npm run typecheck
npm run lint
npm run test
npm run build
```

## 可核验证据

| 文件 | 内容 |
|---|---|
| A_LOCAL.json / A_BACKEND.xml / A_FRONTEND.txt | A 全量后端 1306 collected、1262 passed、44 平台 skip；前端 96 passed，四项通过 |
| F_RED.txt / F_PERMISSION_RED.txt | F 状态序列先失败、显式授权开关先失败 |
| F_REVERSE_POLL.txt / F_REVERSE_REFRESH.txt | 恢复只看父状态、断开多帧缓存刷新分别失败 |
| B_RED.txt / B_RED.xml | 初始后端 58 collected、25 failed、33 passed、0 skipped、0 errors |
| B_EXTRA_RED.txt / B_MALFORMED_RED.txt / F_SCHEDULE_RED.txt | 补充缺帧、空动作、畸形/越界帧、调度失败隐私诊断先失败 |
| B_TARGET.txt / B_TARGET.xml | 三文件专项 65 passed、0 skipped：Runner 17 + 契约 26 + 原追踪升级 22 |
| B_FULL_FIRST_RED.* / B_COMPAT_TARGET.* | 首轮全量仅 V39c 兼容类别失败；保留安全 JobConflict 类别后四文件 84 passed、0 skipped |
| B_REVERSE_STORE/RUNNER/TRUTH/FORCE/PARENT/MEDIA.txt | 六项逆向移除产生实际 RED，已恢复 |
| B_FRONTEND.txt / B_TYPECHECK.txt / B_LINT.txt / B_BUILD.txt | 前端最终四项通过，98 passed；轮询文件 19 场景 |
| B_LOCAL.json / B_BACKEND.* | B 严格全量 1349 collected / 1305 passed / 44 Windows 平台 skipped / 0 failed / 0 errors，含逐项 skip 原因 |

RED 与逆向输出是预期失败证据，不是最终故障。参数化产生的场景数与任务书 F10/B10/S7 的 27 个编号不同，逐项映射见 ../../REGRESSION.md。定向 Vitest 未选中项不记为通过，未当作环境 skip。

合成媒体由真实 FFmpeg 生成 3 秒 H.264，再从选中镜头的人工裁剪区间提取 ≥2 帧。只替换 HTTP 传输和不可用能力事实；Provider、数据校验、真实 Runner、锁、任务收尾、_store 和公开读取 API 均照常执行。成功、部分成功、失败、排队取消、运行取消均有确定上限等待。成功行必须属于本项目、本次执行时间窗、所选镜头的当前区间；没有结果时即使 Job succeeded 也不报已入库。

无同意时 API 建立的 Job 在授权边界失败，并验证无抽帧/无请求；不会把 202 当成上传获准。原片 SHA/mtime/字节、人工评分/裁剪/备注、正式 verdict 历史保持不变；自有暂存清理与外部哨兵保留均有断言。所有输出已去除本机路径和 hostname，不提交媒体、数据库、凭据或 Payload。

## 平台 skip 与强制媒体清单

本机 44 项 skip 都是 Windows 平台事实（MAX_PATH/long-path/Windows 文件占用）；逐项 reason 在 B_LOCAL.json。不是 FFmpeg 缺失，也不是本轮 E2E 前提不足。两个新文件明确加入 media 清单，已有 test_fallback_job_tracking.py 继续在清单；没有添加新 skip 原因，没有放宽 conftest 守卫。Windows CI 真实运行这些用例、LongPathsEnabled=0；CI 的 Windows 不等于用户真机人工验收。

## 同 SHA CI 与门禁边界

A 的 c98b643：[CI 39089911587](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39089911587)，已读取 7 个 Job 均 success。它只证明 A，不代替最终提交。

最终 C 文档提交后读取 Actions run.head_sha，并逐项核验 4 个 Python 后端、2 个 Required FFmpeg、1 个前端全部 completed/success，核对 remote main 与本地 HEAD 相同。最终同 SHA 的完整 run URL、Job URL、实际日志计数保存在本目录 [FINAL_HEAD_CI.local.json](FINAL_HEAD_CI.local.json)，并在交付回复给出 SHA 和链接。此文件在最终提交之后产生并被精确忽略，避免“把 CI 报告再提交导致 SHA 又变化”的循环。GitHub Actions 对应 run 的日志与 JUnit artifacts 是远端证据；不得在尚未核验时宣称最终 7/7。

G5=blocked_by_endpoint；G6=not_evaluated；Windows 本机真人验收=not_done；8～10 段真实素材、200GB 性能未完成。本轮仅证明真实业务链在合成媒体 + Mock 模型下闭环，不能证明真实模型画面理解质量或原生视频优于抽帧。最终 CI 核验前仍待放行；核验后结论限于“代码级闭环通过，发布门禁未完成”。
