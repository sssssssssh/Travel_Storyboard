import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, downloadUrl, endpoints } from '../lib/api'
import type {
  DirectAnalysisResult,
  ImportResult,
  JobOut,
  ReviewPackageOut,
  ReviewRequestOut,
} from '../lib/types'
import { useJob } from '../lib/useJob'
import { Badge, Button, Card, Field, inputClass } from '../components/ui'

import ReviewRequestCard from '../components/ReviewRequestCard'

export default function AIDirector() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const [rating, setRating] = useState('')
  const [jobId, setJobId] = useState<string | null>(null)
  const [importText, setImportText] = useState('')
  const [importResult, setImportResult] = useState<ImportResult | null>(null)
  const [jobKind, setJobKind] = useState('')

  const job = useJob(jobId)
  useEffect(() => {
    if (job.data && job.data.status !== 'running' && job.data.status !== 'pending') {
      qc.invalidateQueries({ queryKey: ['review-requests', id] })
      qc.invalidateQueries({ queryKey: ['packages', id] })
    }
  }, [job.data?.status, job.data, id, qc])

  const packages = useQuery({
    queryKey: ['packages', id],
    queryFn: () => api<ReviewPackageOut[]>(endpoints.packages(id)),
  })

  const requests = useQuery({
    queryKey: ['review-requests', id],
    queryFn: () => api<ReviewRequestOut[]>(endpoints.reviewRequests(id)),
    refetchInterval: job.data?.status === 'running' || job.data?.status === 'pending' ? 1500 : false,
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
    mutationFn: () =>
      api<DirectAnalysisResult>(endpoints.analyzeStory(id), { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['review-requests', id] }),
  })

  const jobDone = job.data && job.data.status !== 'pending' && job.data.status !== 'running'
  const fjobs = job.data?.failures?.items ?? []

  return (
    <div className="mx-auto max-w-5xl px-6 py-8">
      <div className="mb-6 flex items-center gap-3">
        <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">← 返回项目</Link>
        <h1 className="text-2xl font-semibold text-white">AI 导演</h1>
      </div>

      {job.data && (
        <Card className="mb-6">
          <div className="flex items-center gap-3 text-sm">
            <Badge
              tone={
                job.data.status === 'succeeded'
                  ? 'green'
                  : job.data.status === 'partial_failed'
                    ? 'yellow'
                    : job.data.status === 'failed'
                      ? 'red'
                      : 'blue'
              }
            >
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
          {fjobs.length > 0 && (
            <ul className="mt-2 max-h-40 space-y-1 overflow-auto text-xs text-amber-300">
              {fjobs.map((f, i) => (
                <li key={`${f.ref}-${i}`} className="truncate" title={f.message}>
                  [{f.kind}] {f.message}
                </li>
              ))}
            </ul>
          )}
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
            <span className="text-emerald-300">成功 {importResult.created} 条 · 已存在 {importResult.duplicates?.length ?? 0} 条</span>
            {importResult.errors.length > 0 && (
              <ul className="mt-1 space-y-1 text-red-400">
                {importResult.errors.map((e) => <li key={e.index}>第 {e.request_index} 条 · {e.segment_id} [{e.error_code}]：{e.message}</li>)}
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
        {(buildProxies.error || buildPackage.error) && <p className="mt-2 text-red-400">{(buildProxies.error || buildPackage.error)?.message}</p>}
        {requests.error && <p className="mt-2 text-red-400">{requests.error.message}</p>}
        {requests.data && requests.data.length > 0 && (
          <div className="mt-3 space-y-3">
            {requests.data.map((r) => <ReviewRequestCard key={r.id} projectId={id} request={r} />)}
          </div>
        )}
      </Card>

      {/* Optional direct model */}
      <Card>
        <h2 className="mb-3 text-sm font-semibold text-slate-200">直连模型（可选）</h2>
        <p className="mb-3 text-xs text-slate-500">
          模型只会看到抽样后的代表帧。下方会明确列出覆盖了多少镜头、哪些没被看到 ——
          未覆盖的判断不可轻信。
        </p>
        <Button variant="ghost" disabled={analyze.isPending} onClick={() => analyze.mutate()}>
          调用视觉模型分析
        </Button>

        {analyze.data && <DirectAnalysisReport data={analyze.data} />}
      </Card>
    </div>
  )
}

function DirectAnalysisReport({ data }: { data: DirectAnalysisResult }) {
  if (!data.configured) {
    return (
      <p className="mt-3 text-sm text-amber-300">
        未配置模型，请改用离线分析包流程。
      </p>
    )
  }

  const c = data.coverage
  return (
    <div className="mt-4 space-y-4 text-sm">
      {c && Object.keys(c).length > 0 && (
        <div className="rounded border border-ink-700 p-3">
          <div className="mb-2 flex items-center gap-2">
            <span className="font-medium text-slate-200">覆盖情况</span>
            <Badge tone={c.complete ? 'green' : 'yellow'}>
              {c.complete ? '已覆盖全部候选镜头' : '部分覆盖'}
            </Badge>
          </div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-400 sm:grid-cols-3">
            <span>送出图片 {c.images_sent}</span>
            <span>
              覆盖镜头 {c.segments_covered_by_images}/{c.candidate_segments}
            </span>
            <span>
              覆盖素材 {c.assets_covered_by_images}/{c.library_assets}
            </span>
          </div>
          {!c.complete && (c.segments_not_covered ?? 0) > 0 && (
            <p className="mt-2 text-xs text-amber-300">
              有 {c.segments_not_covered} 个候选镜头未被模型看到，报告中的整体判断可能不成立。
            </p>
          )}
        </div>
      )}

      {data.report && (
        <div>
          <div className="mb-1 font-medium text-slate-200">导演报告</div>
          <p className="whitespace-pre-wrap text-slate-300">{data.report}</p>
        </div>
      )}

      {data.stories && data.stories.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-slate-200">候选故事线</div>
          <ul className="space-y-1.5">
            {data.stories.map((st, i) => (
              <li key={i} className="rounded border border-ink-700 p-2">
                <div className="text-slate-200">{st.title || '（无标题）'}</div>
                {st.logline && <div className="text-xs text-slate-400">{st.logline}</div>}
                <div className="mt-1 text-xs text-slate-500">
                  引用镜头 {st.segment_ids.length} 个
                  {st.unknown_segment_ids.length > 0 && (
                    <span className="text-amber-400">
                      （已剔除 {st.unknown_segment_ids.length} 个虚构引用）
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <div className="mb-1 font-medium text-slate-200">
          复核请求（已入库 {data.created ?? 0} 条）
        </div>
        {data.review_requests && data.review_requests.length > 0 ? (
          <ul className="space-y-1 text-xs text-slate-300">
            {data.review_requests.map((r, i) => (
              <li key={i}>
                {r.priority} · {r.start_sec.toFixed(1)}–{r.end_sec.toFixed(1)}s ·{' '}
                {r.reason || '（无说明）'}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-slate-500">没有通过校验的复核请求。</p>
        )}
      </div>

      {data.problems && data.problems.length > 0 && (
        <div>
          <div className="mb-1 font-medium text-amber-300">被丢弃的模型输出</div>
          <ul className="max-h-32 space-y-0.5 overflow-auto text-xs text-amber-300/80">
            {data.problems.map((p, i) => (
              <li key={i}>{p}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
