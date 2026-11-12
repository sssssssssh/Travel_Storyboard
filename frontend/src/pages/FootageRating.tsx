/**
 * TSB-371-B — per-claim review.
 *
 * The first version asked for one score per dimension ("场景: 准确/部分/错误").
 * That compresses a model's seven sentences into one number, so "six right, one
 * wrong" and "all seven half-right" become indistinguishable — and the reviewer
 * has no way to say *which* sentence was wrong, which is the only thing that can
 * be acted on.
 *
 * This version asks about each claim separately:
 *
 *     湖上有很多小船        [对] [错] [不确定]
 *     木栈道横跨水面        [对] [错] [不确定]
 *     栏杆上有鸟（它也不确定） [对] [错] [不确定]
 *
 * A claim left untouched is *unjudged*, not wrong. The dimension scores the API
 * still expects are derived from these per-claim answers so nothing downstream
 * changes, and the per-claim detail is stored alongside for the report.
 */
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints, frameUrl } from '../lib/api'
import type { EvalSessionDetail, EvalSessionItem } from '../lib/types'
import { Badge, Button, Card, Spinner } from '../components/ui'

type Verdict = 'right' | 'wrong' | 'unsure'

const READING_GROUPS = [
  { key: 'subjects', label: '主体', dimension: 'subject' as const },
  { key: 'visible_actions', label: '动作', dimension: 'action' as const },
  { key: 'scene_tags', label: '场景标签', dimension: 'scene' as const },
  { key: 'mood', label: '情绪', dimension: 'scene' as const },
]

const VERDICT_LABEL: Record<Verdict, string> = {
  right: '对',
  wrong: '错',
  unsure: '不确定',
}

interface Claim {
  key: string
  group: string
  dimension: 'scene' | 'subject' | 'action'
  text: string
}

/** Flatten the readings into individually judgeable claims. */
function toClaims(readings: Record<string, unknown> | undefined): Claim[] {
  if (!readings) return []
  const out: Claim[] = []
  for (const group of READING_GROUPS) {
    const value = readings[group.key]
    if (!value) continue
    const list = Array.isArray(value) ? value : [value]
    list.forEach((raw, i) => {
      const text = String(raw).trim()
      if (!text) return
      out.push({ key: `${group.key}:${i}`, group: group.label,
                 dimension: group.dimension, text })
    })
  }
  return out
}

/**
 * Map per-claim verdicts onto the legacy dimension score.
 *
 *   all right            -> 2
 *   some right, some bad -> 1
 *   none right, any bad  -> 0
 *   nothing judged       -> null (excluded from every denominator)
 */
function dimensionScore(verdicts: Verdict[]): 0 | 1 | 2 | null {
  const judged = verdicts.filter((v) => v !== 'unsure')
  if (judged.length === 0) return null
  const right = judged.filter((v) => v === 'right').length
  if (right === judged.length) return 2
  if (right === 0) return 0
  return 1
}

function ClaimRow({
  claim, verdict, onPick, disabled,
}: {
  claim: Claim
  verdict: Verdict | undefined
  onPick: (v: Verdict) => void
  disabled: boolean
}) {
  const tone =
    verdict === 'right' ? 'border-emerald-700' :
    verdict === 'wrong' ? 'border-red-700' :
    verdict === 'unsure' ? 'border-amber-700' : 'border-ink-700'
  return (
    <div className={`flex flex-wrap items-center gap-2 rounded border ${tone} px-2 py-1.5`}
         data-testid={`claim-${claim.key}`}>
      <span className="w-12 shrink-0 text-xs text-slate-500">{claim.group}</span>
      <span className="min-w-0 flex-1 text-xs text-slate-300">{claim.text}</span>
      <span className="flex shrink-0 gap-1">
        {(['right', 'wrong', 'unsure'] as Verdict[]).map((v) => (
          <Button key={v} variant={verdict === v ? 'primary' : 'ghost'}
                  disabled={disabled}
                  data-testid={`claim-${claim.key}-${v}`}
                  onClick={() => onPick(v)}>
            {VERDICT_LABEL[v]}
          </Button>
        ))}
      </span>
    </div>
  )
}

export default function FootageRating() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const sessionId = params.get('session') ?? ''
  // Remount per session so verdicts never leak between sessions.
  return <FootageRatingInner key={`${id}:${sessionId}`} />
}

function FootageRatingInner() {
  const { id = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const sessionId = params.get('session') ?? ''
  const [index, setIndex] = useState(0)
  // claim key -> verdict, for the item currently on screen
  const [verdicts, setVerdicts] = useState<Record<string, Verdict>>({})
  const qc = useQueryClient()

  const createSession = useMutation({
    mutationFn: () => api<EvalSessionDetail>(endpoints.evaluationSessions(id), {
      method: 'POST',
      body: JSON.stringify({ limit: 5 }),
    }),
    onSuccess: (data) => {
      setParams({ session: data.session_id })
      setIndex(0)
    },
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
      qc.setQueryData(['eval-session', id, sessionId], data)
      void qc.invalidateQueries({ queryKey: ['eval-summary', id, sessionId] })
    },
  })

  const items: EvalSessionItem[] = session.data?.items ?? []
  const current = items[index]
  const frameId = current?.frame_ids?.[0]
  const frameSrc = frameId ? frameUrl(frameId) : ''
  const claims = useMemo(() => toClaims(current?.readings), [current])

  /** Persist the current item: derive dimension scores from the claim verdicts. */
  const save = (next: Record<string, Verdict>) => {
    setVerdicts(next)
    if (!current) return
    const byDim: Record<string, Verdict[]> = { scene: [], subject: [], action: [] }
    for (const claim of claims) {
      const v = next[claim.key]
      if (v) byDim[claim.dimension].push(v)
    }
    const fresh = qc.getQueryData<EvalSessionDetail>(['eval-session', id, sessionId])
    const live = fresh?.items.find((i) => i.item_id === current.item_id) ?? current
    rate.mutate({
      itemId: current.item_id,
      body: {
        scene: dimensionScore(byDim.scene),
        subject: dimensionScore(byDim.subject),
        action: dimensionScore(byDim.action),
        // Per-claim detail, so the report can point at the sentence that was wrong.
        claim_verdicts: Object.entries(next).map(([key, verdict]) => ({ key, verdict })),
        expected_version: live.version,
      },
    })
  }

  const pick = (claimKey: string, v: Verdict) => {
    // Clicking the same verdict again clears it, so a misclick is reversible and
    // "leaving it alone" stays possible.
    const next = { ...verdicts }
    if (next[claimKey] === v) delete next[claimKey]
    else next[claimKey] = v
    save(next)
  }

  const summary = useQuery({
    queryKey: ['eval-summary', id, sessionId],
    queryFn: () => api<{
      evaluated_claims: number
      strict_accuracy: number | null
      partial_or_better_rate: number | null
      status: string
    }>(`${endpoints.evaluationSessions(id)}/${sessionId}/summary`),
    enabled: Boolean(id && sessionId),
  })

  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? 'N/A' : `${(v * 100).toFixed(1)}%`

  const progress = useMemo(() => {
    const rated = items.filter((i) => i.review_status === 'RATED').length
    return { rated, total: items.length }
  }, [items])

  const goTo = (next: number) => {
    setIndex(Math.max(0, Math.min(items.length - 1, next)))
    setVerdicts({})
  }

  if (!sessionId) {
    return (
      <div className="p-6">
        <Card>
          <div className="mb-2 text-sm text-slate-300">还没有评价会话。</div>
          <div className="mb-3 text-xs text-slate-500">
            创建会话会冻结本次要核对的镜头与模型原话；之后刷新可继续。
          </div>
          <Button data-testid="create-session" disabled={createSession.isPending}
                  onClick={() => createSession.mutate()}>
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
              className="text-sm text-slate-400 hover:text-slate-200">← 镜头核对</Link>
        <h1 className="text-lg font-semibold text-slate-200">人工评分</h1>
        <span className="text-xs text-slate-500">
          逐句判断；不改动标注与 Storyboard
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
                {current.evidence_state === 'freshness_unverified' && (
                  <Badge tone="yellow">新鲜度未验证</Badge>
                )}
                <span className="text-slate-500" data-testid="item-window">
                  {current.source_window.start_abs_sec.toFixed(1)}s–
                  {current.source_window.end_abs_sec.toFixed(1)}s
                </span>
                <span className="ml-auto flex gap-1">
                  <Button variant="ghost" data-testid="prev-item" disabled={index === 0}
                          onClick={() => goTo(index - 1)}>←</Button>
                  <Button variant="ghost" data-testid="skip-item"
                          onClick={() => goTo(index + 1)}>跳过</Button>
                  <Button variant="ghost" data-testid="next-item"
                          disabled={index >= items.length - 1}
                          onClick={() => goTo(index + 1)}>→</Button>
                </span>
              </div>

              <div className="mb-2 flex min-h-[8rem] items-center justify-center">
                {frameSrc ? (
                  <img data-testid="item-frame" alt="待核对画面"
                       className="max-h-64 rounded border border-ink-700"
                       src={frameSrc}
                       onError={(e) => { e.currentTarget.style.visibility = 'hidden' }}
                       onLoad={(e) => { e.currentTarget.style.visibility = 'visible' }} />
                ) : (
                  <span className="text-xs text-slate-500" data-testid="no-frame">
                    该读数没有可显示的代表帧
                  </span>
                )}
              </div>

              <div className="text-xs text-slate-500" data-testid="play-hint">
                想看动态画面去
                <Link className="underline" to={`/project/${id}/evaluation/footage`}>
                  {' '}镜头核对页{' '}
                </Link>
                播放。
              </div>
            </Card>

            {!current.source_available && (
              <Card>
                <div className="text-sm text-red-300" data-testid="item-source-unavailable">
                  原片不可访问，无法核对画面。系统不会要求你打分，也不计入准确率。
                </div>
              </Card>
            )}

            <Card className={current.source_available ? '' : 'opacity-40 pointer-events-none'}>
              <div className="mb-2 text-xs text-slate-400">
                逐句判断：模型说的每一句，对得上画面吗？
                <span className="ml-2 text-slate-500">
                  不打分的句子既不算对也不算错，直接跳过即可
                </span>
              </div>
              {claims.length === 0 ? (
                <div className="text-xs text-slate-500" data-testid="no-claims">
                  这条读数没有可逐句判断的文字。
                </div>
              ) : (
                <div className="space-y-1">
                  {claims.map((claim) => (
                    <ClaimRow key={claim.key} claim={claim}
                              verdict={verdicts[claim.key]}
                              disabled={!current.source_available}
                              onPick={(v) => pick(claim.key, v)} />
                  ))}
                </div>
              )}
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
            <div className="text-xs text-slate-400">当前统计（仅来自你的评分）</div>
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
