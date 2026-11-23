/**
 * TSB-371-C — the blind A/B page.
 *
 * The blind only works if the page cannot tell which side is which. So:
 *   - the two sides are rendered by the same component with the same styling;
 *   - nothing from the server names the planner, the model, or the variant;
 *   - the preference is submitted before the mapping is revealed;
 *   - after submission the page says, in words, that one reviewer's preference is
 *     not a population win rate.
 *
 * When no real model plan exists the page must say so — never dress an offline or
 * mocked plan up as the model side.
 */
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, endpoints, withToken } from '../lib/api'
import type { AbBlindView, AbRevealedView, AbSide, EvalClipStatus } from '../lib/types'
import { Badge, Button, Card, Spinner, inputClass } from '../components/ui'

const ROLE_LABEL: Record<string, string> = {
  opening: '开场', development: '展开', turning_point: '转折',
  climax: '高潮', closing: '收束',
}

const DIMENSIONS = [
  { key: 'coherence', label: '故事是否连贯' },
  { key: 'pacing', label: '节奏建议' },
  { key: 'reason_credibility', label: '推荐理由可信度' },
  { key: 'operability', label: '剪辑可操作性' },
] as const

const CHOICES = [['A', 'A 更好'], ['B', 'B 更好'], ['TIE', '两者相当'], ['UNABLE', '无法判断']]


/**
 * One plan shot's preview.
 *
 * Clips are expensive (FFmpeg), so they are generated on demand rather than for
 * every shot on load. The button is the point of the page: without it the blind
 * review is a comparison of prose.
 */
function ShotClip({
  projectId, segmentId, startSec, endSec, testId,
}: {
  projectId: string
  segmentId: string
  startSec: number
  endSec: number
  testId: string
}) {
  const [wanted, setWanted] = useState(false)
  const clip = useQuery({
    queryKey: ['ab-clip', projectId, segmentId, startSec, endSec],
    queryFn: () => api<EvalClipStatus>(endpoints.evaluationClips(projectId), {
      method: 'POST',
      body: JSON.stringify({ segment_id: segmentId, start_sec: startSec, end_sec: endSec }),
    }),
    enabled: wanted && Boolean(projectId && segmentId),
    retry: false,
    staleTime: Infinity,
  })

  const err = clip.error as { status?: number; detail?: { code?: string } } | null

  if (!wanted) {
    return (
      <div className="mt-1">
        <Button variant="ghost" data-testid={`${testId}-load`}
                onClick={() => setWanted(true)}>
          查看这段画面
        </Button>
      </div>
    )
  }
  if (clip.isLoading) return <div className="mt-1 text-slate-500">生成片段中…</div>
  if (err) {
    const code = err.detail?.code ?? `HTTP_${err.status}`
    const text = code === 'EVAL_CLIP_SOURCE_UNAVAILABLE'
      ? '该镜头源文件不可访问，无法预览画面'
      : code === 'EVAL_CLIP_SEGMENT_TOO_SHORT'
        ? '该区间太短，无法生成预览'
        : `片段不可用（${code}）`
    return <div className="mt-1 text-amber-400" data-testid={`${testId}-error`}>{text}</div>
  }
  if (!clip.data) return null
  return (
    <video data-testid={testId} controls preload="metadata"
           className="mt-1 w-full max-h-40 rounded bg-black"
           src={withToken(clip.data.media_url)} />
  )
}

function SidePanel({
  side, scores, onScore, disabled, projectId,
}: {
  side: AbSide
  scores: Record<string, number>
  onScore: (dim: string, value: number) => void
  disabled: boolean
  projectId: string
}) {
  const sectionTitles = new Map(side.sections.map((section) => [section.section_id, section.title]))
  return (
    <Card>
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-slate-200" data-testid={`side-${side.side}`}>
          方案 {side.side}
        </h3>
        <span className="text-xs text-slate-500">
          {side.shots.length} 个镜头 · 合计 {side.total_duration_sec.toFixed(1)}s
        </span>
      </div>
      {disabled && side.title && <p className="mb-2 text-sm text-slate-300">{side.title}</p>}
      {disabled && side.logline && <p className="mb-2 text-xs text-slate-400">{side.logline}</p>}
      <div className="mb-2 rounded border border-ink-700 p-2">
        {DIMENSIONS.map((d) => (
          <div key={d.key} className="flex items-center gap-1 py-0.5 text-xs">
            <span className="w-28 text-slate-400">{d.label}</span>
            {[0, 1, 2, 3, 4].map((v) => (
              <Button key={v} variant={scores[d.key] === v ? 'primary' : 'ghost'}
                      disabled={disabled}
                      data-testid={`score-${side.side}-${d.key}-${v}`}
                      onClick={() => onScore(d.key, v)}>
                {v}
              </Button>
            ))}
          </div>
        ))}
      </div>

      <div className="mb-2 flex flex-wrap gap-1 text-xs text-slate-400" data-testid={`structure-${side.side}`}>
        {Object.entries(side.storyline).map(([role, orders]) =>
          orders.length > 0 ? (
            <Badge key={role} tone="slate">
              {ROLE_LABEL[role] ?? role} #{orders.join(',')}
            </Badge>
          ) : null,
        )}
      </div>

      <div className="space-y-1">
        {side.shots.map((shot) => (
          <div key={`${side.side}-${shot.order}`} className="rounded border border-ink-700 p-2 text-xs"
               data-testid={`shot-${side.side}-${shot.order}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-slate-500">#{shot.order}</span>
              <Badge tone="blue">{ROLE_LABEL[shot.narrative_role] ?? shot.narrative_role}</Badge>
              {shot.section_id && sectionTitles.has(shot.section_id) &&
                <span className="text-slate-400">
                  {sectionTitles.get(shot.section_id)}
                </span>}
              <span className="text-slate-400">
                {shot.source_in_sec.toFixed(1)}s–{shot.source_out_sec.toFixed(1)}s
              </span>
              <span className="text-slate-500">{shot.planned_duration_sec.toFixed(1)}s</span>
            </div>
            <div className="mt-1 text-slate-400">{shot.selection_reason}</div>
            <div className="text-slate-500">{[shot.pacing_hint, shot.transition_hint].filter(Boolean).join(' · ')}</div>
            {disabled && shot.claims.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-slate-500">
                {shot.claims.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            )}
            {disabled && shot.needs_review.length > 0 &&
              <p className="mt-1 text-amber-300">待复核：{shot.needs_review.join('、')}</p>}
            {/* Pictures, not just timecodes: a blind review of prose is not a
                review of the cut. The clip is derived and silent. */}
            <ShotClip
              projectId={projectId}
              segmentId={shot.segment_id}
              startSec={shot.source_in_sec}
              endSec={shot.source_out_sec}
              testId={`clip-${side.side}-${shot.order}`}
            />
          </div>
        ))}
      </div>
    </Card>
  )
}

export default function StorylineAB() {
  const { id = '' } = useParams()
  const [params] = useSearchParams()
  const sessionId = params.get('session') ?? ''
  const [a, setA] = useState<Record<string, number>>({})
  const [b, setB] = useState<Record<string, number>>({})
  const [preference, setPreference] = useState<string>('')
  const [dimensionPreferences, setDimensionPreferences] = useState<Record<string, string>>({})
  const [revealed, setRevealed] = useState<AbRevealedView | null>(null)

  const blind = useQuery({
    queryKey: ['ab-blind', id, sessionId],
    queryFn: () => api<AbBlindView | AbRevealedView>(`${endpoints.evaluationAbSessions(id)}/${sessionId}`),
    enabled: Boolean(id && sessionId),
    retry: false,
  })

  const blocked = blind.error as { status?: number; detail?: { code?: string } } | null

  const submit = useMutation({
    mutationFn: () => api<AbRevealedView>(
      `${endpoints.evaluationAbSessions(id)}/${sessionId}/submit`,
      {
        method: 'POST',
        body: JSON.stringify({
          preference,
          a_scores: a,
          b_scores: b,
          dimension_preferences: Object.fromEntries(
            Object.entries(dimensionPreferences).filter(([, value]) => value)),
        }),
      }),
    onSuccess: (data) => setRevealed(data),
  })

  const canSubmit = useMemo(() => Boolean(preference) && !submit.isPending, [preference, submit.isPending])

  if (!sessionId) {
    return (
      <div className="p-6">
        <Card>
          <div className="text-sm text-amber-300" data-testid="ab-blocked">
            <div className="font-semibold">AB_BLOCKED_NO_REAL_MODEL_PLAN</div>
            <div className="mt-1 text-xs text-slate-400">
              当前没有可配对的真实模型方案。A/B 盲评需要「离线规则方案」与「真实模型方案」
              同输入、同目标时长、且模型方案带有真实调用回执。
              系统不会用 Mock 或离线方案冒充模型方案。
            </div>
          </div>
        </Card>
      </div>
    )
  }

  if (blind.isLoading) return <div className="p-6"><Spinner /></div>

  if (blocked) {
    return (
      <div className="p-6">
        <Card>
          <div className="text-sm text-amber-300" data-testid="ab-blocked">
            <div className="font-semibold">
              {blocked.detail?.code === 'AB_BLOCKED_NO_REAL_MODEL_PLAN'
                ? 'AB_BLOCKED_NO_REAL_MODEL_PLAN'
                : blocked.detail?.code ?? `HTTP_${blocked.status}`}
            </div>
            <div className="mt-1 text-xs text-slate-400">
              A/B 未开展。不会用离线或 Mock 方案冒充真实模型对比。
            </div>
          </div>
        </Card>
      </div>
    )
  }

  const data = revealed ?? blind.data
  const result = data?.revealed ? data : null

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/evaluation/footage`}
              className="text-sm text-slate-400 hover:text-slate-200">← 镜头核对</Link>
        <h1 className="text-lg font-semibold text-slate-200">故事线盲评</h1>
        <span className="text-xs text-slate-500">
          {result ? '已提交 · 来源已揭示' : '两侧来源隐藏，提交后揭示'}
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        {!result && (
          <Card>
            <div className="text-xs text-slate-400" data-testid="blind-note">
              请按画面、截取区间和统一的节奏与连接提示评分。
              提交后可以查看方案来源和原始解释。
            </div>
          </Card>
        )}

        <div className="grid gap-3 md:grid-cols-2">
          {data && (
            <SidePanel side={data.A} scores={result?.submission.scores?.A ?? a} disabled={Boolean(result)} projectId={id}
                       onScore={(dim, v) => setA((s) => ({ ...s, [dim]: v }))} />
          )}
          {data && (
            <SidePanel side={data.B} scores={result?.submission.scores?.B ?? b} disabled={Boolean(result)} projectId={id}
                       onScore={(dim, v) => setB((s) => ({ ...s, [dim]: v }))} />
          )}
        </div>

        {!result && (
          <Card>
            <div className="mb-2 grid gap-2 sm:grid-cols-2">
              {DIMENSIONS.map(({ key, label }) => (
                <label key={key} className="text-xs text-slate-400">{label}
                  <select className={inputClass} value={dimensionPreferences[key] ?? ''}
                          onChange={(e) => setDimensionPreferences((old) => ({ ...old, [key]: e.target.value }))}>
                    <option value="">未评价</option>
                    {CHOICES.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
                  </select>
                </label>
              ))}
            </div>
            <div className="mb-2 text-xs text-slate-400">总体偏好</div>
            <div className="flex flex-wrap gap-2">
              {CHOICES
                .map(([v, label]) => (
                  <Button key={v} variant={preference === v ? 'primary' : 'ghost'}
                          data-testid={`pref-${v}`}
                          onClick={() => setPreference(v)}>
                    {label}
                  </Button>
                ))}
            </div>
            <div className="mt-3">
              <Button data-testid="submit-ab" disabled={!canSubmit}
                      onClick={() => submit.mutate()}>
                提交盲评并揭示来源
              </Button>
            </div>
            {submit.isError && (() => {
              const err = submit.error as {
                status?: number
                detail?: { code?: string }
              }
              const code = err?.detail?.code
              // A second submit is not a failure: the first one already counted and
              // the record is immutable. Say so, and push the reviewer to the
              // revealed result instead of asking them to retry something that can
              // never succeed.
              if (code === 'AB_ALREADY_SUBMITTED') {
                return (
                  <div className="mt-2 text-xs text-amber-300" data-testid="already-submitted">
                    这次盲评已经提交过了，结果不可修改。
                    <button
                      className="ml-2 underline"
                      data-testid="reload-reveal"
                      onClick={() => blind.refetch()}>
                      查看已提交的结果
                    </button>
                  </div>
                )
              }
              return (
                <div className="mt-2 text-xs text-red-400" data-testid="submit-error">
                  提交失败：{code ?? `HTTP_${err?.status ?? '?'}`}
                  {err?.status && err.status >= 500 ? '，请稍后重试' : ''}
                </div>
              )
            })()}
          </Card>
        )}

        {result && (
          <Card>
            <div className="text-sm text-slate-200" data-testid="reveal-panel">
              <div>
                你的偏好：<strong>{result.submission.preference}</strong>
                {result.winner_mode && (
                  <> → <strong>{result.winner_mode === 'model' ? '真实模型方案' : '离线规则方案'}</strong></>
                )}
              </div>
              <div className="mt-1 text-xs text-slate-500" data-testid="mapping">
                A = {result.mapping.A === 'model' ? '真实模型方案' : '离线规则方案'} ·
                B = {result.mapping.B === 'model' ? '真实模型方案' : '离线规则方案'}
              </div>
              <div className="mt-2 text-xs text-amber-300" data-testid="single-reviewer-note">
                {result.interpretation}
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  )
}
