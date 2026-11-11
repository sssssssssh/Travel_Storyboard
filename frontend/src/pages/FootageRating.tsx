/**
 * TSB-371-B — the rating page. Designed around one idea: a human should be able
 * to judge five shots in under a minute.
 *
 * Each score is a single click. "Cannot judge" is its own button, never a zero —
 * that distinction is what keeps the accuracy honest. Progress survives a
 * refresh because every click is saved immediately.
 */
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type { EvalSessionDetail, EvalSessionItem } from '../lib/types'
import { Badge, Button, Card, Field, Spinner, inputClass } from '../components/ui'

type Score = 0 | 1 | 2 | null

const SCORE_LABEL: Record<string, string> = { '2': '准确', '1': '部分准确', '0': '错误' }
const DIMENSIONS: { key: 'scene' | 'subject' | 'action' | 'timing'; label: string }[] = [
  { key: 'scene', label: '场景' },
  { key: 'subject', label: '主体' },
  { key: 'action', label: '动作/运动' },
  { key: 'timing', label: '时序' },
]

function ScoreRow({
  label, value, onPick, testId,
}: {
  label: string
  value: Score | undefined
  onPick: (v: Score) => void
  testId: string
}) {
  return (
    <div className="flex items-center gap-2 py-1" data-testid={`dim-${testId}`}>
      <span className="w-20 text-xs text-slate-400">{label}</span>
      {(['2', '1', '0'] as const).map((v) => (
        <Button
          key={v}
          variant={String(value) === v ? 'primary' : 'ghost'}
          data-testid={`${testId}-score-${v}`}
          onClick={() => onPick(Number(v) as Score)}
        >
          {SCORE_LABEL[v]}
        </Button>
      ))}
      <Button
        variant={value === null ? 'primary' : 'ghost'}
        data-testid={`${testId}-score-null`}
        onClick={() => onPick(null)}
      >
        无法判断
      </Button>
    </div>
  )
}

export default function FootageRating() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const sessionId = params.get('session') ?? ''
  // Remount per session so switching sessions cannot carry an index across.
  return <FootageRatingInner key={`${id}:${sessionId}`} />
}

function FootageRatingInner() {
  const { id = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const sessionId = params.get('session') ?? ''
  const [index, setIndex] = useState(0)
  const qc = useQueryClient()

  const [creating, setCreating] = useState(false)
  const createSession = useMutation({
    mutationFn: () => api<EvalSessionDetail>(endpoints.evaluationSessions(id), {
      method: 'POST',
      body: JSON.stringify({ limit: 5 }),
    }),
    onSuccess: (data) => {
      setParams({ session: data.session_id })
      setIndex(0)
      setCreating(false)
    },
    onError: () => setCreating(false),
  })

  const session = useQuery({
    queryKey: ['eval-session', id, sessionId],
    queryFn: () => api<EvalSessionDetail>(
      `${endpoints.evaluationSessions(id)}/${sessionId}`),
    enabled: Boolean(id && sessionId),
    staleTime: 0,
  })

  const rate = useMutation({
    mutationFn: (args: { itemId: string; body: Record<string, unknown> }) =>
      api<EvalSessionDetail>(
        `${endpoints.evaluationSessions(id)}/${sessionId}/ratings/${args.itemId}`,
        { method: 'PATCH', body: JSON.stringify(args.body) }),
    onSuccess: (data) => {
      // Update the session (ratings + versions) AND invalidate the summary, or the
      // headline metric keeps showing the previous value and the next click sends a
      // stale expected_version (which the server correctly rejects with 409).
      qc.setQueryData(['eval-session', id, sessionId], data)
      void qc.invalidateQueries({ queryKey: ['eval-summary', id, sessionId] })
    },
  })

  const items: EvalSessionItem[] = session.data?.items ?? []
  const current = items[index]

  const summary = useQuery({
    queryKey: ['eval-summary', id, sessionId],
    queryFn: () => api<{
      evaluated_claims: number
      strict_accuracy: number | null
      partial_or_better_rate: number | null
      status: string
      hallucination: { severe: number; mild: number; judged: number; rate: number | null }
    }>(`${endpoints.evaluationSessions(id)}/${sessionId}/summary`),
    enabled: Boolean(id && sessionId),
  })

  const setScore = (dim: string, value: Score) => {
    if (!current) return
    // Read the version from the cache at click time: two quick clicks must send
    // 0 then 1, not 0 then 0.
    const fresh = qc.getQueryData<EvalSessionDetail>(['eval-session', id, sessionId])
    const live = fresh?.items.find((i) => i.item_id === current.item_id) ?? current
    rate.mutate({
      itemId: current.item_id,
      body: { [dim]: value, expected_version: live.version },
    })
  }

  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? 'N/A' : `${(v * 100).toFixed(1)}%`

  const progress = useMemo(() => {
    const rated = items.filter((i) => i.review_status === 'RATED').length
    return { rated, total: items.length }
  }, [items])

  if (!sessionId) {
    return (
      <div className="p-6">
        <Card>
          <div className="mb-2 text-sm text-slate-300">还没有评价会话。</div>
          <div className="mb-3 text-xs text-slate-500">
            创建会话会冻结本次要核对的样本；之后刷新页面可继续。
          </div>
          <Button data-testid="create-session" disabled={creating || createSession.isPending}
                  onClick={() => { setCreating(true); createSession.mutate() }}>
            {createSession.isPending ? '创建中…' : '开始核对（5 个镜头）'}
          </Button>
          {createSession.isError && (
            <div className="mt-2 text-xs text-red-400" data-testid="create-error">
              创建失败：{(createSession.error as { status?: number })?.status ?? '错误'}
            </div>
          )}
        </Card>
      </div>
    )
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/evaluation/footage`}
              className="text-sm text-slate-400 hover:text-slate-200">
          ← 镜头核对
        </Link>
        <h1 className="text-lg font-semibold text-slate-200">人工评分</h1>
        <span className="text-xs text-slate-500">
          只记录你的判断；不会修改镜头标注或 Storyboard
        </span>
        <span className="ml-auto text-xs text-slate-400" data-testid="progress">
          已评 {progress.rated} / {progress.total}
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        {session.isLoading && <Spinner />}
        {session.data && items.length === 0 && (
          <Card>
            <div className="text-sm text-amber-300" data-testid="no-items">
              这个会话没有可评价的镜头（没有模型读数）。
            </div>
          </Card>
        )}

        {current && (
          <>
            <Card>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-slate-400" data-testid="item-progress">
                  第 {index + 1} / {items.length} 个
                </span>
                <Badge tone="blue">{current.channel}</Badge>
                <Badge tone={current.evidence_state === 'freshness_unverified' ? 'yellow' : 'slate'}>
                  {current.evidence_state}
                </Badge>
                <span className="text-slate-500" data-testid="item-window">
                  {current.source_window.start_abs_sec.toFixed(1)}s–
                  {current.source_window.end_abs_sec.toFixed(1)}s
                </span>
                {current.review_status === 'STALE_EVIDENCE' && (
                  <Badge tone="red">证据已更新，需重新核对</Badge>
                )}
                <span className="ml-auto flex gap-1">
                  <Button variant="ghost" data-testid="prev-item" disabled={index === 0}
                          onClick={() => setIndex((i) => Math.max(0, i - 1))}>←</Button>
                  <Button variant="ghost" data-testid="skip-item"
                          onClick={() => setIndex((i) => Math.min(items.length - 1, i + 1))}>
                    跳过
                  </Button>
                  <Button variant="ghost" data-testid="next-item"
                          disabled={index >= items.length - 1}
                          onClick={() => setIndex((i) => Math.min(items.length - 1, i + 1))}>
                    →
                  </Button>
                </span>
              </div>

              <div className="mb-2 flex justify-center">
                <img data-testid="item-frame" alt="待核对画面"
                     className="max-h-64 rounded border border-ink-700"
                     src={`${endpoints.frameImage(current.result_id ?? '')}?token=${
                       encodeURIComponent(sessionStorage.getItem('tsb_token') ?? '')}`}
                     onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }} />
              </div>

              <div className="text-xs text-slate-500" data-testid="play-hint">
                如需观看动态画面，请到
                <Link className="underline" to={`/project/${id}/evaluation/footage`}> 镜头核对页 </Link>
                播放该片段。
              </div>
            </Card>

            {!current.source_available && (
              <Card>
                <div className="text-sm text-red-300" data-testid="item-source-unavailable">
                  原片不可访问，无法核对画面。该系统不会要求你为此打分，
                  且此镜头不计入准确率分母。
                </div>
              </Card>
            )}
            <Card className={current.source_available ? '' : 'opacity-40 pointer-events-none'}>
              <div className="mb-2 text-xs text-slate-400">
                逐项判断模型描述是否与画面一致（2=准确 1=部分正确 0=错误）
              </div>
              {DIMENSIONS.map((d) => (
                <ScoreRow key={d.key} label={d.label} testId={d.key}
                          value={(current.ratings[d.key] as Score) ?? undefined}
                          onPick={(v) => {
                            if (!current.source_available) return
                            setScore(d.key, v)
                          }} />
              ))}
              <div className="mt-2 flex flex-wrap gap-2 border-t border-ink-800 pt-2">
                <Field label="幻觉">
                  <select className={inputClass} data-testid="hallucination"
                          value={current.ratings.hallucination ?? ''}
                          onChange={(e) => rate.mutate({
                            itemId: current.item_id,
                            body: { hallucination: e.target.value || null,
                                    expected_version: current.version },
                          })}>
                    <option value="">未判断</option>
                    <option value="none">无</option>
                    <option value="mild">轻微</option>
                    <option value="severe">严重</option>
                  </select>
                </Field>
                <Field label="剪辑可用性">
                  <select className={inputClass} data-testid="usability"
                          value={current.ratings.usability ?? ''}
                          onChange={(e) => rate.mutate({
                            itemId: current.item_id,
                            body: { usability: e.target.value || null,
                                    expected_version: current.version },
                          })}>
                    <option value="">未判断</option>
                    <option value="useful">有用</option>
                    <option value="partly_useful">部分有用</option>
                    <option value="useless">无用</option>
                  </select>
                </Field>
              </div>
              {rate.isError && (
                <div className="mt-2 text-xs text-red-400" data-testid="rate-error">
                  保存失败：{(rate.error as { status?: number })?.status === 409
                    ? '这条评分已被修改，请刷新后重试' : '请重试'}
                </div>
              )}
            </Card>
          </>
        )}

        {summary.data && (
          <Card>
            <div className="text-xs text-slate-400">
              当前统计（仅来自你的评分）
            </div>
            <div className="mt-1 text-sm text-slate-300" data-testid="live-summary">
              有效判断 {summary.data.evaluated_claims} 项 ·
              严格准确率 {pct(summary.data.strict_accuracy)} ·
              部分及以上 {pct(summary.data.partial_or_better_rate)}
            </div>
            <div className="mt-1 text-xs text-slate-500">
              {summary.data.evaluated_claims === 0
                ? '还没有任何有效判断，因此准确率为 N/A——不会显示为 100%。'
                : '小样本，不代表全库准确率。'}
            </div>
            <div className="mt-2">
              <Link to={`/project/${id}/evaluation/report?session=${sessionId}`}>
                <Button data-testid="to-report">查看并导出报告</Button>
              </Link>
            </div>
          </Card>
        )}
      </div>
    </div>
  )
}
