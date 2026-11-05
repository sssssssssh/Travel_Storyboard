# TSB-366-R1 证据目录

基线：`main@8f2d4ebbb74840304b668746030515d5379f15b4`

## 可提交的脱敏摘要

| 文件 | 内容 |
|---|---|
| [../RELEASE_TSB_366_R1.md](../../RELEASE_TSB_366_R1.md) | 完整发布记录：缺陷、修复、测试矩阵、兼容性检查、风险 |
| [DEFECTS.md](DEFECTS.md) | 缺陷清单与 RED→GREEN 对照 |

## 仅本地、不提交（本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-all.local.txt` | 修复前 24 项专项测试的真实失败输出（6 failed） |
| `backend-full.local.txt` / `backend-full.local.xml` | 后端完整运行日志与 junit |
| `vision-408-recheck.local.txt` | 408 用例在本轮 HEAD 上的独立复测输出 |

## 关键结论速查

| 项 | 结果 |
|---|---|
| 5 个 P0/P1 缺陷 | ✅ 全部修复 |
| 后端专项 | 24 passed |
| 前端专项 | 8 passed |
| 后端全量 | 1455 collected / 1 failed（基线既有 408）/ 5 skipped |
| 前端四项 | typecheck / lint / test(106) / build 全通过 |
| 数据库兼容性 | 正式库副本迁移 drift=0；**原库零接触** |
| 数据安全 | 旧 EditPlan 2 行、102 段、9 素材全部不变 |
