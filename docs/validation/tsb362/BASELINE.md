# TSB-362 — 阶段 0 基线核对（BASELINE）

| 字段 | 实际值 |
|---|---|
| 核对时间 | 2026-11-03 15:35（Asia/Shanghai） |
| 任务书冻结基线 | `main@0da77a19725a6af5963672a84d6f6464d4be7ac8` |
| `git rev-parse HEAD` | `0da77a19725a6af5963672a84d6f6464d4be7ac8` |
| `git rev-parse main` | `0da77a19725a6af5963672a84d6f6464d4be7ac8` |
| `git branch --show-current` | `main` |
| 工作树（开始时） | 干净（`git status --porcelain` 无输出） |
| HEAD 提交标题 | `docs(TSB-361): 回填终态 SHA 1235058 的 CI 7/7 结果` |
| 根目录 `VERSION` | `2.1.2` |

**V01 结论：基线符合冻结条件。** 分支、HEAD、工作树三者与任务书一致，未执行任何
reset / checkout / 覆盖操作。

## 现存验证工具（`git ls-files` 实测，非虚构）

| 路径 | 与本任务的关系 |
|---|---|
| `backend/tools/probe_video_capability.py` | **G5 主探测器**。默认 dry-run；`--execute` 需配 `--i-authorize-real-upload` |
| `backend/tools/reverse_verify_native_video.py` | 原生视频接线反向验证 |
| `backend/tools/reverse_verify_video_clip.py` | 派生短片反向验证 |
| `backend/tools/evaluate_video_ab.py` | G6 A/B 评估（本轮不执行） |
| `backend/tools/verify_secure_model.py` | 端点安全（HTTPS/insecure 例外）核验 |
| `backend/tools/e2e_drive.py` | 正式 API 端到端驱动（阶段 C 可能用） |

## 能力门禁位置

| 门禁 | 文件 | 作用 |
|---|---|---|
| 四态能力枚举 | `backend/app/services/vision/video_capabilities.py:41-58` | `verified_native_video` / `video_accepted_sampling_unknown` / `unsupported` / `unverified`；`SENDABLE_STATES` 只含 `verified_native_video` |
| 协议注册表 | `video_capabilities.py:267-275` | 仅注册 `video_url_data_uri`（`video/mp4`，inline data URI，`fetches_remote=False`） |
| 出站授权边界 | `backend/app/services/vision/authorization.py:90-119` | 需 `MODEL_MODE=on` + 本次调用 `upload_acknowledged` + 非空 project_id + base_url/key |
| 视频发送门禁 | `backend/app/services/vision/providers/native_video.py:448-466` | `capability_gate()`：非 sendable 即拒绝 |
| Provider 三重前置 | `providers/native_video.py:201-241` | 先授权 → 再协议 → 再内容嗅探（ISO-BMFF `ftyp`）与大小 |
| 端点安全 | `providers/openai_compatible.py:75-97` | `http://` 非回环必须 `allow_insecure=True`，否则拒绝 |

**任务书路径订正**：任务书 §三 写的是 `backend/app/services/vision/native_video.py`，
该路径**不存在**；实际文件为 `backend/app/services/vision/providers/native_video.py`。
其余路径（`config.py`、`video_capabilities.py`、两份发布记录、`REGRESSION.md`）均存在。

## 环境约束

- Python **3.13.7**（`backend/.venv`）。
- `.env` **只从 `backend/` 作为工作目录运行时被读取**（`SettingsConfigDict(env_file=".env")`
  相对 CWD）。本轮所有命令都先 `cd backend`。
- `backend/.env`：**存在**，且已被根 `.gitignore` 第 19 行 `.env` 覆盖（`git check-ignore` 退出码 0）。
- 仓库内不存在第二个 `.env*` 文件（除 `.env.example`）。
- **venv 内没有任何自动化测试会访问真实网络**；本轮全部为只读检查与 dry-run。

## 证据目录的隐私缺口（本轮发现并已修补）

任务书 §4.2 假设 `*.local.json` 已被 `.gitignore` 覆盖。**实测不成立**：

```
git check-ignore -v -- docs/validation/tsb362/probe-dry-run.local.json
→ 退出码 1（未被忽略）
```

根 `.gitignore` 只按**精确路径**逐条列举（如 `docs/validation/m5.3-r3.2/FINAL_HEAD_CI.local.json`），
没有任何 `*.local.json` 通配规则。也就是说，若照任务书把报告写进
`docs/validation/tsb362/`，`git add -A` 会把它**直接暂存**。

处置：本轮只新增一个不含任何敏感内容的防护文件
`docs/validation/tsb362/.gitignore`（`*.local.json`、`*.local.*`），加装后复核：

```
git check-ignore -v -- docs/validation/tsb362/probe-dry-run.local.json
→ docs/validation/tsb362/.gitignore:10:*.local.*   （已忽略）
git check-ignore -v -- docs/validation/tsb362/probe-real.local.json
→ docs/validation/tsb362/.gitignore:10:*.local.*   （已忽略）
```

未改动根 `.gitignore`，未触碰任何产品代码。
