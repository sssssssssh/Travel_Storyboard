# v2.1.3 拉取与 macOS 环境适配记录

**日期**：2026-10-15
**拉取**：`664239b` → `6649b6f`（11 个 commit，fast-forward）
**机器**：macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**结论**：**代码可运行、无产品缺陷**。本机全量 `1096 collected / 12 failed`，
12 项**全部源于 ffmpeg 构建缺 `drawtext`**（排除该文件后
`1058 collected / 0 failed`）；另有 15 项曾被 `.env` 的 `MODEL_MODE=off` 掩盖，
置 `on` 后全通过。两者都是**环境差异**，非回归。

---

## 一、拉取结果

```
git pull --ff-only origin main
664239b..6649b6f   11 commits, 46 files, +11192 / -8
```

本地领先 0、落后 11，工作区干净，**无冲突**。

新增内容为 v2.1.3 原生视频理解（TSB-325…332）：视频能力契约与适配层、
时间事件 Schema、`video_analyze` 任务、A/B 评价脚手架、前端理解面板等。

验证拉取健康：

| 检查 | 结果 |
| --- | --- |
| `import app` | OK（version 2.1.2） |
| 依赖变化 | **无**（requirements 未改，无需重装） |
| 前端 `vitest run` | **65 passed**（与发布记录一致） |
| 早前 macOS 验收文件 | 全部仍在（`e2e_drive.py`、`ai_review.py`、v2.0.7 三份记录） |

---

## 二、本机失败与根因（两项，均为环境）

### 失败项 1：`test_video_clip.py` 12 项 —— ffmpeg 缺 `drawtext`

**现象**：

```
[AVFilterGraph] No such filter: 'drawtext'
Error opening output file .../rot.mp4
FFmpegError: Command failed (8)
```

**根因**：该套件的 `_make_source()` 用
`drawtext=text='%{eif\:t\:d}'...` 合成测试源，而 **drawtext 需要 ffmpeg 编译时
带 `libfreetype`**。

实测：

```
/opt/homebrew/bin/ffmpeg -filters | grep -c drawtext   ->  0
ffmpeg -version | grep enable-libfreetype              ->  (缺失)
```

**关键事实**：Homebrew 的 ffmpeg formula **依赖列表里没有 freetype**
（`dav1d, lame, libvmaf, libvpx, openssl@4, opus, sdl2-compat, svt-av1, x264, x265, xz`），
因此**任何 Homebrew 装的 ffmpeg 9.0.2 都没有 drawtext**。

对比 CI：media 作业用 **Ubuntu apt 的 ffmpeg**（Debian/Ubuntu 构建带
`--enable-libfreetype`）或 Windows 项目自带件，二者都有 drawtext，
所以发布记录里的 `457 passed / 0 failed / 0 skipped` 在**那些**环境成立。

**这不是产品缺陷**：`drawtext` 在 `backend/app/` 中**零引用**，
仅在 `tests/test_video_clip.py` 里用于合成夹具。

**影响面**：本机全量 `1096 collected / 12 failed`，12 项全部集中在此文件，
其余 1084 项全绿。逐项核验：12 项失败对应 **36 次**
`No such filter: 'drawtext'`（夹具每用例被调用多次），
**无一项因其他原因失败**。

> **反证**：CI media 作业清单（54 个文件）在本机原样跑，
> 结果为 `1012 collected / 12 failed / 44 skipped`，
> 12 项失败**同样全部**是 `test_video_clip.py`——即该文件在
> "不许静默 skip" 的强制模式下也一致地失败于同一个缺失能力。

### 失败项 2（已解决）：15 项模型测试 —— `.env` 的 `MODEL_MODE=off`

**现象**：

```
AssertionError: assert 'MODEL_UPLOAD_DENIED' == 'MODEL_CAPABILITY_UNSUPPORTED'
MODEL_UPLOAD_DENIED: 全局开关未开启（TRAVELSB_MODEL_MODE=off）
```

**根因**：本机 `backend/.env` 设 `TRAVELSB_MODEL_MODE=off`，
而 `test_native_video_provider.py` 期望走到"能力不支持"分支，
被更早触发的"全局开关关闭"抢先拒绝。

**验证**：

```
TRAVELSB_MODEL_MODE=on  pytest tests/test_native_video_provider.py  ->  30 passed
```

**结论**：这是**配置差异**，不是代码问题。默认 `off` 是产品的隐私默认值
（C5：默认不外发画面），**不应**为了跑测试而改产品默认；
跑这些套件时显式置 `on` 即可。

> 注：`.env` 已被 `.gitignore` 忽略，其私钥与端点**未**出现在本记录，也未随代码提交。

---

## 三、运行末尾噪声（已存在，非本轮引入）

全量运行末尾偶发：

```
app.services.runner.StateConvergenceError: <job_id>: unresolved state pending
RuntimeError: cannot schedule new futures after interpreter shutdown
```

**均不计入失败**：两次全量运行 junit 均为 `failures=12`，
且 12 项全部是上述 drawtext 用例，`non-clip failures = []`。

该现象**已在 `docs/RELEASE_V2_1_3.md:220-239` 记录并定位**
（`staging_cleanup._OWNERS` / `_CLEANED` 为进程级全局，跨用例累积），
故本记录不重复计为新发现。

---

## 四、复现本机绿的方式

```bash
cd backend

# 1) 只缺 drawtext 时，这样跑应全绿（其余全部用例）
TRAVELSB_MODEL_MODE=on ../.venv/bin/python -m pytest -q --ignore=tests/test_video_clip.py
```

# 2) 原样复现 CI media 作业（清单直接从 ci.yml 提取，不依赖任何临时文件）：
FILES=$(python3 - <<'PY'
import re
y = open('../.github/workflows/ci.yml').read()
blk = re.search(r'\n  media:\n(.*?)(?=\n  [a-zA-Z_]+:\n|\Z)', y, re.S).group(1)
print(' '.join(sorted(set(re.findall(r'tests/[a-zA-Z_0-9]+\.py', blk)))))
PY
)
TSB_REQUIRE_FFMPEG=1 TRAVELSB_MODEL_MODE=on \
  ../.venv/bin/python -m pytest -q $FILES
```

第 1 条在本机实测：**`1058 collected / 0 failed / 0 errors / 44 skipped`**
（即排除该文件后**全绿**，反证 12 项失败确实只源于这一个缺失能力）。

第 2 条（CI media 作业原样）在本机实测：
`1012 collected / 12 failed / 44 skipped`，12 项失败全部集中在
`test_video_clip.py`。

**要在 macOS 上跑通 `test_video_clip.py`**，需要一个带 `libfreetype` 的 ffmpeg：

- 方案 A：`brew install ffmpeg --with-freetype`（该选项在新版 formula 已移除，多半无效）；
- 方案 B：用 `conda`/`static builds` 里带 drawtext 的构建；
- 方案 C：改用项目自带的 Windows 版 ffmpeg（**不适用于 macOS**）。

> 本轮**未**为解决此问题改动产品代码或测试——因为
> "本机 ffmpeg 缺能力"不等于"测试写错了"。是否需要为缺失能力加
> `skipif` 守卫，属于**产品决策**，已列为待确认项（见第六节）。

---

## 五、范围声明

- **未改动**任何产品代码、测试、CI 配置。
- **未删除或改写**任何文件；本次拉取为纯 fast-forward。
- **未提交**任何内容（本记录为工作树新增文件）。
- 未修改 `backend/.env`。

---

## 六、待确认 / 尚未完成

1. **是否为 `drawtext` 缺失加能力守卫** —— 需产品决策。
   若加 `skipif(no drawtext)`，则须同步登记到
   `conftest._EXPECTED_SKIP_REASONS`（AGENTS.md §4.5 规则 1），
   否则在 `TSB_REQUIRE_FFMPEG=1` 下会被判为意外 skip 而使作业失败。
   若不改，则**任何 Homebrew-macOS 环境跑全量都会看到这 12 项失败**，
   容易被误报为回归。
2. **AGENTS.md 前端用例数过期** —— 第 138 行写 `当前 48 passed`，
   实测与发布记录均为 **65 passed**（§3.5 要求数字一致）。
3. v2.1.3 的 Windows 实机验收门禁与真实素材端到端**仍未执行**
   （见 `docs/RELEASE_V2_1_3.md` 与 `docs/validation/v2.1.3/`）。
   结论措辞仍应为**「代码级整改通过，发布门禁未完成」**。
