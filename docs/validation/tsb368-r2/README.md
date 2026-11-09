# TSB-368-R2 证据索引

基线 `15f4cefaf24884ca68592c79c8a2f83bcb9772fd`
发布记录：[../../RELEASE_TSB_368_R2.md](../../RELEASE_TSB_368_R2.md)

## 文档

| 文件 | 内容 |
|---|---|
| [BASELINE.md](BASELINE.md) | 开工基线、环境、预期 RED node id、生产/读取端字段对比 |
| [ROOT_CAUSE.md](ROOT_CAUSE.md) | 两项缺陷的根因，以及「为什么测试没发现」 |
| [PRODUCER_CONSUMER_MAPPING.md](PRODUCER_CONSUMER_MAPPING.md) | **生产者→消费者完整字段映射表** |
| [SYNTHETIC_E2E.md](SYNTHETIC_E2E.md) | 合成端到端报告（生产写入路径逐跳验证） |
| [REAL_VIDEO_UAT.md](REAL_VIDEO_UAT.md) | 真实素材预检 + **审批单** + BLOCKED 说明 |

## 状态速查

| 项 | 状态 |
|---|---|
| P0-01 原生视频事件映射 | ✅ PASS（RED 13 → GREEN 18 → 反向 13） |
| P1-02 input_digest 一致性 | ✅ PASS（RED 2 → GREEN 10 → 反向 2） |
| 生产写入路径端到端 | ✅ PASS（9 项） |
| 后端全量 | ✅ 1869 / 0 failed / 6 skipped |
| 前端四项 | ✅ 148 passed |
| CI run #124 | ✅ 7/7 |
| **真实模型验收** | 🔒 **BLOCKED（未获授权）** |

## 仅本地、不提交（`.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-p0.local.txt` | P0-01 修复前 13 项失败 |
| `RED-p1.local.txt` | P1-02 修复前 2 项失败 |
| `backend-full.local.txt` / `.xml` | 后端全量 1869 项 |

## 复现方式

```powershell
cd backend

# 本轮新增的三组测试
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368r2_video_event_contract.py `
    tests\test_tsb368r2_director_digest_contract.py -q

# 生产往返需要 FFmpeg（会现场生成合成源文件）
$env:TSB_REQUIRE_FFMPEG = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368r2_video_production_roundtrip.py -q

# TSB-368 全部专项
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368_candidates.py `
    tests\test_tsb368_query_cost.py tests\test_tsb368_director_api.py `
    tests\test_tsb368r1_contracts.py tests\test_tsb368r2_*.py -q
```

## 必须保持的区分

1. **原生视频观察 ≠ 已看完整素材库**（真实库该渠道仍为 **0 条**）
2. **模型自报 `confidence` ≠ 校准概率**（DTO 一律标 `uncalibrated`）
3. **`start_rel_sec` ≠ 源视频绝对时间**（相对 clip 起点，禁止当绝对时间用）
4. **`freshness_unverified` ≠ `current`**（源签名无法离线重算时如实降级）
5. **合成端到端 PASS ≠ 真实模型验收 PASS**
6. **代码合同通过 ≠ 可以发布上线**
