/**
 * TSB-371 — "which file is missing?"
 *
 * A reviewer told "2 items unavailable" cannot tell a tool fault from a changed
 * library. This page names the file, its recorded size and when it was last
 * indexed, so the owner can recognise it — and deliberately says "not found at
 * the recorded location", never "deleted", because a rename or an unmounted drive
 * looks identical from here.
 *
 * No absolute path is shown: the folder *name* is enough to recognise, and the
 * full path is the user's own business.
 */
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, endpoints } from '../lib/api'
import { Badge, Card, Spinner } from '../components/ui'

interface MissingEntry {
  asset_id: string
  display_name: string
  status: string
  available: boolean
  size_mb: number
  folder_hint: string
  recorded_at: string | null
  affected_segments: number
}

interface MissingOut {
  project_id: string
  statement: string
  count: number
  items: MissingEntry[]
  summary: {
    assets_total: number
    assets_missing: number
    segments_affected: number
  }
  notes: string[]
}

function when(iso: string | null): string {
  if (!iso) return '未记录'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '未记录'
  return d.toLocaleString('zh-CN', { hour12: false })
}

export default function MissingSources() {
  const { id = '' } = useParams()
  const report = useQuery({
    queryKey: ['missing-sources', id],
    queryFn: () => api<MissingOut>(endpoints.evaluationMissingSources(id)),
    enabled: Boolean(id),
    staleTime: 0,
  })

  if (report.isLoading) return <div className="p-6"><Spinner /></div>
  if (report.isError) {
    return (
      <div className="p-6">
        <Card>
          <div className="text-sm text-red-400" data-testid="missing-error">加载失败。</div>
        </Card>
      </div>
    )
  }

  const r = report.data!

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/evaluation/footage`}
              className="text-sm text-slate-400 hover:text-slate-200">← 镜头核对</Link>
        <h1 className="text-lg font-semibold text-slate-200">源文件缺失</h1>
        <Badge tone={r.count > 0 ? 'red' : 'green'}>
          {r.count > 0 ? `${r.count} 个找不到` : '全部就位'}
        </Badge>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        <Card>
          <div className="text-xs text-slate-400" data-testid="missing-summary">
            素材共 {r.summary.assets_total} 个，其中 {r.summary.assets_missing} 个在记录位置找不到
            {r.summary.segments_affected > 0 &&
              `，涉及 ${r.summary.segments_affected} 个镜头`}。
          </div>
          <div className="mt-2 text-xs text-amber-300" data-testid="statement">
            这里只说<b>在记录位置没找到</b>，不代表文件被删除——改名、移动或磁盘未挂载都会这样。
          </div>
        </Card>

        {r.count === 0 ? (
          <Card>
            <div className="text-sm text-emerald-300" data-testid="all-present">
              所有素材都在记录位置。
            </div>
          </Card>
        ) : (
          <Card>
            <div className="mb-2 text-xs text-slate-400">找不到的文件</div>
            <div className="space-y-2">
              {r.items.map((item) => (
                <div key={item.asset_id} className="rounded border border-red-900 bg-red-950/20 p-2"
                     data-testid={`missing-${item.display_name}`}>
                  <div className="text-sm text-slate-200">
                    <code className="rounded bg-ink-900 px-1 py-0.5">{item.display_name}</code>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-3 text-xs text-slate-400">
                    <span>大小 {item.size_mb} MB</span>
                    {item.folder_hint && <span>文件夹 <code>{item.folder_hint}</code></span>}
                    <span>记录于 {when(item.recorded_at)}</span>
                    {item.affected_segments > 0 && (
                      <span className="text-red-300">涉及 {item.affected_segments} 个镜头</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </Card>
        )}

        <Card>
          <div className="mb-1 text-xs text-slate-400">说明</div>
          <ul className="list-disc pl-4 text-xs text-slate-500" data-testid="missing-notes">
            {r.notes.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        </Card>
      </div>
    </div>
  )
}
