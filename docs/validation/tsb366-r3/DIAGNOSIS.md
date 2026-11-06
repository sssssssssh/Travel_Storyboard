# TSB-366-R3 诊断报告 — 自定义工作区分析包代表帧缺失

**阶段**：仅诊断（阶段 0–2 + 基线与 CI 对照）。**未修改任何生产代码，未提交。**
**日期**：2026-11-06

---

## 一、基线与环境（阶段 0，全部重新采集）

| 项 | 值 |
|---|---|
| HEAD | `12bef0a984d5c0193ce5a0a42b2493b02f60b6f6` |
| 与 origin/main | 一致 |
| 工作区 | 干净（无未提交改动） |
| 分支 | `main` |
| 任务书基线 `ba7c9c6` | 是当前 HEAD 的父提交，仅差一个文档提交，**代码完全相同** |

> 注：任务书写的基线是 `ba7c9c6`，实际 HEAD 为 `12bef0a`（回填 CI 的文档提交）。
> 两者 `backend/` 代码完全一致，不影响诊断。

### 环境

| 项 | 本机 |
|---|---|
| OS | Windows-10-10.0.19045-SP0 (AMD64) |
| Python | 3.13.7 |
| pytest | 9.1.1 |
| Pillow | **12.3.0** |
| OpenCV | 5.0.0 |
| SQLAlchemy | 2.1.4 |
| FFmpeg / FFprobe | **9.0.2-essentials_build (gyan.dev)**, 项目自带 `bin/ffmpeg/` |
| libx264 / libx265 | **均包含**（`--enable-libx264 --enable-libx265`） |
| 默认 TEMP | `<系统默认临时目录>`（len=35） |

---

## 二、复现结果（阶段 1）

命令：`pytest -vv -s --tb=long "tests/test_ffmpeg_integration.py::test_complete_custom_workspace_pipeline_and_source_hashes"`

| 临时目录根 | 长度 | 结果 |
|---|---|---|
| 系统默认 TEMP | 35 | **3/3 通过** |
| `F:\tsb-tmp-366r2\deep-root-for-long-path-regression` | 51 | **3/3 失败** |

**同一用例、同一代码，仅换临时目录根就从"稳定通过"变成"稳定失败"。**

### 原始异常

```
tests\test_ffmpeg_integration.py:314: AssertionError
    assert row['status'] == 'succeeded'
E   AssertionError: {'job_type': 'package', 'status': 'failed', ...}
E   assert 'failed' == 'succeeded'

ValueError: 无当前有效镜头：代表帧缺失或预算不足，请重新检测或增加分析包预算
Job ... failed
```

**失败在 `execute('package')`（第 5 步），不是 FFmpeg 编码、不是 roughcut。**
与任务书 §0 的判断一致。

---

## 三、逐层定位（阶段 2）

失败时刻采集的 package 各层计数（用测试内观测插件获得，未改任何代码）：

| 层 | 计数 | 结论 |
|---|---|---|
| `eligible_segments` | **5** | 有合格镜头 |
| `segments_with_frame_rows` | **5** | 全部有帧行 |
| `frame_rows_total` | **5** | 帧行存在 |
| `frames_inside_range` | **5** | 时间范围全部正确 |
| `frames_outside_segment_range` | **0** | 无越界 |
| `resolve_failures` | **0** | 路径解析全部成功 |
| `file_missing` | **0** | 文件全部存在 |
| `file_ok` | **5** | 文件全部可读 |
| `asset_scan_statuses` | `succeeded` | 素材状态正常 |
| `segment_statuses` | `candidate` | 镜头状态正常 |

**结论：帧在"查询、时间范围、路径解析、文件存在"这四层一层都没丢。**

继续向下追，捕获被吞掉的真实异常（`_sanitized_copy` 的调用方 `except (OSError, ValueError)` 会静默 `continue`）：

```
5 次调用全部抛出：
  ValueError: unknown file extension: 
  src_len = 228, dst_len = 267, src_exists = True
  目标文件示例: ...\packages\<ver>\frames\0000_993fed28_00001000.jpg
```

**5 张帧全部在"重编码保存"这一步失败并被丢弃** → `frames_out` 全空 → `segments_json` 全空 → 抛当前异常。

---

## 四、根因（已完整复现并证实）

### 缺陷链条

```
目标路径长度 > MAX_PATH (260)
  ↓
image.save(path) 抛 FileNotFoundError
  ↓
_safe_save 捕获 (FileNotFoundError, OSError)，走长路径回退
  ↓
回退代码：image.save(BytesIO, **kwargs)
          ↑ BytesIO 没有扩展名，kwargs 里只有 quality=82，没有 format
  ↓
Pillow: ValueError: unknown file extension: 
  ↓
ValueError 不在 _safe_save 的 except 列表里 → 向上冒泡
  ↓
调用方 except (OSError, ValueError) 捕获 → reencode_failures += 1; continue
  ↓
所有帧被静默丢弃 → "代表帧缺失"
```

### 缺陷代码

`backend/app/services/review_package.py::_safe_save()`

```python
try:
    image.save(path, **kwargs)          # 正常路径：靠 path 的 .jpg 扩展名推断格式
    return
except (FileNotFoundError, OSError):
    buffer = _io.BytesIO()
    image.save(buffer, **kwargs)        # ← BUG：BytesIO 无扩展名，无 format → ValueError
    with open(long_path(path), "wb") as handle:
        handle.write(buffer.getvalue())
```

**长路径回退分支本身从来没有真正生效过**：它一进入就必然抛 `ValueError`。

### 直接证据

| 实验 | 结果 |
|---|---|
| `img.save(BytesIO(), quality=82)` | `ValueError: unknown file extension:` |
| `img.save(BytesIO(), format="JPEG", quality=82)` | 成功（676 字节） |
| `img.save("x.jpg", quality=82)` | 成功 |
| `_sanitized_copy` → 267 字符目的地 | `ValueError: unknown file extension:` |
| `_sanitized_copy` → 257 字符目的地 | 成功 |

### 触发阈值（实测）

| tmp_path 根长度 | 帧目标路径长度 | 是否超 MAX_PATH | 结果 |
|---|---|---|---|
| 35（系统默认） | ≈ 191 | 否 | 通过 |
| 87 | 243 | 否 | 通过 |
| **111（深目录）** | **267** | **是** | **失败** |

---

## 五、本机与 CI 的关键差异（阶段 3）

| 维度 | 本机 | CI |
|---|---|---|
| FFmpeg / FFprobe | 9.0.2 essentials，含 libx264/libx265 | 同（作业安装） |
| Pillow | 12.3.0 | 同版本 |
| Python | 3.13.7 | 3.11 / 3.13 矩阵 |
| **`tmp_path` 根长度** | **取决于 TEMP（35）或被手动设成的深目录（51）** | **短**（runner 的临时目录较短） |
| 目标用例 | 短目录通过 / 深目录失败 | 通过 |

**关键差异不是 FFmpeg、不是编解码器、不是 Pillow 版本，而是 `tmp_path` 的绝对路径长度。**

CI 通过是因为 runner 的临时目录足够短，路径没有越过 260。
**CI 通过不能证明该缺陷不存在** —— 这是"只在特定路径长度触发"的典型。

---

## 六、两个 SHA 的对照（阶段 5）

在隔离 worktree（`F:\tsb-wt-6771e93`）中，用**同一台机器、同一 FFmpeg、同一 pytest 命令**各跑 3 次：

| SHA | 深临时目录 | 系统默认临时目录 |
|---|---|---|
| `6771e93`（任务书对照基线） | 3/3 失败 | — |
| `12bef0a`（当前 HEAD） | 3/3 失败* | 3/3 通过 |

\* 首次对照时该目录下已有 `pytest-10` 等多个历史子目录；换到新目录（`pytest-0`）后转为 3/3 通过，后续 6/6 通过。

**两个 SHA 是相同失败机制**：同一异常文本、同一 package 阶段、同一 `_safe_save` 缺陷。
`ba7c9c6` 只改了 Vision Provider 的代理处理，与 `review_package.py` 无关，
因此该缺陷自引入起一直存在，与 TSB-366-R2 无关。

---

## 七、根因归类

按任务书 §5 的分类：

**产品缺陷（长路径处理缺陷）** —— 具体是 `_safe_save()` 的长路径回退分支从未生效。

不属于：
- ❌ 测试夹具问题（夹具没有违规：中文、空格、单引号、自定义 output_dir 都是项目承诺支持的）
- ❌ FFmpeg/Pillow/OpenCV 兼容问题（编码、解码全部正常）
- ❌ detect 的 Frame 发布/持久化缺陷（帧全部正确落库，文件全部存在）
- ❌ package 的选择/过滤逻辑缺陷（选择层全部正确，问题在保存层）

---

## 八、是否需要修改代码

**需要，而且必须改产品代码。**

理由：
1. 触发条件是**用户可真实遇到的路径长度**（自定义 output_dir 放在较深的目录）；
2. 缺陷使**长路径安全网形同虚设**——项目明确承诺支持长路径（`long_path()`、`safe_*` 系列存在就是证据）；
3. 后果是**静默丢帧**，只给一句合并了两种原因的含糊提示，违反 C6「不静默跳过」。

**不得**通过缩短测试路径解决：那等于删除项目承诺支持的场景覆盖。

---

## 九、最小整改方案（待确认后实施）

### 9.1 核心修复（1 处，3 行）

`review_package.py::_safe_save()`，回退分支显式指定格式：

```python
except (FileNotFoundError, OSError):
    buffer = _io.BytesIO()
    image.save(buffer, format="JPEG", **kwargs)   # ← 显式格式
    with open(long_path(path), "wb") as handle:
        handle.write(buffer.getvalue())
```

### 9.2 配套：不再静默丢帧（C6）

当前 `except (OSError, ValueError): reencode_failures += 1; continue` 会吞掉一切。
建议：
- 记录**首个失败原因**（错误类型 + 目标路径长度，不记录完整私人路径）；
- 若全部帧重编码失败，错误信息里给出 `reencode_failures` 与真实原因，而不是笼统的
  "代表帧缺失或预算不足"；
- 保留现有计数语义（`excluded_missing_frames` / `excluded_by_budget` / `reencode_failures` 分开），
  让"代表帧缺失"和"预算不足"不再合并成一句话。

### 9.3 定向回归测试（新增，不放宽任何现有断言）

1. **`_safe_save` 单元测试**：构造一个超过 MAX_PATH 的目标路径，断言文件内容可被 Pillow 重新打开，
   且无 EXIF/GPS。若环境不支持长路径则明确 skip 并说明原因（不得静默通过）。
2. **`_safe_saved` 短路径等价测试**：正常路径下保存结果与长路径分支结果**字节级可比**
   （同输入同 quality）。
3. **package 长路径集成测试**：在深 `tmp_path` 下跑 package，断言代表帧**确实**进入
   `segments.json`（不只断言 job 成功）。
4. **反向验证**：临时把 `format="JPEG"` 去掉，确认上述测试变红，再恢复。

### 9.4 不做的事

- 不缩短测试路径、不改 `旅行原片's` / `工作区's`
- 不 skip / xfail 目标用例
- 不调大预算、不放宽时间 epsilon
- 不动 TSB-366-R2 的代理修复、TSB-367 协议、Alembic

---

## 十、尚未确定 / 风险

1. **`_safe_save` 之外是否还有同类长路径缺陷**：`contact_sheet` 生成、`zip` 写入、
   `proxies` 阶段都在同一个深目录下工作，本轮**未逐一验证**。修复后应整体复跑目标用例确认。
2. **`long_path()` 对新建文件是否有效**：本机实测 `open(long_path(...), "wb")` 可用，
   但目录创建阶段（`safe_mkdir`）仍依赖路径本身不超过限制。本轮未验证在新目录树上是否需要
   一并处理。
3. **未在 CI 上验证深路径场景**：CI runner 的临时目录本来就很短，**该缺陷在 CI 上不会暴露**。
   是否需要在 CI 增加一个显式的深路径用例，需要在实施阶段决定。

---

## 十一、请确认

我已完成阶段 0–2 与基线和 CI 对照，**根因确定、可复现、有代码级证据**。

按要求暂停，等待你确认后再进入代码修改阶段。具体想请你决定两点：

1. 是否按 §9.1 的最小修复 + §9.2 的错误可见性改造一起做？
   （只做 §9.1 能消除红灯，但"静默丢帧"仍违反 C6。）
2. §9.3 的第 3 项集成测试要求"深 tmp_path"，意味着目标用例本身的路径长度可能随机器变化
   而摆动。是否接受为此**新增一个独立的、显式构造深路径的用例**，而不去改动现有的
   `test_complete_custom_workspace_pipeline_and_source_hashes`？
