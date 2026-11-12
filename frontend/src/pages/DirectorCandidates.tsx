/**
 * TSB-368-B — read-only director candidate browser.
 *
 * Deliberately minimal: it shows what exists, what has actually been observed and
 * what is unknown. It cannot apply anything, write a storyboard, or start
 * analysis — there is no button that does any of those, and no request is sent
 * unless the user changes a filter.
 *
 * The three observation channels are shown as three separate badges. Merging them
 * into one "analyzed" tick is exactly the confusion this page exists to prevent.
 */
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type {
  DirectorCandidate,
  DirectorCandidatesOut,
  DirectorCoverage,
  ObservationOut,
  ObservationState,
} from '../lib/types'
import { Badge, Button, Card, Spinner, inputClass } from '../components/ui'

const STATE_LABEL: Record<ObservationState, string> = {
  current: '已验证',
  freshness_unverified: '新鲜度未验证',
  stale: '已过期',
  not_analyzed: '未分析',
  failed: '分析失败',
}

const STATE_TONE: Record<ObservationState, string> = {
  current: 'green',
  freshness_unverified: 'blue',
  stale: 'yellow',
  not_analyzed: 'slate',
  failed: 'red',
}

function ChannelBadge({ name, ch }: { name: string; ch: ObservationOut }) {
  return (
    <span
      className="inline-flex items-center gap-1"
      data-testid={`channel-${name}`}
      data-state={ch.state}
      title={ch.limitations.join('; ') || undefined}
    >
      <Badge tone={STATE_TONE[ch.state]}>{name}: {STATE_LABEL[ch.state]}</Badge>
    </span>
  )
}

function CoverageBar({ cov }: { cov: DirectorCoverage }) {
  const pct = cov.library_segments
    ? Math.round((cov.segments_with_any_valid_reading / cov.library_segments) * 100)
    : 0
  return (
    <div className="text-xs text-slate-400" data-testid="coverage">
      <div className="mb-1">
        素材库共 <strong className="text-slate-200">{cov.library_segments}</strong> 个镜头 ·
        有任何可靠读数 <strong className="text-slate-200">{cov.segments_with_any_valid_reading}</strong> /
        {cov.library_segments}（{pct}%）· 无有效读数{' '}
        <strong className="text-amber-300">{cov.no_valid_model_reading}</strong>
      </div>
      <div className="flex flex-wrap gap-3">
        <span data-testid="cov-static">静帧有效 {cov.static_valid}</span>
        <span data-testid="cov-multiframe">多帧有效 {cov.multiframe_valid}</span>
        <span data-testid="cov-video">原生视频有效 {cov.video_native_valid}</span>
        <span>合格 {cov.eligible_segments}</span>
        <span>待复核 {cov.needs_review_segments}</span>
        <span>已排除 {cov.excluded_segments}</span>
      </div>
      {!cov.coverage_complete && (
        <div className="mt-1 text-amber-300" data-testid="coverage-incomplete">
          分析覆盖不完整：并非所有镜头都已被模型看过。
        </div>
      )}
    </div>
  )
}

function CandidateRow({ c }: { c: DirectorCandidate }) {
  const visual = (c.observations.static.status_detail.visual ?? {}) as Record<string, unknown>
  const tags = Array.isArray(visual.scene_tags) ? (visual.scene_tags as string[]) : []
  const videoSummary =
    typeof c.observations.video_native.status_detail.summary === 'string'
      ? (c.observations.video_native.status_detail.summary as string)
      : ''
  return (
    <div
      className="rounded border border-ink-700 p-3 text-xs"
      data-testid={`candidate-${c.segment_id}`}
      data-eligibility={c.eligibility}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <span className="text-slate-300">{c.asset_display_name || c.asset_id.slice(0, 8)}</span>
        <span className="text-slate-500" data-testid="window">
          {c.source_window.start_sec.toFixed(1)}s – {c.source_window.end_sec.toFixed(1)}s
        </span>
        {c.eligibility !== 'eligible' && (
          <Badge tone={c.eligibility === 'excluded' ? 'red' : 'yellow'}>
            {c.eligibility === 'excluded' ? '已排除' : '待复核'}
          </Badge>
        )}
        {c.human.rating !== 'pending' && (
          <span data-testid="human-rating">
            <Badge tone={c.human.rating === 'exclude' ? 'red' : 'green'}>
              人工: {c.human.rating}
            </Badge>
          </span>
        )}
        {c.human.story_role && <Badge tone="blue">角色: {c.human.story_role}</Badge>}
        {c.editorial_hints.priority === 'preferred' && <Badge tone="green">优先</Badge>}
        {/* TSB-371: a human judged this shot's reading wrong. Shown as a caution,
            not an automatic exclusion — the decision stays with the user. */}
        {c.review_state === 'wrong' && (
          <span data-testid="review-wrong">
            <Badge tone="red">人工判错</Badge>
          </span>
        )}
        {c.review_state === 'trusted' && (
          <span data-testid="review-trusted">
            <Badge tone="green">人工核对通过</Badge>
          </span>
        )}
      </div>

      <div className="mb-1 flex flex-wrap gap-2">
        <ChannelBadge name="静帧" ch={c.observations.static} />
        <ChannelBadge name="多帧" ch={c.observations.multiframe} />
        <ChannelBadge name="原生视频" ch={c.observations.video_native} />
      </div>

      {(tags.length > 0 || videoSummary) && (
        <div className="text-slate-400">
          {tags.length > 0 && <span>静帧标签: {tags.join(', ')} </span>}
          {videoSummary && (
            <span className="text-slate-500">· 视频摘要: {videoSummary}</span>
          )}
        </div>
      )}

      {(c.exclusion_reasons.length > 0 || c.limitations.length > 0) && (
        <div className="mt-1 text-slate-500" data-testid="reasons">
          {[...c.exclusion_reasons, ...c.limitations].slice(0, 6).join(' · ')}
        </div>
      )}
    </div>
  )
}

export default function DirectorCandidates() {
  const { id = '' } = useParams()
  const [eligibility, setEligibility] = useState('eligible')
  const [rating, setRating] = useState('')
  const [storyRole, setStoryRole] = useState('')
  const [assetId, setAssetId] = useState('')
  const [sort, setSort] = useState('editorial_priority')
  const [offset, setOffset] = useState(0)
  const limit = 20

  const params = useMemo(() => {
    const p = new URLSearchParams()
    p.set('limit', String(limit))
    p.set('offset', String(offset))
    p.set('eligibility', eligibility)
    p.set('sort', sort)
    if (rating) p.set('rating', rating)
    if (storyRole) p.set('story_role', storyRole)
    if (assetId) p.set('asset_id', assetId)
    return p.toString()
  }, [eligibility, rating, storyRole, assetId, sort, offset])

  const q = useQuery({
    queryKey: ['director-candidates', id, params],
    queryFn: () => api<DirectorCandidatesOut>(`${endpoints.directorCandidates(id)}?${params}`),
    enabled: Boolean(id),
  })

  // Distinct assets seen so far, for the asset filter.
  const assets = useMemo(() => {
    const seen = new Map<string, string>()
    for (const c of q.data?.items ?? []) {
      seen.set(c.asset_id, c.asset_display_name || c.asset_id.slice(0, 8))
    }
    return [...seen.entries()]
  }, [q.data])

  const resetPage = () => setOffset(0)

  if (q.isError) {
    return (
      <div className="p-6" data-testid="director-error">
        <div className="text-red-400">加载候选镜头失败：{(q.error as Error).message}</div>
        <Button className="mt-3" onClick={() => q.refetch()}>重试</Button>
      </div>
    )
  }

  const data = q.data
  const isEmptyProject = data !== undefined && data.coverage.library_segments === 0
  const isEmptyFilter = data !== undefined && data.total === 0 && !isEmptyProject

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">
          ← 返回项目
        </Link>
        <h1 className="text-lg font-semibold text-slate-200">导演候选镜头</h1>
        <span className="text-xs text-slate-500">只读 · 不触发分析、不写入故事板</span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        <Card>
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <select
              className={inputClass}
              value={eligibility}
              data-testid="filter-eligibility"
              onChange={(e) => { setEligibility(e.target.value); resetPage() }}
            >
              <option value="eligible">合格</option>
              <option value="needs_review">待复核</option>
              <option value="excluded">已排除</option>
              <option value="all">全部</option>
            </select>
            <select
              className={inputClass}
              value={rating}
              data-testid="filter-rating"
              onChange={(e) => { setRating(e.target.value); resetPage() }}
            >
              <option value="">全部人工评分</option>
              <option value="favorite">favorite</option>
              <option value="like">like</option>
              <option value="pending">未评分</option>
              <option value="exclude">exclude</option>
            </select>
            <input
              className={inputClass}
              placeholder="故事角色"
              value={storyRole}
              data-testid="filter-story-role"
              onChange={(e) => { setStoryRole(e.target.value); resetPage() }}
            />
            <select
              className={inputClass}
              value={assetId}
              data-testid="filter-asset"
              onChange={(e) => { setAssetId(e.target.value); resetPage() }}
            >
              <option value="">全部素材</option>
              {assets.map(([aid, name]) => (
                <option key={aid} value={aid}>{name}</option>
              ))}
            </select>
            <select
              className={inputClass}
              value={sort}
              data-testid="filter-sort"
              onChange={(e) => { setSort(e.target.value); resetPage() }}
            >
              <option value="editorial_priority">按策划优先级</option>
              <option value="timeline">按时间线</option>
            </select>
            <Button onClick={() => q.refetch()} data-testid="refresh">
              刷新
            </Button>
          </div>
          {data && <CoverageBar cov={data.coverage} />}
        </Card>

        {q.isLoading && <Spinner />}

        {isEmptyProject && (
          <Card>
            <div className="text-sm text-slate-400" data-testid="empty-project">
              这个项目还没有任何素材镜头。请先扫描并检测素材。
            </div>
          </Card>
        )}

        {isEmptyFilter && (
          <Card>
            <div className="text-sm text-slate-400" data-testid="empty-filter">
              当前筛选条件没有命中镜头（项目共 {data.coverage.library_segments} 个镜头）。
            </div>
          </Card>
        )}

        {data && data.items.length > 0 && (
          <div className="space-y-2" data-testid="candidate-list">
            {data.items.map((c) => (
              <CandidateRow key={c.segment_id} c={c} />
            ))}
          </div>
        )}

        {data && data.total > limit && (
          <div className="flex items-center gap-3 text-xs text-slate-400">
            <Button
              disabled={offset === 0}
              data-testid="prev-page"
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              上一页
            </Button>
            <span data-testid="page-info">
              {offset + 1}–{Math.min(offset + limit, data.total)} / {data.total}
            </span>
            <Button
              disabled={offset + limit >= data.total}
              data-testid="next-page"
              onClick={() => setOffset(offset + limit)}
            >
              下一页
            </Button>
          </div>
        )}

        {data && data.limitations.length > 0 && (
          <div className="text-xs text-slate-500" data-testid="page-limitations">
            限制：{data.limitations.join(' · ')}
          </div>
        )}
      </div>
    </div>
  )
}
