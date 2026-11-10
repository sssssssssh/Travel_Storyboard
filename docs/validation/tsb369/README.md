# TSB-369 证据索引

基线 `a6a79076a26a46e698abf182fec8444d2b17fff4`
发布记录：[../../RELEASE_TSB_369.md](../../RELEASE_TSB_369.md)

## 文档

| 文件 | 内容 |
|---|---|
| [PLAN_CONTRACT.md](PLAN_CONTRACT.md) | 方案 schema、证据合同、22 类校验错误码 |
| [REAL_LIBRARY_UAT.md](REAL_LIBRARY_UAT.md) | 真实库只读实测与 Chrome UAT 证据 |
| `uat-browser.json` | 浏览器 UAT 原始结果（不提交，含真实项目 ID） |
| `backend-full.local.txt` | 后端全量输出（不提交） |

## 状态速查

| 项 | 状态 |
|---|---|
| 证据输入聚合 | PASS（17 项） |
| 草案 schema + 校验器 | PASS（32 项） |
| 只读 Preview API | PASS（24 项） |
| 前端只读草案页 | PASS（14 项） |
| 反向验证（3 项关键防线） | PASS |
| 后端全量 | 1942 / 0 failed / 6 skipped |
| 前端四项 | 162 passed |
| 真实库 Chrome UAT | **12/12 PASS** |
| **真实模型策划** | **BLOCKED（未授权）** |

## 复现

```powershell
cd backend
.\\.venv\\Scripts\\python.exe -m pytest tests\\test_tsb369_input.py tests\\test_tsb369_plan.py tests\\test_tsb369_api.py -q

cd ..\\frontend
pnpm exec vitest run src/pages/DirectorPlans.test.tsx
```

## 必须保持的区分

1. **离线规则 ≠ AI 看过素材**（UI 与导出文档均标注 `offline_rules`）
2. **结构建议 ≠ 故事结论**（`narrative_role` 是剪辑位置，不是叙事事实）
3. **用户主题 ≠ 素材内容**（主题永不进入草案文本）
4. **`complete` ≠ 完整覆盖**（只表示结构完整）
5. **草案 ≠ 正式 Storyboard**（不写库、无 apply 按钮）
6. **合成/只读验证通过 ≠ 真实模型验收通过**
