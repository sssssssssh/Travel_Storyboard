import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, downloadUrl, endpoints } from '../lib/api'
import type { ImportResult, JobOut, ReviewPackageOut, ReviewRequestOut } from '../lib/types'
import { useJob } from '../lib/useJob'
import { Badge, Button, Card, Field, inputClass } from '../components/ui'

const priorityTone: Record<string, string> = { high: 'red', medium: 'yellow', low: 'slate' }

export default function AIDirector() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const [rating, setRating] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [importText, setImportText] = useState('')
  const [importResult, setImportResult] = useState<ImportResult | null>(null)
  const [analyzeResult, setAnalyzeResult] = useState<string | null>(null)
  const [jobKind, setJobKind] = useState('')

  const job = useJob(jobId)

  const packages = useQuery({
    queryKey: ['packages', id],
    queryFn: () => api<ReviewPackageOut[]>(endpoints.packages(id)),
  })

  const requests = useQuery({
    queryKey: ['review-requests', id],
    queryFn: () => api<ReviewRequestOut[]>(endpoints.reviewRequests(id)),
  })

  function startJob(type: string, body?: Record<string, unknown>) {
    setJobKind(type)
    return api<JobOut>(endpoints.createJob(id, type), {
      method: 'POST',
      body: body ? JSON.stringify(body) : undefined,
    })
  }

  const buildPackage = useMutation({
    mutationFn: () => startJob('package', { options: { rating } }),
    onSuccess: (j) => setJobId(j.id),
  })

  const buildProxies = useMutation({
    mutationFn: () => api<JobOut>(endpoints.buildProxies(id), { method: 'POST', body: JSON.stringify({ options: {} }) }),
    onSuccess: (j) => setJobId(j.id),
  })

  const importReq = useMutation({
    mutationFn: () =>
      api<ImportResult>(endpoints.importReviewRequests(id), {
        method: 'POST',
        body: JSON.stringify(JSON.parse(importText)),
      }),
    onSuccess: (r) => {
      setImportResult(r)
      qc.invalidateQueries({ queryKey: ['review-requests', id] })
    },
  })

  const analyze = useMutation({
    mutationFn: () => api<Record<string, unknown>>(endpoints.analyzeStory(id), { method: 'POST' }),
    onSuccess: (r) => setAnalyzeResult(JSON.stringify(r, null, 2)),
  })

  const jobDone = job.data && job.data.status !== 'pending' && job.data.status !== 'running'

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="mb-6 flex items-center gap-3">
        <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">← 返回项目</Link>
        <h1 className="text-2xl font-semibold text-white">AI 导演</h1>
      </div>

      {job.data && (
        <Card className="mb-6">
          <div className="flex items-center gap-3 text-sm">
            <Badge tone={job.data.status === 'succeeded' ? 'green' : job.data.status === 'failed' ? 'red' : 'blue'}>
              {job.data.status}
            </Badge>
            <span className="text-slate-300">
              {jobKind === 'package' ? '分析包' : '低清预览'} {job.data.stats.done ?? 0}/{job.data.stats.total ?? 0}
            </span>
            {job.data.error && <span className="truncate text-red-400">{job.data.error}</span>}
            {jobDone && (
              <Button variant="ghost" onClick={() => { setJobId(null); qc.invalidateQueries() }}>关闭</Button>
            )}
          </div>
        </Card>
      )}

      {/* Round 1: offline package */}
      <Card className="mb-6">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">第一轮 · 生成脱敏分析包</h2>
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-44">
            <Field label="筛选评分">
              <select className={inputClass} value={rating} onChange={(e) => setRating(e.target.value)}>
                <option value="">全部（排除🚫）</option>
                <option value="like">👍 推荐</option>
                <option value="favorite">⭐ 收藏</option>
              </select>
            </Field>
          </div>
          <Button disabled={buildPackage.isPending} onClick={() => buildPackage.mutate()}>
            生成分析包
          </Button>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          包内已去除绝对路径、帧 EXIF/GPS 已剥离，导出 zip 后可上传给 ChatGPT 分析。
        </p>
        {packages.data && packages.data.length > 0 && (
          <ul className="mt-3 divide-y divide-ink-800">
            {packages.data.map((p) => (
              <li key={p.id} className="flex items-center justify-between py-2 text-sm">
                <span className="text-slate-300">{p.version} · {p.file_count} 文件</span>
                <a className="text-accent hover:underline" href={downloadUrl(endpoints.packageDownload(p.id))}>下载 zip</a>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Round 2: import review requests */}
      <Card className="mb-6">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">第二轮 · 导入 AI 复核请求</h2>
        <textarea
          className={`${inputClass} min-h-[140px] font-mono text-xs`}
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          placeholder='{"schema_version":"2.0","requests":[{"asset_id":"...","segment_id":"...","start_sec":1.0,"end_sec":8.0,"priority":"high","reason":"..."}]}'
        />
        <Button className="mt-2" disabled={importReq.isPending || !importText.trim()} onClick={() => importReq.mutate()}>
          导入并校验
        </Button>
        {importReq.isError && <div className="mt-2 text-sm text-red-400">JSON 解析失败：{importReq.error instanceof Error ? importReq.error.message : ''}</div>}
        {importResult && (
          <div className="mt-2 text-sm">
            <span className="text-emerald-300">成功 {importResult.created} 条</span>
            {importResult.errors.length > 0 && (
              <ul className="mt-1 space-y-1 text-red-400">
                {importResult.errors.map((e) => <li key={e.index}>第 {e.index} 条：{e.message}</li>)}
              </ul>
            )}
          </div>
        )}
      </Card>

      {/* Round 2: proxies */}
      <Card className="mb-6">
        <h2 className="mb-3 text-sm font-semibold text-slate-200">复核预览</h2>
        <Button variant="ghost" disabled={buildProxies.isPending} onClick={() => buildProxies.mutate()}>
          生成低清预览（按优先级）
        </Button>
        {requests.data && requests.data.length > 0 && (
          <ul className="mt-3 divide-y divide-ink-800">
            {requests.data.map((r) => (
              <li key={r.id} className="flex items-center gap-3 py-2 text-sm">
                <Badge tone={priorityTone[r.priority] ?? 'slate'}>{r.priority}</Badge>
                <span className="text-slate-300">{r.start_sec.toFixed(1)}s–{r.end_sec.toFixed(1)}s</span>
                <span className="truncate text-slate-500">{r.reason}</span>
                <Badge tone={r.status === 'proxied' ? 'green' : r.status === 'failed' ? 'red' : 'slate'}>{r.status}</Badge>
                {r.proxy_ref && (
                  <a className="text-accent hover:underline" href={downloadUrl(`/api/media/file?path=${encodeURIComponent(r.proxy_ref)}`)}>预览</a>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* Optional direct model */}
      <Card>
        <h2 className="mb-3 text-sm font-semibold text-slate-200">直连模型（可选）</h2>
        <Button variant="ghost" disabled={analyze.isPending} onClick={() => analyze.mutate()}>
          调用视觉模型分析
        </Button>
        {analyzeResult && (
          <pre className="mt-3 overflow-auto rounded bg-ink-800 p-3 text-xs text-slate-300">{analyzeResult}</pre>
        )}
      </Card>
    </div>
  )
}
