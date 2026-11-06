# TSB-366-R3 — 自定义工作区分析包代表帧缺失

| 字段 | 内容 |
|---|---|
| 诊断基线 | `12bef0a984d5c0193ce5a0a42b2493b02f60b6f6` |
| 根因与证据 | [ROOT_CAUSE.md](validation/tsb366-r3/ROOT_CAUSE.md) |
| 证据目录 | [docs/validation/tsb366-r3/](validation/tsb366-r3/) |
| **交付 SHA** | **`73bf982c6b8d6ee71df2d901321b03015e673b4b`** |
| CI | run #103，**7/7 success** —— https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42189919219 |

> **结论**：代表帧**没有**丢在查询、时间范围、路径解析或预算这些环节，
> 而是**全部丢在"重编码保存"**：`_safe_save()` 的长路径回退分支
> `image.save(BytesIO, **kwargs)` 缺少 `format`，在目标路径超过 `MAX_PATH`
> 时必然抛 `ValueError: unknown file extension`，又被调用方当成"重编码失败"吞掉。
> 修复过程中还暴露并修复了**同一类的另外两处**：contact sheet 保存、zip 写入。

---

## 一、问题

`test_complete_custom_workspace_pipeline_and_source_hashes` 在本机间歇失败，
CI 一直通过。失败信息只有一句：

```
ValueError: 无当前有效镜头：代表帧缺失或预算不足，请重新检测或增加分析包预算
```

这句话把"文件缺失"和"预算不足"合并了，且**完全没有提到重编码失败**——
而后者正是真实原因。

### 触发条件（实测）

| tmp_path 根长度 | 帧目标路径 | 超 MAX_PATH(260) | 结果 |
|---|---|---|---|
| 35（系统默认） | ≈191 | 否 | 3/3 通过 |
| 87 | 243 | 否 | 6/6 通过 |
| **111** | **267** | **是** | **3/3 失败** |

### 逐层排除（失败时刻实测）

| 层 | 计数 |
|---|---|
| 合格镜头 / 有帧行镜头 / 帧行总数 | 5 / 5 / 5 |
| 时间范围内 / 越界 | 5 / **0** |
| 路径解析失败 | **0** |
| 文件缺失 / 存在 | **0 / 5** |

**四层全部健康**，异常发生在之后的保存层。

---

## 二、三处长路径缺陷

| # | 位置 | 机制 | 状态 |
|---|---|---|---|
| 1 | `_safe_save()` 回退分支 | `image.save(BytesIO, quality=…)` 无 `format` → `ValueError` | ✅ 已修 |
| 2 | contact sheet 保存 | 与 #1 同源（走 `_safe_save`） | ✅ 随 #1 修复 |
| 3 | **zip 写入** `zf.write(p, …)` | `zipfile` 内部 `os.stat()` 看不见 >MAX_PATH 的路径 | ✅ 已修 |

#2、#3 是在修复 #1 之后**才暴露**的——此前被第一处完全掩盖。

---

## 三、修复

### 3.1 核心（缺陷 #1）

```python
except (FileNotFoundError, OSError):
    buffer = _io.BytesIO()
    save_kwargs = dict(kwargs)
    save_kwargs.setdefault("format", _LONG_PATH_SAVE_FORMAT)   # ← 显式 JPEG
    image.save(buffer, **save_kwargs)
    with open(long_path(path), "wb") as handle:
        handle.write(buffer.getvalue())
```

`setdefault` 而非硬编码：调用方已指定 `format` 时（contact sheet 传 `format="JPEG"`）保持其选择；
编码选项（`quality`）原样转发，保证短路径与长路径结果一致。

### 3.2 zip 写入（缺陷 #3）

```python
with zipfile.ZipFile(long_path(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(pkg_dir.rglob("*")):
        if not safe_is_file(p):
            continue
        with open(long_path(p), "rb") as handle:
            zf.writestr(p.relative_to(pkg_dir.parent).as_posix(), handle.read())
```

`ZipFile.write(path)` 内部用 `os.stat`，长路径下不可用；改为读字节后 `writestr`，
归档内容与条目名保持不变。

### 3.3 错误可见性（C6：不静默跳过）

| 改动 | 说明 |
|---|---|
| 四类原因分开计数 | `reencode_failures` / `excluded_missing_frames` / `excluded_by_budget` / `excluded_resolve_failures` / `excluded_outside_range` |
| 记录首个重编码失败 | `_describe_reencode_error()`：异常类型、原因、**目标路径长度**（不记录完整私人路径） |
| 全失败时给明确原因 | `_no_frames_message()` 按主因给出可操作提示，**重编码失败绝不再写成"预算不足"** |
| 部分失败时保留成功帧 | 失败帧计数并记入 `failures.record`，成功帧照常进包 |
| manifest 增加诊断字段 | `selection.reencode_failures`、`selection.first_reencode_error` 等 |

修复后的错误示例：

```
无当前有效镜头：1 个合格镜头均未能产出代表帧。1 张代表帧重编码失败，无法写入分析包
（首个失败：ValueError unknown file extension；目标路径 267 字符≥ MAX_PATH 260=True）。
```

---

## 四、测试

### 4.1 新增（15 项）

| 文件 | 项数 | 覆盖 |
|---|---:|---|
| `tests/test_tsb366r3_long_path.py` | 12 | 长路径保存、短/长路径结果一致、深目录创建读写、EXIF/GPS 清理、**强制回退分支**、错误消息分因、诊断不泄漏路径 |
| `tests/test_tsb366r3_package_deep.py` | 3 | 深路径 package 的帧**确实进入 `segments.json` 与 zip**、计数分开、全失败消息可操作 |

### 4.2 平台处理（不静默跳过）

| 平台 | 处理 |
|---|---|
| **Windows** | 真实执行全部长路径用例（含 267+ 字符路径） |
| **Linux** | 4 项 Windows 专属用例带**明确 skip 原因**（`MAX_PATH only constrains Windows`，已在 `conftest._EXPECTED_SKIP_REASONS` 白名单）；回退分支由 `test_the_long_path_fallback_is_reachable_even_without_max_path` **强制模拟首次失败**来覆盖 |

### 4.3 原有测试

**未修改、未删除、未 skip、未放宽任何断言。**
`test_complete_custom_workspace_pipeline_and_source_hashes` 保持原样。

### 4.4 反向验证

移除回退分支的 `format`：

```
FAILED test_safe_save_succeeds_past_max_path
FAILED test_safe_save_long_path_result_matches_a_short_path
FAILED test_sanitized_copy_succeeds_past_max_path
FAILED test_the_long_path_fallback_is_reachable_even_without_max_path
FAILED test_the_fallback_names_the_format_explicitly
```

5 项变红（含 Linux 也会执行的那两条）。恢复后 15 项全绿，无残留标记。

---

## 五、验证结果

| 项 | 结果 |
|---|---|
| 新增 R3 测试 | **15 全绿** |
| **目标用例连续 10 次** | **10/10 通过** |
| **FFmpeg 集成文件连续 3 次**（深临时目录） | **3/3 通过** |
| 目标用例（曾 3/3 失败的深临时目录） | 通过 |
| **后端完整测试** | **1716 collected / 0 failed / 0 errors / 5 skipped** |

5 项 skip 均为已登记的 `link creation is a platform fact`，与 TSB-361 记录一致。

### 关于临时目录长度的一个真实约束

本机全量结果**依赖临时目录根长度**，这是既有事实（非本轮引入）：

| 临时目录根长度 | 观察 |
|---|---|
| 21 / 35 字符 | `test_deep_scan_api::test_scan_320_char_cjk_root` 失败（构造不出 320 字符路径） |
| **58 字符** | **两者都通过** ← 本轮全量使用的长度 |
| 76+ 字符 | `test_video_provider_wiring` / `test_video_e2e_formal_api` 失败（§七 的遗留缺陷） |

即：跑全量需要一个"不长不短"的临时目录。这是环境约束，不是本轮缺陷，但会
让"本机全量通过"的结论依赖具体机器 —— 已如实记录，未用缩短测试路径的方式掩盖。

---

## 六、范围声明

**未改动**：TSB-366-R2 的代理修复、TSB-367 操作协议与 Alembic、任何原片、
任何测试断言强度。

**未做**：contact sheet **布局**逻辑、proxies 阶段、roughcut 阶段的路径处理均**未重构**——
因为长路径助手实测矩阵显示它们工作正常，`proxies`/`roughcut` 在修复后的目标用例中
也已端到端通过。无证据不做扩大整改。

---

## 七、范围外发现：video 链路也有长路径缺陷（未修，记录）

排查过程中发现，当临时目录根长度达到 **76 字符**时，以下测试失败：

- `test_video_provider_wiring.py::test_r07*`、`test_r10*`
- `test_video_e2e_formal_api.py::test_v31`、`test_v34`

失败形态：`video_analyze_task` 在调用 `complete_video` **之前**就结束了
（`used == []`），属于派生/暂存阶段的路径问题，**与本轮修复的
`review_package.py` 无关**（本轮生产改动只涉及该文件）。

| 临时目录根长度 | video 套件 |
|---|---|
| 21 / 35 字符 | PASS |
| **76 字符** | **FAIL** |

**按任务书 §3「不要为了扩大整改范围而无证据重构」的要求，本轮不修**，
列为遗留项，建议单独开一轮（TSB-366-R4 或并入视频链路专项）处理。

判定依据：本轮生产代码改动**仅** `backend/app/services/review_package.py`
（`git status` 可验证），视频链路未被触碰。

---

## 八、尚未完成 / 遗留

1. **真实素材验收未做**：全部验证基于合成素材。
2. **CI 无法复现该缺陷**：runner 临时目录短，缺陷只有在深路径下暴露。
   本轮新增的测试**主动构造**深路径，因此具备回归能力，但 CI 上跑的是同一套用例。
3. **`safe_mkdir` 的 `Path.exists()` 陷阱**：超长普通路径下 `Path.exists()` 返回 False
   而目录其实已创建。本轮未改该行为（现有代码路径都用 `safe_is_file`），
   但需要在后续改动中留意。
4. **`long_path()` 对新建文件依赖父目录已存在**：目录创建仍需 `safe_mkdir`，
   两者必须配合使用。


---

## 九、CI 执行结果

| 项 | 值 |
|---|---|
| 交付 SHA | `73bf982c6b8d6ee71df2d901321b03015e673b4b` |
| run | **#103** |
| 链接 | https://github.com/sssssssssh/Travel_Storyboard/actions/runs/42189919219 |
| 结论 | **7/7 success** |

| 作业 | 结论 |
|---|---|
| `Required FFmpeg (windows-latest)` | ✅ success |
| `Required FFmpeg (ubuntu-latest)` | ✅ success |
| `Backend (Python 3.11)` ×2 | ✅ success |
| `Backend (Python 3.13)` ×2 | ✅ success |
| `Frontend` | ✅ success |

**Required FFmpeg Windows / Ubuntu 真实执行并通过**——这两个作业跑的就是
`test_ffmpeg_integration.py`（普通 Backend 作业会 ignore 它）。

本轮新增的 15 项长路径测试也在 `backend` 作业范围内执行；其中 4 项 Windows 专属
用例在 Ubuntu 上带明确 skip 原因，回退分支由强制模拟用例覆盖。

---

## 十、完成判定（对照任务书 §9）

| 条件 | 状态 |
|---|---|
| 确认 main HEAD 和工作区 | ✅ `12bef0a`，干净 |
| 目标用例本机 3 次复现结果已记录 | ✅ 深目录 3/3 失败、默认目录 3/3 通过 |
| 确认失败在 package 阶段 | ✅ 第 5 步 `execute('package')` |
| 读取失败 Job 的完整 error/stats | ✅ 各层计数已采集 |
| 采集 Asset/Segment/Frame 数据库状态 | ✅ 见 ROOT_CAUSE §二 |
| 确认代表帧在哪一层被排除 | ✅ **重编码保存**层 |
| 验证 image_path / resolve / safe_is_file / Pillow 解码 | ✅ 四层全部健康 |
| 核对路径长度、tmp_path、custom output_dir | ✅ 阈值 260 已定量 |
| 核对 FFmpeg/Pillow/OpenCV 与 CI 差异 | ✅ 差异在 tmp_path 长度 |
| 对比两个 SHA 是否同一失败机制 | ✅ 同一机制 |
| 根因有可复现证据 | ✅ 三处缺陷全部可复现 |
| 修复不破坏原片只读/目录隔离/隐私清理 | ✅ EXIF/GPS 清理有专门测试 |
| **目标用例 10/10** | ✅ **10/10** |
| **FFmpeg 集成文件 3/3** | ✅ **3/3** |
| 完整测试通过 | ✅ **1716 / 0 failed / 5 skipped** |
| 现有 CI 通过 | ✅ run #103 7/7 |
| 发布记录和证据索引完成 | ✅ 本文 + `ROOT_CAUSE.md` + `README.md` + `DIAGNOSIS.md` |
| 已提交推送，工作区干净 | ✅ `73bf982`，与 origin 一致 |

**关于"完整测试通过"**：本机全量 1716 项 0 失败是在**临时目录根 58 字符**下取得的。
这一点已在 §五 如实说明：临时目录过短会让 320 字符用例失败，过长会触发
视频链路的另一处既存长路径缺陷（§七，未修）。**没有通过缩短测试路径或改断言来达成绿色。**
