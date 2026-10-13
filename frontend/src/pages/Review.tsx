import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints, mediaUrl } from '../lib/api'
import type { SegmentListItem, SegmentListOut } from '../lib/types'
import { formatTimeCode } from '../lib/format'
import { Badge, Button, EmptyState, Spinner, inputClass } from '../components/ui'

const RATINGS = [
  { key: 'like', label: '👍 推荐' },
  { key: 'favorite', label: '⭐ 收藏' },
  { key: 'exclude', label: '🚫 排除' },
  { key: 'pending', label: '? 待确认' },
] as const

const reasonLabel: Record<string, string> = {
  hard_cut: '硬切',
  content_change: '内容变化',
  max_duration: '时长兜底',
  end: '结尾',
  single: '单镜头',
}

const LIMIT = 60

function qualityHints(m: Record<string, unknown>): string[] {
  const hints: string[] = []
  const blur = m.blur_min as number | undefined
  const over = m.overexposed_ratio as number | undefined
  const under = m.underexposed_ratio as number | undefined
  if (typeof blur === 'number' && blur < 40) hints.push('模糊')
  if (typeof over === 'number' && over > 0.35) hints.push('过曝')
  if (typeof under === 'number' && under > 0.35) hints.push('欠曝')
  return hints
}

export default function Review() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const [rating, setRating] = useState('')
  const [reason, setReason] = useState('')
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<SegmentListItem | null>(null)
  const [note, setNote] = useState('')
  const [storyRole, setStoryRole] = useState('')
  const [inSec, setInSec] = useState('')
  const [outSec, setOutSec] = useState('')

  const query = new URLSearchParams()
  query.set('offset', String(page * LIMIT))
  query.set('limit', String(LIMIT))
  if (rating) query.set('rating', rating)
  if (reason) query.set('boundary_reason', reason)

  const { data, isLoading } = useQuery({
    queryKey: ['segments', id, rating, reason, page],
    queryFn: () => api<SegmentListOut>(endpoints.segments(id, `?${query.toString()}`)),
  })

  const annotate = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api(endpoints.annotate(selected!.id), { method: 'POST', body: JSON.stringify(payload) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['segments', id] })
      qc.invalidateQueries({ queryKey: ['project', id] })
    },
  })

  function openSegment(s: SegmentListItem) {
    setSelected(s)
    setNote(s.note ?? '')
    setStoryRole(s.story_role)
    setInSec(String(s.start_sec))
    setOutSec(String(s.end_sec))
  }

  function saveRange() {
    if (!selected) return
    annotate.mutate({ in_sec: Number(inSec), out_sec: Number(outSec) })
  }

  const selectedView = data?.items.find((s) => s.id === selected?.id) ?? selected
  const totalPages = data ? Math.max(1, Math.ceil(data.total / LIMIT)) : 1

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-800 px-6 py-4">
        <div className="flex items-center gap-3">
          <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">
            ← 返回项目
          </Link>
          <h1 className="text-lg font-semibold text-white">分镜审阅</h1>
          <span className="text-sm text-slate-500">{data?.total ?? 0} 个片段</span>
        </div>
        <div className="flex gap-2">
          <select className={inputClass + ' max-w-[140px]'} value={rating} onChange={(e) => { setRating(e.target.value); setPage(0) }}>
            <option value="">全部评分</option>
            {RATINGS.map((r) => (
              <option key={r.key} value={r.key}>{r.label}</option>
            ))}
          </select>
          <select className={inputClass + ' max-w-[140px]'} value={reason} onChange={(e) => { setReason(e.target.value); setPage(0) }}>
            <option value="">全部边界</option>
            {Object.entries(reasonLabel).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-auto px-6 py-5">
          {isLoading ? (
            <Spinner label="加载片段…" />
          ) : !data || data.items.length === 0 ? (
            <EmptyState title="暂无片段" hint="先返回项目页运行「检测片段」" />
          ) : (
            <>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
                {data.items.map((s) => {
                  const hints = qualityHints(s.quality_metrics)
                  return (
                    <button
                      key={s.id}
                      onClick={() => openSegment(s)}
                      className={`overflow-hidden rounded-lg border text-left transition-colors ${
                        selected?.id === s.id ? 'border-accent' : 'border-ink-700 hover:border-ink-600'
                      } bg-ink-900`}
                    >
                      <div className="relative aspect-video w-full bg-ink-800">
                        {s.thumbnail ? (
                          <img src={mediaUrl(id, s.thumbnail)} alt="" className="h-full w-full object-cover" loading="lazy" />
                        ) : (
                          <div className="flex h-full items-center justify-center text-xs text-slate-600">无预览</div>
                        )}
                        <div className="absolute bottom-1 right-1 rounded bg-black/70 px-1.5 py-0.5 text-xs text-slate-200">
                          {formatTimeCode(s.start_sec)} – {formatTimeCode(s.end_sec)}
                        </div>
                      </div>
                      <div className="p-2">
                        <div className="mb-1 flex items-center justify-between gap-2">
                          <span className="truncate text-xs text-slate-400">{s.asset_display_name}</span>
                          <Badge tone="slate">{reasonLabel[s.boundary_reason] ?? s.boundary_reason}</Badge>
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {hints.map((h) => (
                            <Badge key={h} tone={h === '模糊' ? 'yellow' : 'red'}>{h}</Badge>
                          ))}
                          {s.status === 'needs_review' && <Badge tone="yellow">需要复核</Badge>}
                          {s.user_rating !== 'pending' && (
                            <Badge tone="blue">{RATINGS.find((r) => r.key === s.user_rating)?.label ?? s.user_rating}</Badge>
                          )}
                        </div>
                      </div>
                    </button>
                  )
                })}
              </div>
              <div className="mt-5 flex items-center justify-center gap-4">
                <Button variant="ghost" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
                  上一页
                </Button>
                <span className="text-sm text-slate-500">
                  {page + 1} / {totalPages}
                </span>
                <Button variant="ghost" disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)}>
                  下一页
                </Button>
              </div>
            </>
          )}
        </div>

        {selectedView && (
          <aside className="w-80 shrink-0 overflow-auto border-l border-ink-800 bg-ink-900 p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-white">镜头详情</h2>
              <button className="text-slate-500 hover:text-slate-300" onClick={() => setSelected(null)}>✕</button>
            </div>
            <div className="mb-3 aspect-video w-full overflow-hidden rounded-lg bg-ink-800">
              {selectedView.thumbnail ? (
                <img src={mediaUrl(id, selectedView.thumbnail)} alt="" className="h-full w-full object-contain" />
              ) : (
                <div className="flex h-full items-center justify-center text-xs text-slate-600">无预览</div>
              )}
            </div>
            {selectedView.status === 'needs_review' && <p className="mb-3 text-xs text-amber-300">需要复核：{String(selectedView.quality_metrics.analysis_warning ?? '检测边界发生变化，旧镜头已保留；请使用新的检测镜头检查并更新剪辑计划。')}</p>}
            {annotate.error && <p className="mb-3 text-xs text-red-400">{annotate.error.message}</p>}
            <dl className="mb-4 space-y-1.5 text-sm">
              <div className="flex justify-between"><dt className="text-slate-500">素材</dt><dd className="max-w-[200px] truncate text-slate-200" title={selectedView.asset_display_name}>{selectedView.asset_display_name}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">入点</dt><dd className="text-slate-200">{selectedView.start_sec.toFixed(2)}s</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">出点</dt><dd className="text-slate-200">{selectedView.end_sec.toFixed(2)}s</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">时长</dt><dd className="text-slate-200">{selectedView.duration_sec.toFixed(2)}s</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">边界</dt><dd className="text-slate-200">{reasonLabel[selectedView.boundary_reason] ?? selectedView.boundary_reason}</dd></div>
              <div className="flex justify-between"><dt className="text-slate-500">清晰度</dt><dd className="text-slate-200">{(selectedView.quality_metrics.blur_min as number)?.toFixed(0) ?? '—'}</dd></div>
            </dl>

            <div className="mb-4">
              <div className="mb-1.5 text-xs font-medium uppercase text-slate-500">评分</div>
              <div className="grid grid-cols-2 gap-1.5">
                {RATINGS.map((r) => (
                  <button
                    key={r.key}
                    onClick={() => annotate.mutate({ rating: r.key })}
                    className={`rounded-md border px-2 py-1.5 text-xs transition-colors ${
                      selectedView.user_rating === r.key
                        ? 'border-accent bg-accent/20 text-white'
                        : 'border-ink-700 text-slate-300 hover:border-ink-600'
                    }`}
                  >
                    {r.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="mb-4">
              <div className="mb-1.5 text-xs font-medium uppercase text-slate-500">调整 in/out（秒）</div>
              <div className="flex items-center gap-2">
                <input className={inputClass} value={inSec} onChange={(e) => setInSec(e.target.value)} />
                <span className="text-slate-500">–</span>
                <input className={inputClass} value={outSec} onChange={(e) => setOutSec(e.target.value)} />
              </div>
              <Button variant="ghost" className="mt-2 w-full" disabled={annotate.isPending} onClick={saveRange}>
                保存区间
              </Button>
            </div>

            <div className="mb-4">
              <label className="mb-1.5 block text-xs font-medium uppercase text-slate-500">故事用途
                <input className={inputClass} value={storyRole} onChange={(e) => setStoryRole(e.target.value)} />
              </label>
              <Button variant="ghost" disabled={annotate.isPending} onClick={() => annotate.mutate({ story_role: storyRole })}>保存用途</Button>
            </div>
            <div>
              <div className="mb-1.5 text-xs font-medium uppercase text-slate-500">备注</div>
              <textarea
                className={`${inputClass} min-h-[80px]`}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="这段镜头的故事价值、用途…"
              />
              <Button variant="ghost" className="mt-2 w-full" disabled={annotate.isPending} onClick={() => annotate.mutate({ note })}>
                保存备注
              </Button>
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
