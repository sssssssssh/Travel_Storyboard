# TSB-366-R4 根因分析：视频链路为什么没进入 `complete_video()`

**基线：** `main@5890c10`　**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2

---

## 〇、先说清楚证据的边界

`used == []`（Provider spy 未记录到调用）**只**证明
`NativeVideoVisionProvider.complete_video()` 没被调用，**不能**单独指认是哪个函数失败。
任务书 §2.3 特别强调这一点，本报告因此分两层取证：

| 层 | 证明什么 | 平台相关性 |
| --- | --- | --- |
| **A. 契约层**（确定性） | 视频链路的每次派生文件访问**是否经过**长路径兼容 helper | 平台无关 |
| **B. 行为层**（真跑） | 正常路径上字节数/内容正确；真缺失仍抛错 | 平台无关 |
| **C. Windows 端到端** | 真实 MAX_PATH 下的完整链路 | **依赖 Windows CI** |

本机是 macOS，**没有 MAX_PATH 限制**，所以任务书 §4.1 的 35/58/76 矩阵在 macOS 上
**恒为绿**（实测确实全绿）。这不能证明缺陷不存在，也不该被当成"已修复"的证据。
真实缺陷由 A/B 两层定位，C 层由 CI 的 Windows 作业承担。

---

## 一、失败链路的准确断点

按任务书 §2.3 的十一个环节，逐一给出定位结果：

| # | 环节 | 有无缺陷 | 说明 |
| --- | --- | --- | --- |
| 1 | `create_owned_staging()` 建 staging | ✅ 无 | `staging_cleanup.py` 已用 `long_path`（`_lstat`/`_listdir`/`pinned_directory`） |
| 2 | `derive_clip()` 调 FFmpeg | ✅ 无 | argv 里的输出路径由 FFmpeg 直接处理（原生程序不受 MAX_PATH 限制） |
| 3 | FFmpeg 输出存在性验证 | ✅ 无 | `ffmpeg.run(expect_output=...)` → `_verify_expected()` **已内置** `long_path` 回退 |
| 4a | `probe_clip()` 文件检查 | ❌ **D1** | `if not path.is_file()` —— 普通 API |
| 4b | `probe_clip()` → ffprobe | ✅ 无 | `ffmpeg.probe()` 已用 `long_path(source)` |
| 5 | ClipArtifact 大小 / SHA-256 | ❌ **D1** | `size = path.stat().st_size` —— 普通 API（`_sha256` 已用 `safe_open`，是正确的） |
| 6a | `_analyze_one()` 预算计量 | ❌ **D2** | `clip.path.stat().st_size` —— 普通 API |
| 6b | `_analyze_one()` 构造 VideoPart | ❌ **D2** | `clip.path.read_bytes()` —— 普通 API |
| 7 | 授权检查 | ✅ 无 | 不涉及文件系统 |
| 8 | `provider.complete_video()` | （未到达） | 被 4a/5/6a/6b 拦截 |
| 9 | loopback HTTP | （未到达） | 同上 |
| 10 | Observation 持久化 | （未到达） | 同上 |
| 11 | staging cleanup | ❌ **D3**（次要） | `path_occupies_disk()` 用普通 `os.lstat`，靠"吞异常"凑巧方向正确 |

**结论：准确失败节点是 `probe_clip()` 的 `is_file()`/`stat()`（D1）与
`_analyze_one()` 的 `stat()`/`read_bytes()`（D2），全部位于 `complete_video()` 之前。
D1 会先命中（`probe_clip` 在 `derive_clip` 内、早于 `_analyze_one`）。**

### 为什么表现是 `used == []` 而不是一个明确的失败用例

- **D1** 抛 `VideoClipError(CLIP_UNREADABLE, "derived clip was not created")`
  —— 消息本身误导：文件**真实存在**，只是普通 API 看不见它。
- **D2** 抛 `FileNotFoundError` —— 它不是 `ModelError`，所以不走预算/能力分支，
  直接作为未分类异常终结该 segment。

两者都在 Provider 之前，于是 spy 永远是空列表。
**这也解释了为什么"单独复跑就过"**：换一台 TEMP 更短的机器，路径没到 260，缺陷消失。

---

## 二、三处缺陷的修复前后

### D1 — `backend/app/services/vision/video_clip.py::probe_clip()`

```python
# 修复前
if not path.is_file():
    raise VideoClipError(CLIP_UNREADABLE, "derived clip was not created")
...
size = path.stat().st_size

# 修复后
if not safe_is_file(path):
    raise VideoClipError(CLIP_UNREADABLE, "derived clip was not created")
...
size = safe_stat(path).st_size
```

`safe_is_file` / `safe_stat` 只在**路径缺失**时回退到 `long_path` 形式；
`PermissionError` 等真实错误原样抛出（不会被误判成"文件不存在"）。

### D2 — `backend/app/services/vision/video_job.py::_analyze_one()`

```python
# 修复前
plan.budget.note_derived([(clip.duration_sec, clip.path.stat().st_size)])
...
data=clip.path.read_bytes(),

# 修复后
plan.budget.note_derived([(clip.duration_sec, _clip_size_bytes(clip.path))])
...
data=_clip_read_bytes(clip.path),
```

新增两个辅助函数，内部走 `safe_stat` / `safe_open`：

```python
def _clip_size_bytes(path) -> int:
    return safe_stat(path).st_size

def _clip_read_bytes(path) -> bytes:
    with safe_open(path, "rb") as fh:
        return fh.read()
```

**契约不变**：预算仍按真实派生字节计；Provider 仍收到完整 MP4；
真缺失仍抛 `FileNotFoundError`（不伪造 0 字节 / 空 bytes）。

### D3 — `backend/app/services/vision/video_job.py::path_occupies_disk()`

```python
# 修复前：超长路径抛 OSError，被 except OSError 吞成"存在"
try:
    os.lstat(stage)
except (FileNotFoundError, NotADirectoryError):
    return False
except OSError:
    return True
return True

# 修复后：两条异常分支都先用 long_path 复核
try:
    os.lstat(target)
except (FileNotFoundError, NotADirectoryError):
    try:
        os.lstat(long_path(target))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True
except OSError:
    try:
        os.lstat(long_path(target))
    ...
```

修复前答案方向凑巧正确，但**理由错误**：它把"路径过长"伪装成"存在但不可读"，
掩盖真实错误（违反 C6）。`lstat`（不跟随链接）的 lexists 语义保持不变。

---

## 三、为什么本机 35/58/76 矩阵全绿而 Windows 76 会红

| | macOS | Windows (`LongPathsEnabled=0`) |
| --- | --- | --- |
| 路径 300 字符 | 正常读写 | 普通 Win32 API 失败 |
| `Path.is_file()` | 正常 | **返回 False**（不抛异常） |
| `Path.stat()` / `read_bytes()` | 正常 | 抛 `FileNotFoundError` / `OSError 206` |
| `long_path()` | 原样返回（`os.name != "nt"`） | 加 `\\?\` 前缀 |
| 视频链路 | 全绿（缺陷不可见） | 在 D1/D2 处中断 |

`app/ffmpeg.py` 的 `safe_*` 用 `os.name == "nt"` 作门禁是**正确的**：
POSIX 没有 MAX_PATH，`\\?\` 在那里无意义。测试若强行在 macOS 上要求它走回退分支，
等于要求代码做一件不该做的事。**这正是本轮改用契约层断言的原因。**

---

## 四、未能做到的事（诚实列出）

1. **本机无法复现 Windows MAX_PATH 端到端失败。** 35/58/76 矩阵在 macOS 全绿，
   只能证明"没有引入回归"，不能证明 Windows 行为。真实 Windows 验证依赖 CI。
2. 早期尝试用"注入式模拟 Win32 短路径 API"复现，**未能做可靠**：
   POSIX 上不存在 `\\?\` 别名，模拟要么自相矛盾、要么需要改写生产代码的门禁
   （那是为了让测试变绿而改代码，明确禁止）。该方案已废弃，改用契约层断言。
3. 因此 D1/D2/D3 的"红灯"证据是**契约层**的：去掉修复后，
   `test_tsb366r4_video_long_paths.py` 中对应用例转红（见 REVERSE-VERIFY.txt）。
   这条证据链是确定的，但它证明的是"接线正确"，不是"Windows 上实测通过"。
