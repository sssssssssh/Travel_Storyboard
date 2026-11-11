# TSB-371 证据索引

基线 `cca8e973f054fac5924483efa0cff54445c127d1`
发布记录：[../../RELEASE_TSB_371.md](../../RELEASE_TSB_371.md)

| 文件 | 内容 |
|---|---|
| [LEDGER_RECONCILIATION.md](LEDGER_RECONCILIATION.md) | 三份文档矛盾核对、原始账本、无法闭合的缺口 |

## 状态

| 项 | 状态 |
|---|---|
| CODE_PASS | PASS |
| FOOTAGE_REVIEW_READY | PASS |
| FOOTAGE_REVIEW | **MANUAL_REVIEW_PENDING**（真人未打分） |
| AB | **AB_BLOCKED_NO_REAL_MODEL_PLAN** |
| G3 | BLOCKED_UNAVAILABLE |
| RELEASE | BLOCKED |

## 复现

```powershell
cd backend
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_tsb371_*.py -q

cd ..\\frontend
pnpm exec vitest run src/pages/Evaluation.test.tsx
```

## 必须保持的区分

1. **可评分 ≠ 已评分**（UI 就绪不代表有真人判断）
2. **模型自报 confidence ≠ 准确率**
3. **1 位评价者 ≠ 人群胜率**
4. **N/A ≠ 0% ≠ 100%**
5. **源不可用 ≠ 模型描述错误**
6. **离线与模型方案不同 ≠ 模型更好**
