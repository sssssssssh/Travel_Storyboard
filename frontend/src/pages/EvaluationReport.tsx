/**
 * TSB-371-D — the report page.
 *
 * The page shows exactly what the exported files contain. Where a number has no
 * denominator it prints `N/A`; it never rounds "no data" up to a percentage.
 */
import { useQuery } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, endpoints, withToken } from '../lib/api'
import { Badge, Button, Card, Spinner } from '../components/ui'

interface ReportMetrics {
  evaluated_claims: number
  strict_accuracy: number | null
  partial_or_better_rate: number | null
  by_dimension: Record<string, {
    evaluated: number
    strict_accuracy: number | null
    partial_or_better_rate: number | null
  }>
  hallucination: { severe: number; mild: number; judged: number; rate: number | null }
  counts: Record<string, number>
}

interface ReportBody {
  session_id: string
  project_code: string
  scope: { n: number; note: string }
  human_metrics: ReportMetrics
  status: string
  gates: Record<string, { status?: string; reason?: string } | string>
  ab: { status: string; reason: string } | null
  limitations: string[]
  disclaimer: string
}

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? 'N/A' : `${(v * 100).toFixed(1)}%`

export default function EvaluationReport() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const sessionId = params.get('session') ?? ''

  const report = useQuery({
    queryKey: ['eval-report', id, sessionId],
    queryFn: () => api<ReportBody>(endpoints.evaluationReport(id, sessionId)),
    enabled: Boolean(id && sessionId),
    retry: false,
  })

  const download = async (format: 'json' | 'csv' | 'md') => {
    const url = new URL(withToken(endpoints.evaluationReport(id, sessionId)),
                        window.location.origin)
    url.searchParams.set('format', format)
    const res = await fetch(url.toString())
    const text = await res.text()
    const blob = new Blob([text], {
      type: format === 'json' ? 'application/json' : format === 'csv' ? 'text/csv' : 'text/markdown',
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `tsb371-report-${sessionId.slice(0, 8)}.${format}`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  if (!sessionId) {
    return <div className="p-6"><Card><div className="text-sm text-slate-300">缺少会话 ID。</div></Card></div>
  }

  if (report.isLoading) return <div className="p-6"><Spinner /></div>

  if (report.isError) {
    return (
      <div className="p-6">
        <Card>
          <div className="text-sm text-red-400" data-testid="report-error">报告加载失败。</div>
        </Card>
      </div>
    )
  }

  const r = report.data!
  const m = r.human_metrics

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/evaluation/rating?session=${sessionId}`}
              className="text-sm text-slate-400 hover:text-slate-200">← 继续评分</Link>
        <h1 className="text-lg font-semibold text-slate-200">评价报告</h1>
        <Badge tone={r.status === 'MANUAL_REVIEW_PENDING' ? 'yellow' : 'green'}
               >{r.status}</Badge>
        <span className="ml-auto flex gap-2">
          <Button data-testid="download-json" onClick={() => download('json')}>导出 JSON</Button>
          <Button data-testid="download-csv" onClick={() => download('csv')}>导出 CSV</Button>
          <Button data-testid="download-md" onClick={() => download('md')}>导出 Markdown</Button>
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        <Card>
          <div className="text-xs text-slate-400" data-testid="scope-line">
            项目 {r.project_code} · 会话 {r.session_id.slice(0, 8)} ·
            有效人工判断 n = <strong>{r.scope.n}</strong>
          </div>
          <div className="mt-1 text-xs text-slate-500">{r.scope.note}</div>
        </Card>

        <Card>
          <div className="mb-2 text-xs text-slate-400">人工已核实结果</div>
          <table className="w-full text-xs">
            <tbody>
              <tr><td className="py-0.5 text-slate-400">有效断言数</td>
                  <td className="text-slate-200" data-testid="m-n">{m.evaluated_claims}</td></tr>
              <tr><td className="py-0.5 text-slate-400">严格准确率</td>
                  <td className="text-slate-200" data-testid="m-strict">{pct(m.strict_accuracy)}</td></tr>
              <tr><td className="py-0.5 text-slate-400">部分及以上</td>
                  <td className="text-slate-200" data-testid="m-partial">
                    {pct(m.partial_or_better_rate)}</td></tr>
              <tr><td className="py-0.5 text-slate-400">幻觉（严重 / 轻微）</td>
                  <td className="text-slate-200" data-testid="m-hallucination">
                    {m.hallucination.severe} / {m.hallucination.mild}</td></tr>
              <tr><td className="py-0.5 text-slate-400">幻觉率</td>
                  <td className="text-slate-200">{pct(m.hallucination.rate)}</td></tr>
            </tbody>
          </table>
          {m.evaluated_claims === 0 && (
            <div className="mt-2 text-xs text-amber-300" data-testid="pending-note">
              还没有有效人工评分，因此准确率为 N/A。系统不会在无人评分时显示 100%。
            </div>
          )}
        </Card>

        <Card>
          <div className="mb-2 text-xs text-slate-400">分维度</div>
          <table className="w-full text-xs">
            <thead><tr className="text-slate-500">
              <th className="text-left">维度</th><th>已评</th><th>严格</th><th>部分+</th>
            </tr></thead>
            <tbody>
              {Object.entries(m.by_dimension).map(([dim, block]) => (
                <tr key={dim} data-testid={`dim-row-${dim}`}>
                  <td className="text-slate-400">{dim}</td>
                  <td className="text-center text-slate-300">{block.evaluated}</td>
                  <td className="text-center text-slate-300">{pct(block.strict_accuracy)}</td>
                  <td className="text-center text-slate-300">{pct(block.partial_or_better_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>

        <Card>
          <div className="mb-2 text-xs text-slate-400">故事线 A/B</div>
          <div className="text-xs text-slate-300" data-testid="ab-status">
            {r.ab ? `${r.ab.status} — ${r.ab.reason}` : '见 A/B 会话'}
          </div>
        </Card>

        <Card>
          <div className="mb-2 text-xs text-slate-400">未完成门禁</div>
          <ul className="text-xs text-slate-400" data-testid="gate-list">
            {Object.entries(r.gates).map(([gate, info]) => (
              <li key={gate}>
                <code className="text-slate-300">{gate}</code>
                {' : '}
                {typeof info === 'string'
                  ? info
                  : <span className={info.status === 'BLOCKED' ? 'text-amber-300' : 'text-slate-300'}>
                      {info.status} — {info.reason}
                    </span>}
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <div className="mb-1 text-xs text-slate-400">限制与声明</div>
          <ul className="list-disc pl-4 text-xs text-slate-500" data-testid="limitations">
            {r.limitations.map((l, i) => <li key={i}>{l}</li>)}
          </ul>
          <div className="mt-2 text-xs text-slate-500">{r.disclaimer}</div>
        </Card>
      </div>
    </div>
  )
}
