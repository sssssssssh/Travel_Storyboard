# TSB-366-R3 证据索引

基线：`12bef0a984d5c0193ce5a0a42b2493b02f60b6f6`

## 可提交的脱敏摘要

| 文件 | 内容 |
|---|---|
| [../../RELEASE_TSB_366_R3.md](../../RELEASE_TSB_366_R3.md) | 完整发布记录 |
| [ROOT_CAUSE.md](ROOT_CAUSE.md) | **根因与证据**：缺陷链条、逐层排除计数、路径长度阈值、本机与 CI 差异、两个 SHA 对照 |
| [DIAGNOSIS.md](DIAGNOSIS.md) | 诊断阶段报告（整改前输出） |

## 仅本地、不提交（本目录 `.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `REPRO-deep-run1.local.txt` | 深临时目录下目标用例的完整失败 traceback |
| `backend-full.local.txt` / `.xml` | 后端完整运行日志与 junit |

## 结论速查

| 项 | 结果 |
|---|---|
| 代表帧丢失位置 | **重编码保存**（不是查询/时间范围/路径解析/文件存在/预算） |
| 根因 | `_safe_save()` 回退分支 `image.save(BytesIO, **kwargs)` 缺 `format` |
| 触发阈值 | 目标路径 > `MAX_PATH` 260 |
| 同类缺陷（整改中新发现） | contact sheet 保存（同源）、**zip 写入**（`zipfile` 内部 `os.stat`） |
| 修复 | 回退分支显式 `format="JPEG"`；zip 改用 `long_path` 读字节 + `writestr` |
| 错误可见性 | 四类原因分开计数；全失败给明确原因；部分失败保留成功帧 |
| 反向验证 | 去掉 `format` → 5 项变红（含 Linux 也执行的强制回退用例） |
| 原有测试 | **未修改、未 skip、未放宽** |
| CI 能否复现 | **不能**（runner 临时目录短）；故新增测试**主动构造**深路径 |

## 修复前后的失败点迁移（证实三处缺陷依次暴露）

```
修复前：代表帧缺失（_safe_save 吞掉所有帧）
  ↓ 修 #1 后
中间态：contact_sheets/sheet_0001.jpg 找不到（#2，随 #1 一并修复）
  ↓
中间态：zipfile os.stat 找不到文件（#3）
  ↓ 修 #3 后
目标用例通过（深临时目录）
```
