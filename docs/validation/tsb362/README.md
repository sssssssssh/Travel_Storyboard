# TSB-362 证据目录（真实视频模型能力探测与端到端验证）

基线：`main@0da77a19725a6af5963672a84d6f6464d4be7ac8`　当前阶段：**A、B 已完成；C 未开始（前置条件未满足）**

## 可提交 Git 的脱敏摘要

| 文件 | 内容 |
|---|---|
| [BASELINE.md](BASELINE.md) | 阶段 0：SHA、分支、工具清单、门禁位置、路径订正、隐私缺口修补 |
| [dry-run-summary.md](dry-run-summary.md) | 阶段 1：配置只读诊断、dry-run 退出码、请求预算、风险 R1–R5、授权前检查单 |
| [r3-frame-control-fix.md](r3-frame-control-fix.md) | R3 缺陷：单帧负对照未接线；RED → GREEN → 反向验证全记录 |
| [full-suite-result.md](full-suite-result.md) | 严格全量 `1385 / 1 failed / 5 skipped`；唯一失败为基线既有偶发及其归因证据 |
| [native-video-probe-summary.md](native-video-probe-summary.md) | 阶段 B：真实端点探测（7 次请求），400 确因为部署 `--limit-mm-per-prompt video=0` |
| [stageB-rerun.md](stageB-rerun.md) | 阶段 B 重跑（新部署）：逐项排除客户端因素，视频上限仍为 0；含服务端配置指引 |
| [stageB-rerun-3.md](stageB-rerun-3.md) | 视频功能开启后：首个视频请求击垮服务（503），3 分钟未自愈 |
| [e2e-multiframe.md](e2e-multiframe.md) | **阶段 C**：真实素材正式链路 E2E（多帧模式），7/7 入库、原片 SHA 不变、正式库未污染 |

## 仅本地、禁止提交（已被本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `probe-dry-run.local.json` | dry-run 脱敏报告（`sendable=false`、`state=unverified`） |
| `probe-dry-run.stdout.local.txt` | 控制台原文；**含私有端点主机名**，不得进入 Git |
| `probe-real.local.json` | 阶段 B 真实探测报告（`state=unsupported`、`sendable=false`） |
| `probe-real.stdout.local.txt` | 阶段 B 控制台原文；**含私有端点主机名** |
| `diag-400.local.txt` | 400 响应体原文（已脱敏），含部署参数提示 |
| `RED-r3-*.local.txt` / `GREEN-r3-*.local.txt` / `REVERSE-r3-*.local.txt` | R3 的 RED / GREEN / 反向验证运行日志 |
| `backend-full.local.xml` / `backend-full.local.txt` | 严格全量 junit 与运行日志 |

## 门禁状态（阶段 B 收口时）

| 门禁 | 状态 |
|---|---|
| G5 端点健康 | ✅ 健康（2026-11-03 实测 200；此前的 503 未复现） |
| G5 视频被接收 | ❌ 400：本部署 `--limit-mm-per-prompt video=0`（**服务端配置**） |
| G5 时序能力 | ❌ 未验证（模型从未看到任何视频帧） |
| G5 总体 | `unsupported`（`classify_probe_outcome` 实测输出） |
| G6 原生视频 vs 多帧 A/B | `not_evaluated` |
| 正式业务链路 E2E（阶段 C） | `not_started`（前置条件未满足） |
| `TRAVELSB_MODEL_SUPPORTS_NATIVE_VIDEO` | `false`（**未改动**） |

**解除阻塞的唯一办法是服务端**：把部署参数改为 `--limit-mm-per-prompt video=1`
（或升级不支持该参数的 vLLM 版本）。客户端改代码无法、也不应该绕过。

## 阶段顺序（任务书 §十一）

- **A**（已完成）：仓库核对、配置只读检查、dry-run、风险与预算。
- **B**（需授权）：仅发送工具自生成的合成 MP4；出现 5xx/503 立即停止。
- **C**（需再次授权）：用户指定的单段旅行短片 + 正式 API→Runner→DB→UI 链路。
- **D**：收口发布记录与门禁结论。

> 阶段 A **不构成**任何能力声明：端点可用 ≠ 视频被接收 ≠ 模型看懂时序 ≠ 业务已入库。
