# TSB-368 证据索引

基线：`bddadd82a7d37fc681549fb3e6036511914985d3`
发布记录：[../../RELEASE_TSB_368.md](../../RELEASE_TSB_368.md)

## 文档

| 文件 | 内容 |
|---|---|
| [FIELD_MAPPING.md](FIELD_MAPPING.md) | **源表 → DTO 字段映射**，逐项标注已验证/推导/不可用 |
| [COVERAGE_AND_RANKING.md](COVERAGE_AND_RANKING.md) | 候选人资格、人工优先、覆盖口径、排序与多样性规则 |

## 截图（真实 Chrome，已脱敏）

| 文件 | 证明什么 |
|---|---|
| `raw/368-0-before-switch.png` | 改选前：A 的提案显示在面板上 |
| `raw/368-0-after-switch.png` | 改选后：旧提案消失，只剩「预览变更」，目标已变 B |
| `raw/368-b-page.png` | 导演候选页：三渠道独立徽标、覆盖率分子/分母、「未分析」明确显示 |

脱敏确认：只含项目内 UUID、合成文件名 `uat_clip_*.mp4`。
**不含**本地绝对路径、令牌、模型原始响应、私人身份。

## 仅本地、不提交（`.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-368-0.local.txt` | 368-0 修复前 3 failed |
| `GREEN-368-0.local.txt` | 368-0 修复后 7 passed |
| `REVERSE-368-0.local.txt` | 还原缺陷后再次 3 failed |
| `frontend-all.local.txt` | 前端全量 |
| `backend-full.local.txt` / `.xml` | 后端全量 |

## 复现方式

```powershell
# 后端 368 测试（不需要 FFmpeg）
cd backend
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368_candidates.py `
    tests\test_tsb368_query_cost.py tests\test_tsb368_director_api.py -q

# 前端四项
cd frontend
pnpm run typecheck; pnpm run lint; pnpm exec vitest run; pnpm run build

# 真实 Chrome（需要隔离 UAT 数据 + 服务；驱动脚本放在仓库外，见 RELEASE 说明）
node <UAT_DIR>\uat368-0.js   # 368-0 UI
node <UAT_DIR>\uat368-b.js   # 368-B 候选页
```

## 关键结论速查

| 结论 | 证据 |
|---|---|
| UAT-07 旧提案不再错标目标 | RED 3 failed → GREEN 7 → 反向验证 3 failed；Chrome 9/9 |
| 无 AI 结果的镜头仍是候选 | `test_a_shot_without_any_analysis_is_eligible_not_analyzed` |
| 三渠道互不替代 | `test_static_only_never_claims_video_understanding` 等 |
| 有过期记录 ≠ 已覆盖 | 反向验证：改坏后 `test_a_superseded_result_is_stale_not_current` 精确失败 |
| 人工优先级高于 AI | `test_human_favorite_survives_poor_technical_quality` |
| 无 N+1 | SQL 数恒定 7 条（4/40/500/1000 镜头） |
| 只读 | Chrome 实测零写请求；后端前后计数一致 |
| 不编造地名 | `test_brief_text_is_not_treated_as_video_content` |

## 必须保持的四条语义区分

1. **静帧语义结果 ≠ 看过视频**
2. **多帧运动分析 ≠ 原生视频理解**
3. **原生视频观察 ≠ 已看完整素材库**
4. **合成素材 + Chrome + CI ≠ 真实旅行素材 / 真实模型能力（G5/G6）**
