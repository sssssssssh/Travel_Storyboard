# TSB-368-R1 证据索引

基线 `4520636999a45425f5b0c06f5924adda7d99554b`
发布记录：[../../RELEASE_TSB_368_R1.md](../../RELEASE_TSB_368_R1.md)

## 文档

| 文件 | 内容 |
|---|---|
| [REAL_LIBRARY_UAT.md](REAL_LIBRARY_UAT.md) | 真实素材库只读验收完整记录 |
| [CONTEXT_CONSUMPTION_CONTRACT.md](CONTEXT_CONSUMPTION_CONTRACT.md) | **TSB-369 输入合同**：如何取得真实内容描述 |

## 真实素材 UAT 环境

| 项 | 值 |
|---|---|
| 项目 | `M5-real-acceptance`（用户授权的本地真实旅行素材） |
| 素材 | 6 个 DJI 4K 航拍视频，合计约 333 秒 |
| 镜头 | 99 个 |
| 服务 | 本地 FastAPI，`127.0.0.1`，应用自身 DATA_DIR |
| 浏览器 | Google Chrome 156.0.8078.80（Playwright 驱动） |
| 外部调用 | **无**（未上传、未调用模型、未启动分析） |

**隐私**：真实素材截图**未提交**到仓库。截图含真实文件名与画面元数据，
按任务书 §5.3 要求保留在本地。

## 仅本地、不提交（`.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED.local.txt` | 三个缺陷修复前的失败输出 |
| `backend-full.local.txt` / `.xml` | 后端全量 1832 项 |
| 真实库 UAT 结果 JSON / 截图 | 含真实素材标识，保留在本地临时目录 |

## 复现方式

```powershell
cd backend

# 合同测试（不依赖真实素材）
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368r1_contracts.py -q

# TSB-368 全部专项
.\.venv\Scripts\python.exe -m pytest tests\test_tsb368_candidates.py `
    tests\test_tsb368_query_cost.py tests\test_tsb368_director_api.py `
    tests\test_tsb368r1_contracts.py -q

# 真实库 UAT（需要本地真实项目 + 运行中的服务）
# 见 RELEASE §5，脚本放在仓库外的临时目录
```

## 关键结论速查

| 结论 | 证据 |
|---|---|
| 人工排除不再被 pending 注解覆盖 | RED 5 失败 → GREEN → 反向验证 5 失败 |
| needs_review 不进自动策划 | RED 4 失败 → GREEN → 反向验证 4 失败 |
| digest 对备注/内容变化敏感 | 含"ID 不变但内容变"的专项测试，移除指纹即失败 |
| 真实库覆盖率与 DB 一致 | 99/99，三资格和 99，8/4/0 与 DB 完全吻合 |
| 真实模型输出质量 | 多帧描述具体且主动声明不确定性；`content_mismatch` 0 条 |
| **真实数据格式兼容缺陷已修** | 扁平 14 条 / 嵌套 772 条，修复后静帧内容全部可见 |
| 只读 | 前后 DB 计数、版本数、Job 数完全一致；页面零写请求 |
| 无泄漏 | 路径/令牌/request_usage 命中 0 |

## 必须保持的区分

1. **静帧语义结果 ≠ 看过视频**
2. **多帧运动分析 ≠ 原生视频理解**
3. **原生视频观察 ≠ 已看完整素材库**（本库该渠道为 **0 条**）
4. **模型线索 ≠ 人工确认事实**（本库人工标注为 **0 条**）
5. **合成数据测试通过 ≠ 真实素材验收通过** —— 本轮真实 UAT 之所以有价值，
   正因为它发现了合成数据永远发现不了的问题（扁平 `analysis_json`）
