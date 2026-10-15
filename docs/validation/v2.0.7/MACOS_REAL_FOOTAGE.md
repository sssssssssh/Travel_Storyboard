# macOS 真实素材端到端验收记录

**日期**：2026-10-15
**机器**：macOS 15.7.7 (Sequoia)，Apple Silicon (arm64)
**基线 commit**：`main` @ `cb8a666`
**结论**：**代码级链路在本机真实素材上端到端通过；Windows 实机发布门禁仍未完成。**

> 按 AGENTS.md 第六节（C8），本记录**不**构成真实素材发布门禁。
> 它证明的是：在 macOS + 真实 4K HEVC 无人机素材上，全链路可跑通且原片未变。
> Windows 实机 + LongPathsEnabled=0 的门禁见 `WINDOWS_REAL_FOOTAGE.md`（仍未执行）。

---

## 一、环境事实（与 AGENTS.md 的 Windows 假设不同）

| 项 | AGENTS.md 假设 | 本次实测 |
| --- | --- | --- |
| OS | Windows 开发机 | macOS 15.7.7 arm64 |
| Python | `backend/.venv` 3.13.7 | **`backend/.venv` 不存在**；使用根 `.venv` Python 3.12.14 |
| FFmpeg | 项目自带于 `bin/ffmpeg/` | **`bin/ffmpeg/` 不存在，PATH 上也没有** |
| FFmpeg 安装方式 | 自带 | 手工安装 Homebrew bottle **9.0.2**（见第三节） |
| Node / pnpm | 18.19.0 | node 可用；**pnpm 未安装**，改用 `node_modules/.bin/*` |
| `pytest-timeout` | 未安装 | 未安装（一致） |

**FFmpeg 版本与 AGENTS.md 要求一致：9.0.2。**

---

## 二、素材

`/Users/shush/Downloads/video`（**只读使用**）

| 文件 | 大小 | 时长 | 规格 |
| --- | --- | --- | --- |
| DJI_20261002163755_0001_D.MP4 | 295.6 MB | 20.50s | 3840×2160 HEVC, ~119.9fps, AAC 48k stereo |
| DJI_20261003134833_0002_D.MP4 | 678.7 MB | 48.58s | 同上 |
| DJI_20261003153602_0004_D.MP4 | 883.0 MB | 60.99s | 同上 |
| DJI_20261003161452_0005_D.MP4 | 760.3 MB | 51.82s | 同上 |
| DJI_20261003164352_0007_D.MP4 | 1988.4 MB | 140.79s | 同上 |
| DJI_20261003165009_0008_D.MP4 | 153.9 MB | 10.79s | 同上 |

合计 **4.4 GB / 6 段 / 333 秒**。全部 4K HEVC、带音轨、DJI OsmoAction5 Pro 机型标签
（含 `creation_time` 时间戳 → 可用于验证隐私处理）。

**覆盖面**：真实 4K 长片 ✅，HEVC ✅，有音轨 ✅，长短混合 ✅。
**未覆盖**：竖屏 ❌，无音轨 ❌，HDR ❌，VFR 显式确认 ❌。因此**不满足**门禁要求的
"8～10 段横/竖屏、有/无音轨、4K/HEVC" 全维度。

---

## 三、环境搭建（可复现）

`brew install ffmpeg` **失败**：网络无法访问 GitHub release，portable-ruby 下载中断。

改为手工安装 Homebrew bottle（macOS 15 → `arm64_sequoia` tag）：

1. 从 `https://formulae.brew.sh/api/formula/<name>.json` 取 bottle 的
   **真实 ghcr 路径与 sha256**（不要猜路径：`openssl@4` 实际在
   `homebrew/core/openssl/4`，而非 `homebrew/core/openssl@4`）。
2. 用 `ghcr.io/token?service=ghcr.io&scope=repository:<repo>:pull` 取匿名 token
   下载 blob，并逐字节校验 sha256。
3. 依赖：dav1d, lame, libvmaf, libvpx, openssl@4, opus, sdl2-compat, svt-av1,
   x264, x265, mpg123（mpg123 是 lame 的传递依赖，首轮漏装）。
4. bottle 内含 `@@HOMEBREW_CELLAR@@` / `@@HOMEBREW_PREFIX@@` 占位符，**必须**替换。

**踩坑记录（三条，均实际发生过）**：

- `install_name_tool -change` **无法匹配含 `@@` 的路径**：返回 exit 0 但静默不改。
- 替换串长度**必须与占位符完全相同**（19 字节）。用更短的串 + NUL 补齐会**提前截断
  C 字符串**，`otool -L` 只显示到第一个 NUL，得到 `/opt/homebrew/hbc` 这种垃圾路径。
  正确做法：`@@HOMEBREW_CELLAR@@`(19) → `/opt/homebrew/hbcde`(19)、
  `@@HOMEBREW_PREFIX@@`(19) → `/opt/homebrew/hbpre`(19)，再建同名符号链接。
- 改写 Mach-O 后**代码签名失效，macOS 会以 SIGKILL(137) 杀掉进程**，
  必须对每个 dylib 与可执行文件 `codesign --force --sign -`。

结果：

```
ffmpeg version 9.0.2
ffprobe version 9.0.2
/api/system/health -> {"ffmpeg":"ffmpeg","ffprobe":"ffprobe","ffmpeg_available":true}
```

---

## 四、端到端结果

驱动脚本：`backend/tools/e2e_drive.py`（本次新增的**测试工具**，非产品代码）。
它通过正式 REST API 驱动全链路，并强制两项断言：C1（原片字节不变）与 C6（失败可见）。

| 阶段 | 状态 | 统计 |
| --- | --- | --- |
| scan | `succeeded` | 6/6，failed=0 |
| detect | `succeeded` | 6/6，failed=0，**抽出 100 个候选片段** |
| package | `succeeded` | 1 个包，242 文件 / 7.0 MB，100 段 / 127 帧 / 7 张 contact sheet |
| proxies | `succeeded` | **total=0（见第五节说明）** |
| roughcut | `succeeded` | 12/12 shots，failed=0 |

**粗剪产物实测**（ffprobe，非"文件存在"）：

```
roughcut-abd6948f56a44d34.mp4
  h264 1280×720 + aac, duration=38.33s, size=22,417,676 B
  HTTP GET /api/projects/<id>/roughcut -> 200, 22,417,676 B
```

代表帧为有效 640×360 JPEG（127 张），说明 4K HEVC 确实解码成功。

### C1 原片只读 —— 通过

对 6 个原片在做前后比对 **SHA-256 / 大小 / mtime**：

```
c1_originals_unchanged: true
c1_problems: []
```

无修改、无新增、无删除。原片 mtime 仍为 2026-10-02/03（素材原始时间），未被触碰。

### C5 隐私与令牌边界 —— 通过

| 检查 | 期望 | 实测 |
| --- | --- | --- |
| 无 token | 401 | 401 |
| 错 token | 401 | 401 |
| 正确 token | 200 | 200 |
| 非回环 Host | 403 | 403 |
| 恶意 Origin | 403 | 403 |
| API 泄露 token | 否 | 否（0 次出现） |

分析包隐私声称**逐项复核为真**（不是只读 manifest 的自述）：

- 包内绝对路径出现次数：**0**（全目录 `grep -rc`，含 scripts 与二进制）
- 127 张帧的 EXIF：**0 张含 EXIF**
- 原片确实含 `creation_time` 标签 → 说明"有可去除的元数据"这一前提成立

---

## 五、发现的问题

### P1（需要你确认，非缺陷）proxies 产 0 个预览

`proxies` 任务只处理 **`ReviewRequest` 且 `status='pending'`** 的记录
（`story_planner.py:156`），而不是直接对片段编码。全新项目在"人工复核"这一步之前
没有任何 ReviewRequest，因此 `total=0` 是**设计如此**，且它如实报告了 0，
没有伪装成成功产生了文件（符合 C6）。

**影响**：UI 上"生成代理"按钮在未导入复核请求时看起来"什么都没做"。
**未做**：我没有跑完整的人工复核回路（导入 review requests → 再跑 proxies），
所以**代理编码路径在真实素材上未被本记录覆盖**。

### P2（文档漂移，AGENTS.md §3.5）

`docs/validation/v2.0.7/local-backend.txt` 记录 **469 passed**，
本次实测 **513 passed**。文档数字与实际不一致（正是 §3.5 警告的问题）。

### P3（环境差异，非代码问题）

AGENTS.md 假设 `backend/.venv`(3.13.7) 与项目自带 `bin/ffmpeg/`；
本机两者皆无。任何按 AGENTS.md 4.2/4.4 直接执行命令的人都会失败。

---

## 六、自动化验证结果

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `pytest -q` | **513 passed, 0 failed, 0 errors, 44 skipped** |
| 媒体作业（禁静默 skip） | `TSB_REQUIRE_FFMPEG=1 pytest test_ffmpeg_integration.py test_cancel_lifecycle.py` | **30 passed**（真实解码确实执行） |
| 前端 typecheck | `tsc --noEmit` | 通过 |
| 前端 lint | `eslint .` | 通过 |
| 前端 test | `vitest run` | **21 passed** |

44 个 skip 的 `-rs` 原因全部为平台事实类（`requires Windows` /
`long-path behaviour is Windows-specific` / `MAX_PATH only constrains Windows`），
无静默失败。后端全绿与本次真实素材手动跑通**互相印证**。

---

## 七、范围声明

**未改动**：产品代码一行未改。
**未删除或改写**：任何原片、任何既有测试、任何文档数字。
**新增**：仅 `backend/tools/e2e_drive.py`（测试驱动工具）与本记录。

---

## 八、尚未完成（诚实列出）

1. **Windows 实机门禁未执行** —— 本记录为 macOS，`LongPathsEnabled=0` 场景未测。
   结论措辞仍应为「代码级整改通过，发布门禁未完成」。
2. **人工复核回路未跑** —— 未导入 review requests，因此 proxies 编码路径、
   复核处置（disposition）、计划引用稳定性均**未**在真实素材上验证。
3. **取消/并发边界未测** —— 取消时延、进程树回收、409 互斥、清理失败可见性
   属 TSB 专项，本轮未覆盖。
4. **素材维度不全** —— 无竖屏、无无音轨片段、无 HDR；6 段而非要求的 8～10 段。
5. **60+ 段 / 200GB 全量性能未测** —— 本轮 4.4 GB。
6. **P1/P2/P3 未修复** —— 仅记录，未提交改动。
