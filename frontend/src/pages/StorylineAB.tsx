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
import { api, endpoints } from '../lib/api'
import type { AbBlindView, AbRevealedView, AbSide } from '../lib/types'
import { Badge, Button, Card, Spinner } from '../components/ui'

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

function SidePanel({
  side, scores, onScore, disabled,
}: {
  side: AbSide
  scores: Record<string, number>
  onScore: (dim: string, value: number) => void
  disabled: boolean
}) {
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
              <span className="text-slate-400">
                {shot.source_in_sec.toFixed(1)}s–{shot.source_out_sec.toFixed(1)}s
              </span>
              <span className="text-slate-500">{shot.planned_duration_sec.toFixed(1)}s</span>
            </div>
            <div className="mt-1 text-slate-400">{shot.selection_reason}</div>
            {shot.claims.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-slate-500">
                {shot.claims.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            )}
          </div>
        ))}
      </div>

      <div className="mt-3 border-t border-ink-800 pt-2">
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
  const [revealed, setRevealed] = useState<AbRevealedView | null>(null)

  const blind = useQuery({
    queryKey: ['ab-blind', id, sessionId],
    queryFn: () => api<AbBlindView>(`${endpoints.evaluationAbSessions(id)}/${sessionId}`),
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

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/evaluation/footage`}
              className="text-sm text-slate-400 hover:text-slate-200">← 镜头核对</Link>
        <h1 className="text-lg font-semibold text-slate-200">故事线盲评</h1>
        <span className="text-xs text-slate-500">
          {revealed ? '已提交 · 来源已揭示' : '两侧来源隐藏，提交后揭示'}
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        {!revealed && (
          <Card>
            <div className="text-xs text-slate-400" data-testid="blind-note">
              两个方案使用相同的音乐/转场说明字段与相同排版，来源信息不显示。
              请先看完再评分，提交前可以反复修改。
            </div>
          </Card>
        )}

        <div className="grid gap-3 md:grid-cols-2">
          {data && (
            <SidePanel side={data.A} scores={a} disabled={Boolean(revealed)}
                       onScore={(dim, v) => setA((s) => ({ ...s, [dim]: v }))} />
          )}
          {data && (
            <SidePanel side={data.B} scores={b} disabled={Boolean(revealed)}
                       onScore={(dim, v) => setB((s) => ({ ...s, [dim]: v }))} />
          )}
        </div>

        {!revealed && (
          <Card>
            <div className="mb-2 text-xs text-slate-400">选镜头是否合理</div>
            <div className="flex flex-wrap gap-2">
              {[['A', 'A 更好'], ['B', 'B 更好'], ['TIE', '差不多'], ['NEITHER', '都不满意']]
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
            {submit.isError && (
              <div className="mt-2 text-xs text-red-400" data-testid="submit-error">
                提交失败：{(submit.error as { detail?: { code?: string } })?.detail?.code ?? '请重试'}
              </div>
            )}
          </Card>
        )}

        {revealed && (
          <Card>
            <div className="text-sm text-slate-200" data-testid="reveal-panel">
              <div>
                你的偏好：<strong>{revealed.submission.preference}</strong>
                {revealed.winner_mode && (
                  <> → <strong>{revealed.winner_mode === 'model' ? '真实模型方案' : '离线规则方案'}</strong></>
                )}
              </div>
              <div className="mt-1 text-xs text-slate-500" data-testid="mapping">
                A = {revealed.mapping.A === 'model' ? '真实模型方案' : '离线规则方案'} ·
                B = {revealed.mapping.B === 'model' ? '真实模型方案' : '离线规则方案'}
              </div>
              <div className="mt-2 text-xs text-amber-300" data-testid="single-reviewer-note">
                {revealed.interpretation}
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  )
}
