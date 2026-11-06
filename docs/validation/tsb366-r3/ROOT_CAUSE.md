# TSB-366-R3 根因与证据

| 字段 | 内容 |
|---|---|
| 诊断阶段基线 | `12bef0a984d5c0193ce5a0a42b2493b02f60b6f6`（任务书写作 `ba7c9c6`，为其父提交，`backend/` 代码相同） |
| 失败用例 | `tests/test_ffmpeg_integration.py::test_complete_custom_workspace_pipeline_and_source_hashes` |

> 本轮诊断**未修改任何生产代码**，结论全部来自实测。

---

## 一、根因

**`_safe_save()` 的长路径回退分支从未生效过。**

```python
try:
    image.save(path, **kwargs)          # 靠 path 的 .jpg 扩展名推断编码器
except (FileNotFoundError, OSError):
    buffer = _io.BytesIO()
    image.save(buffer, **kwargs)        # ← BytesIO 没有扩展名，kwargs 只有 quality
                                        #   → ValueError: unknown file extension
```

目标路径超过 `MAX_PATH`(260) 时，第一次保存抛 `FileNotFoundError`，回退分支必然抛
`ValueError`；调用方 `except (OSError, ValueError): reencode_failures += 1; continue`
把异常当作"重编码失败"吞掉，于是**所有代表帧被静默丢弃**。

### 完整链条

```
目标路径 > 260
  → image.save(path) 抛 FileNotFoundError
  → _safe_save 回退到 BytesIO
  → 未传 format ⇒ Pillow: ValueError: unknown file extension
  → 调用方吞掉，reencode_failures += 1, continue
  → frames_out 全空 → segments_json 全空
  → "无当前有效镜头：代表帧缺失或预算不足"
```

### 直接证据

| 实验 | 结果 |
|---|---|
| `img.save(BytesIO(), quality=82)` | `ValueError: unknown file extension:` |
| `img.save(BytesIO(), format="JPEG", quality=82)` | 成功 |
| `img.save("x.jpg", quality=82)` | 成功 |
| `_sanitized_copy` → 267 字符目的地 | `ValueError: unknown file extension:` |
| `_sanitized_copy` → 257 字符目的地 | 成功 |

---

## 二、代表帧在哪一层丢失

失败时刻的逐层实测计数（用测试外观测插件获得）：

| 层 | 计数 | 结论 |
|---|---|---|
| `eligible_segments` | **5** | 有合格镜头 |
| `segments_with_frame_rows` | **5** | 全部有帧行 |
| `frame_rows_total` | **5** | 帧行存在 |
| `frames_inside_range` | **5** | 时间范围正确 |
| `frames_outside_segment_range` | **0** | 无越界 |
| `resolve_failures` | **0** | 路径解析成功 |
| `file_missing` / `file_ok` | **0 / 5** | 文件全部存在 |

**查询、时间范围、路径解析、文件存在四层一层都没丢。**

继续追到被吞掉的真实异常：

```
5 次 _sanitized_copy 调用全部抛：
  ValueError: unknown file extension: 
  src_len=228, dst_len=267, src_exists=True
```

**结论：帧全部丢失在"重编码保存"这一步。**

---

## 三、触发条件：路径长度阈值

| tmp_path 根长度 | 帧目标路径 | 超 MAX_PATH(260) | 结果 |
|---|---|---|---|
| 35（系统默认） | ≈191 | 否 | 3/3 通过 |
| 87 | 243 | 否 | 6/6 通过 |
| **111（深目录）** | **267** | **是** | **3/3 失败** |

这解释了全部现象，包括"同一目录重跑有时通过"（`pytest-N` 递增改变深度）。

---

## 四、本机与 CI 差异

| 维度 | 本机 | CI |
|---|---|---|
| FFmpeg / FFprobe | 9.0.2 essentials，含 libx264/libx265 | 同（作业安装） |
| Pillow / OpenCV | 12.3.0 / 5.0.0 | 同版本 |
| **tmp_path 根长度** | 取决于 TEMP，**可被设得很深** | **短** |

**关键差异不是编解码器或库版本，而是临时目录的绝对路径长度。**
CI runner 的临时目录短，路径不越 260 ⇒ 该缺陷在 CI 上**永不暴露**。

---

## 五、两个 SHA 是同一失败机制

隔离 worktree 中，同机器、同 FFmpeg、同命令各跑 3 次：

| SHA | 深临时目录 |
|---|---|
| `6771e93`（任务书对照基线） | 3/3 失败 |
| `12bef0a`（当前 HEAD） | 同目录下同样失败 |

同一异常文本、同一 `package` 阶段、同一 `_safe_save` 缺陷。
`ba7c9c6` 只改了 Vision Provider，与 `review_package.py` 无关 ⇒ 缺陷自引入起一直存在。

---

## 六、整改阶段新发现的两处长路径缺陷

修复 `_safe_save` 后，目标用例暴露出**同一类问题的另外两处**（此前被第一处掩盖）：

| # | 位置 | 机制 |
|---|---|---|
| 2 | contact sheet 保存 | 与 #1 同源，走 `_safe_save`，随 #1 一并修复 |
| 3 | **zip 写入** `zf.write(p, ...)` | `zipfile` 内部 `os.stat(path)` 在 >MAX_PATH 时找不到文件，抛 `FileNotFoundError` |

`test_complete_custom_workspace_pipeline_and_source_hashes` 的失败点在修复过程中
从"代表帧缺失"转移到 `contact_sheets/sheet_0001.jpg`，最后定位到 zip 写入。

### 长路径助手实测矩阵（274–278 字符）

| 操作 | 结果 |
|---|---|
| `safe_mkdir` | OK（`Path.exists()` 在超长普通路径下会误报 False，属已知平台陷阱） |
| `long_path` 写入 / `safe_is_file` | OK |
| Pillow `save(path)` | **FAIL**（缺陷 #1） |
| Pillow + `long_path` + 显式 format | OK（修复后） |
| **zipfile `write(path)`** | **FAIL**（缺陷 #3） |
| ffmpeg 输出到长路径 | OK |

---

## 七、根因归类

按任务书 §5：**产品缺陷（长路径处理）**，不是测试夹具、不是 FFmpeg/Pillow 兼容问题、
不是 detect 发布缺陷、不是 package 选择缺陷。

三条判据：
1. 触发条件是**用户可真实遇到**的路径长度（自定义 `output_dir` 放深目录）；
2. 项目明确承诺长路径支持（`long_path()`、`safe_*` 系列的存在即为证据）；
3. 后果是**静默丢帧**，只给一句合并了两种原因的提示，违反 C6。
