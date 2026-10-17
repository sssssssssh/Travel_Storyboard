# M5.3-R3 验证证据

**终态 commit：** `main@46c720f`　**审阅基线：** `main@65fd605`
**机器（本机测量）：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**CI：** [run 39009705787](https://github.com/sssssssssh/Travel_Storyboard/actions/runs/39009705787) — **7/7 success**

本目录保存 R3 的**原始 junitxml 与 CI 运行记录**，不是转录数字。
发布记录中的每个计数都可在此复核。

---

## 一、文件清单

| 文件 | 内容 | 计数 |
| --- | --- | --- |
| `backend-full-46c720f.xml` | 后端全量（本机，含 `backend/.env`） | tests=1266 failures=0 errors=0 skipped=44 |
| `backend-full-CI-conditions-no-env.xml` | 后端全量，**移走 `backend/.env`**（CI 真实条件） | tests=1266 failures=0 errors=0 skipped=44 |
| `ci-run-39009705787.json` | GitHub Actions 运行记录与作业明细 | 7 jobs / 7 success |

复核命令（`cd backend`）：

```bash
../.venv/bin/python -m pytest -q --junitxml=out.xml
../.venv/bin/python - <<'PY'
import xml.etree.ElementTree as ET
r = ET.parse('out.xml').getroot()
s = r.find('testsuite') if r.find('testsuite') is not None else r
print('tests=%s failures=%s errors=%s skipped=%s' % (
    s.get('tests'), s.get('failures'), s.get('errors'), s.get('skipped')))
PY
```

> 计数口径：`tests` 含 skip，`passed` 不含。**不要**把 `tests` 报成 "N passed"。

---

## 二、TSB-349：Ubuntu CI 真实失败与跨平台验证

**上一轮的失败（run `38993163807` @ `65fd605`）：** 7 作业中 5 成功 2 失败。

```
Ubuntu Backend (Python 3.11): 1171 passed / 1 failed / 44 skipped
Ubuntu Backend (Python 3.13): 1171 passed / 1 failed / 44 skipped
失败用例: tests/test_staging_leaf_substitution.py::
          test_v18_a_replaced_leaf_directory_is_refused
```

**根因。** 测试执行 `rmtree(stage)` 后 `mkdir(stage)`，在 ext4/overlayfs 上
**立即复用**刚释放的 inode，于是 `_identity()` 返回与登记时完全相同的
`(st_dev, st_ino)`。守卫只比较 dev/ino，遂认定"还是我们那个目录"，把替换目录里的
`someone_elses.bin` 删除。

**macOS（APFS）不复用 inode，因此该缺陷在开发机上永远测不出来。**
这是"仅本机通过不能作为验收"的直接例证，也正是 R2 记录中 G2 无法回填的根因。

**本轮 CI 结果（run `39009705787` @ `46c720f`）：**

| 作业 | 上轮 | 本轮 |
| --- | --- | --- |
| Backend (Python 3.11) — ubuntu-latest | ❌ failed | ✅ **success** |
| Backend (Python 3.13) — ubuntu-latest | ❌ failed | ✅ **success** |
| Backend (Python 3.11) — windows-latest | ✅ | ✅ success |
| Backend (Python 3.13) — windows-latest | ✅ | ✅ success |
| Required FFmpeg (ubuntu-latest) | ✅ | ✅ success |
| Required FFmpeg (windows-latest) | ✅ | ✅ success |
| Frontend | ✅ | ✅ success |

已确认 `46c720f` 同时包含 `_verify_generation` 实现与
`test_v21_reused_inode_identity_cannot_authorise_deletion` 用例，且 `backend`
作业跑**全量套件**（未忽略该文件），因此 Ubuntu 的通过是**对该修复的真实
跨平台验证**。

---

## 三、TSB-345 确定性复现与两种伪造向量

**复现方法（不依赖 inode 复用是否恰好发生）。**
`rmtree` + `mkdir` 之后，把登记身份改写成与替换目录一致，即可在**任意平台**
确定性触发 Linux 的条件。

修复前实测：

```
recorded identity forced to live identity (16777231, 11217751)
(this is exactly what Linux inode reuse produces)
result.ok=True
someone_elses.bin survived: False     <- 文件被删除
```

修复后：

```
result.ok=False  error_code=STAGING_NOT_OWNED
someone_elses.bin survived: True
VERDICT: SAFE
```

**两种伪造向量均被关闭：**

| 向量 | 结果 |
| --- | --- |
| 叶子 inode 被复用（身份强制相等） | `ok=False`、`STAGING_NOT_OWNED`、文件**存活** |
| 攻击者自建同名标记文件（猜 nonce） | 拒绝，受害者文件**存活**（64 hex 不可猜） |

**所有权不再依赖 inode：** `inspect.getsource(_verify_generation)` 不引用
`st_ino`/`dev` —— 判定只依据创建事件写入的 nonce。

---

## 四、反向验证记录

"先复现、再转绿"的每一步都做了反向验证：**临时把修复去掉，确认对应用例转红，
再恢复**，以排除"空断言式的绿"。

| 任务 | 反向操作 | 期望 | 实测 |
| --- | --- | --- | --- |
| TSB-345 | 身份强制相等（模拟 inode 复用） | 拒绝删除 | ✅ 修复前删除了文件；修复后拒绝 |
| TSB-346 | 恢复 `Path(stage).exists()` | V37d 红 | ✅ 1 红 |
| TSB-347 | 恢复覆盖式 `derived_seconds` | V38f/V38g 红 | ✅ 2 红 |
| TSB-347 | 移除 Provider 的 `guard` 调用 | V38d 红 | ✅ 1 红 |
| TSB-348 | 回退改回"记录即返回" | V39f 红 | ✅ 1 红 |

全部恢复后套件全绿。

---

## 五、凭据与端点安全

* 全仓库 `git grep` 该 token ⇒ **0 次命中**。
* `backend/.env` 由 `.gitignore:23` 忽略；CI 无该文件（见
  `backend-full-CI-conditions-no-env.xml` 与本机 XML 计数完全一致）。
* CI 未注入任何真实 Key；`ci.yml` 不导出全局 `TRAVELSB_MODEL_MODE`。
* 代际 nonce **不进入**任何公开面：`V21e` 断言结果对象、message、path、
  error_code 与清理回执均不含该 nonce。

---

## 六、未完成事项（与发布记录一致）

1. **G5 真实端点**：`blocked_by_endpoint` —— 端点全线 HTTP 503
   `no healthy upstream`（文本/图片/视频一律），属外部部署状态，
   **不构成**视频能力通过或不通过的证据。
2. **G6 真实 A/B（20–30 镜头）**：`not_evaluated`，依赖 G5。
3. **Windows 实机手工确认**：CI 的 Windows 作业通过，但未做真机人工走查。
4. **`test_vision_commit_races.py` Ubuntu 间歇竞争**：本批 CI 全绿、未复现，
   因此**未** xfail/skip；保留为观察项。

**在 G5、G6 完成前不得声称**「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。
