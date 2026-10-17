# M5.3-R2 验证证据

**终态 commit：** `main@544eee6`　**审阅基线：** `main@8c16eae`
**机器：** macOS 15.7.7 arm64 / Python 3.12.14 / FFmpeg 9.0.2（Homebrew）
**记录日期：** 2026-10-17

本目录保存 R2 的**原始 junitxml**，不是转录数字。发布记录中的每个计数都可在此
复核。

---

## 一、文件清单

| 文件 | 内容 | 计数 |
| --- | --- | --- |
| `backend-full-544eee6.xml` | 后端全量（本机，含 `backend/.env`） | tests=1237 failures=0 errors=0 skipped=44 |
| `backend-full-CI-conditions-no-env.xml` | 后端全量，**移走 `backend/.env`**（CI 真实条件） | tests=1237 failures=0 errors=0 skipped=44 |
| `media-job-544eee6.xml` | media 作业模拟（`TSB_REQUIRE_FFMPEG=1`，65 文件清单） | tests=1153 failures=0 errors=0 skipped=44 |
| `tsb342-before-fix-RED.xml` | TSB-342 新套件**修复前**的运行记录 | tests=34 failures=26 |

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

## 二、"移走 `.env`" 是 CI 的真实条件

`SettingsConfigDict(env_file=".env")` 相对于**当前工作目录**解析，因此只有在
`backend/` 下运行时才会读到 `backend/.env`。开发机上该文件存在并提供了
`TRAVELSB_MODEL_BASE_URL` 与 `TRAVELSB_MODEL_API_KEY`；**CI 上不存在该文件**。

这正是 TSB-343 的根因：只设置 `model_mode="on"` 的 fixture 在 CI 上会因缺少
URL/Key 而让 `authorize_upload()` 以 `MODEL_NOT_CONFIGURED` 提前拒绝，把断言
期望的 `MODEL_UPLOAD_DENIED` 抢先覆盖。

复现方式（本目录两个全量 XML 的差异即来源于此）：

```bash
cd backend
mv .env /tmp/env_hidden
../.venv/bin/python -m pytest -q --junitxml=/tmp/noenv.xml     # 改前会红
mv /tmp/env_hidden .env                                        # 记得还原
```

改前结果：`test_native_video_provider.py` + `test_video_model_switch_scope.py`
合计 **35 tests / 17 failures**，典型断言：

```
assert 'MODEL_NOT_CONFIGURED' == 'MODEL_UPLOAD_DENIED'
```

改后（`apply_mock_model_contract()`）：同上条件 **0 failures**；全量
1237/0/0/44 与含 `.env` 时完全一致 —— 即测试结果不再依赖开发机环境。

---

## 三、逐项反向验证记录

"先复现、再转绿"的每一步都做了反向验证：**临时把修复去掉，确认对应用例转红，
再恢复**。这样可排除"空断言式的绿"。

| 任务 | 反向操作 | 期望转红 | 实测 |
| --- | --- | --- | --- |
| TSB-342 | 提示词写回 `Example: {"order": ["red","green","blue"]}` | V01b | ✅ 1 红 |
| TSB-342 | 恢复 motion-only 回退 | V04 / V03 / V06 | ✅ 3 红 |
| TSB-343 | 移走 `backend/.env` | 17 项 | ✅ 17 红（改前） |
| TSB-344 | `_is_link()` 改为恒 `False` | 链接类用例 | ✅ 6 红（含 V21） |
| TSB-344 | 删除 `_validate_chain` 身份比对 | V18d | ✅ 红，且清理**错误删除了文件** |
| TSB-337 | 恢复 `phase="complete", total=0` | V23 / V23b / V24 | ✅ 3 红 |
| TSB-338 | 删除发送点的 `note_attempt` | W5 | ✅ 1 红 |
| TSB-339 | 恢复单字段 `_redact_report_for_commit` | V30b/c/d | ✅ 3 红 |

---

## 四、TSB-340 由端到端测试暴露的真实缺陷

端到端测试的价值在于它**确实抓到了三个只有全链路才暴露的集成缺陷**，
而非仅仅"跑通一遍"：

1. **`CLIP_FAILED` 从未定义** —— `video_job.py` 在推导失败分支引用它，
   `errors.py` 无此常量，触发时抛 `NameError`。与 R1 修过的 `MODEL_FAILED`
   同类：稳定错误码只在**使用点**出现，从未在边界声明。
2. **暂存叶子被重复创建** —— TSB-336 的预登记与 `derive_clip` 自身的
   `create_owned_staging`（以 `exist_ok=False` 作所有权证明）冲突，
   实测 `FileExistsError: File exists: '<segment_id>'`，job 直接 failed。
3. **预登记只登记路径、没真的建目录** —— 失败时清理器被指向从不存在的 stage，
   实测 `cleanup_detail` 记 `"stage was never created"`、`removed_files=0`。

修复前后的 job stats 对照（同一测试）：

```
# 改前（第 3 个缺陷）
'cleanup_detail': {'attempted': 1, 'ok': 1, 'results': [
    {'path_kind': 'video_staging', 'ok': True, 'bytes': 0,
     'note': 'stage was never created'}]}
'usage': {'calls': 0, 'clip_bytes': 0}          # 一个字节都没外发

# 改后
Mock 服务器实际收到含 video 部分的请求体；
SegmentVideoObservation 落库；cleanup.failed == 0；
原片 sha256 与 (size, mtime_ns, inode) 均未变。
```

---

## 五、TSB-342 修复前 RED 证据摘录

来自 `tsb342-before-fix-RED.xml`（26 failures / 34 tests）：

```
LEAKS the true order? True
  build_probe_prompt() 含 ["red", "green", "blue"]，与 PROBE_PHASE_ORDER 相同

'There is motion in this clip.'        ⇒ FALSE POSITIVE
    basis: "describes motion (order not established)"
'things are changing over time'        ⇒ FALSE POSITIVE
'{"order": ["red","green","blue"]}'    ⇒ FALSE POSITIVE
    basis: "reported the clip's colour order correctly"   ← 从提示词抄来的
'The order is blue, then green, then red.' ⇒ 正确拒绝
```

即：重复提示词自身的示例即可解锁 `verified_native_video` ⇒ 上传通道。

---

## 六、凭据与端点安全

* 全仓库 `git grep` 该 token ⇒ **0 次命中**。
* 端点主机仅以**截断形式**出现在 `docs/RELEASE_M5_3_R1.md:407`
  （`http://1643210566335864.../glm_53_flash_video_test_wzy/v1`），非凭据。
* `backend/.env` 由 `.gitignore:23` 忽略（`git check-ignore` 已确认）。
* 本批新增的 `--redact` 会清洗端点主机、本机绝对路径与凭据形态串；
  实测 `pai-eas.aliyuncs.com` 与 `1643210566335864` 在 redacted 报告中均搜不到。
* CI 未注入任何真实 Key；`ci.yml` 不导出全局 `TRAVELSB_MODEL_MODE`。

---

## 七、未完成事项（与发布记录一致）

1. **G5 真实端点**：`blocked_by_endpoint` —— 端点全线 HTTP 503
   `no healthy upstream`（文本/图片/视频一律），属外部部署状态，
   **不构成**视频能力通过或不通过的证据。
2. **G6 真实 A/B**：`not_evaluated`，依赖 G5。
3. **G2「同 SHA CI 7/7」**：需在 GitHub Actions 上复核后回填发布记录；
   本目录提供的是本机对 media 清单与 skip 守卫的等价模拟。
4. **Windows 实机**：`--ascii`/console 修复按 cp1252/cp936 模拟验证，
   未在真实 Windows 控制台运行。
5. **`test_vision_commit_races.py` Ubuntu 间歇竞争**：本机 macOS 连续 5 次全绿，
   未在 Ubuntu 复现，因此**未** xfail/skip。

**在 G5、G6 完成前不得声称**「视频模型已真正看懂旅行视频」或
「视频输入比抽帧更好」。
