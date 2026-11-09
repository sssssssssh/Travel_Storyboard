# TSB-367-UAT-01 证据索引

基线：`a48429335e6457f5dd59d0943dd61cef9846a1a0`
发布记录：[../../RELEASE_TSB_367_UAT_01.md](../../RELEASE_TSB_367_UAT_01.md)

## 验收环境（真实）

| 项 | 值 |
|---|---|
| 浏览器 | Google Chrome **156.0.8078.80** |
| 驱动 | Playwright 1.44.1（`playwright-core`，装在**仓库外** `F:\tsb-uat-367-01`） |
| 服务 | FastAPI `127.0.0.1:9123`，托管 `frontend/dist` 生产构建 |
| 数据 | `F:\tsb-uat-367-01\run2\`（仓库外独立数据库） |
| 素材 | 2 个合成视频、5 个 Segment、3 个已保存镜头 |
| 离线 | 全程无外部模型调用，无上传 |

> **这不是 jsdom**：页面由真实 Chrome 渲染，网络请求由真实 HTTP 发出，
> 断言对象是页面 DOM + 响应状态 + 只读 API 快照三者。

## 截图（已脱敏，可提交）

| 文件 | 证明什么 |
|---|---|
| `raw/uat-01-initial.png` | 进入故事板页、版本识别、3 个已保存镜头 |
| `raw/uat-02-selection.png` | 指定镜头的「已选镜头」与「目标」标签一致 |
| `raw/uat-03-preview-up.png` | 上移提案的**服务端 diff**，确认前版本不变 |
| `raw/uat-03-applied-up.png` | 应用后 `applied` 回执 + 新版本号 + 顺序真的变了 |
| `raw/uat-03-boundaries.png` | 首项「上移」/末项「下移」按钮禁用 |
| `raw/uat-04-trim.png` | 裁剪 diff 显示前后 in/out |
| `raw/uat-05-locked-trim.png` | 锁定后裁剪被拒绝的可见提示 |
| `raw/uat-06-remove.png` | 移除后镜头离开当前方案 |
| `raw/uat-07-after-reselect.png` | 改选镜头后旧提案仍显示（可用性观察） |
| `raw/uat-08-dirty-draft.png` | 未保存草稿在刷新后保留 |
| `raw/uat-10-history.png` | 版本历史面板与恢复入口 |

脱敏确认：截图只含项目内 UUID、镜头 ID、版本号、合成文件名 `uat_clip_*.mp4`。
**不含**令牌、用户私人目录、密钥、代理端口。

## 仅本地、不提交（`.gitignore` 排除）

| 文件 | 内容 |
|---|---|
| `RED-f01.local.txt` | 修复前 5 failed 的完整输出 |
| `GREEN-f01.local.txt` | 修复后 6 passed |
| `REVERSE-VERIFY.local.txt` | 还原缺陷后再次 5 failed |
| `frontend-full.local.txt` | 前端全量 129 passed |
| `backend-full.local.txt` / `.xml` | 后端全量结果 |

## 复现方式

```powershell
# 1. 造隔离数据（仓库外）
$env:TRAVELSB_DATA_DIR = "F:\tsb-uat-367-01\run2\data"
python F:\tsb-uat-367-01\setup_uat_project.py F:\tsb-uat-367-01\run2

# 2. 生产构建 + 启动
cd frontend; pnpm run build
cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 9123

# 3. 真实浏览器驱动
cd F:\tsb-uat-367-01
node uat.js      # UAT-01..03
node uat3.js     # UAT-04..07
node uat4.js     # UAT-05/08/09/11（数据安全重点）
node uat5.js     # UAT-10/12
node uat7.js     # UAT-07 提案绑定专项
```

## 关键结论速查

| 结论 | 证据 |
|---|---|
| UAT-F01 缺陷真实存在 | RED：5 failed，失败类别恰好是锚点反向 + 边界未禁用 |
| 修复有效 | GREEN 6 passed + 真实浏览器 `[B,A,C]` + 版本 +1 |
| 测试非空断言 | 反向验证：还原缺陷 → 5 failed（集合与 RED 一致） |
| 旧提案不会错对象 | 改选后应用，**只有原提案镜头被改** |
| 409 不覆盖草稿 | tab A 提案 v21 → tab B 应用 v22 → tab A apply 被拒，版本仍 v22 |
| 源片未被修改 | 两个合成片 SHA-256 前后一致 |
