/**
 * TSB-369-C — read-only draft story-plan page.
 *
 * The page is intentionally a *preview*, not an editor for the real storyboard:
 * it can reorder and re-trim shots in memory, recompute the timeline, and export
 * a guidance document, but it never posts the plan back, never saves a version,
 * and never starts a job. There is no "apply" button because there is no apply
 * semantics this round.
 *
 * Two honesty rules drive the UI:
 *   1. Coverage is always shown as a fraction, never a bare percentage, and the
 *      "no model reading" count is always visible — the draft is built from
 *      partial evidence and must look like it.
 *   2. Every factual line a shot claims is rendered from the structured `claims`
 *      with its evidence badge, so a suggestion cannot be mistaken for an
 *      observation.
 */
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type {
  DirectorCandidatesOut,
  PlanClaim,
  PlanDraft,
  PlanInputStatus,
  PlanPreviewRequest,
  PlanShot,
  PlanVariant,
} from '../lib/types'
import { Badge, Button, Card, Field, Spinner, inputClass } from '../components/ui'

const KIND_LABEL: Record<string, string> = {
  observed_signal: '观察',
  human_supplied: '人工',
  editorial_suggestion: '建议',
  unknown: '未知',
}

const KIND_TONE: Record<string, string> = {
  observed_signal: 'blue',
  human_supplied: 'green',
  editorial_suggestion: 'slate',
  unknown: 'yellow',
}

const CHANNEL_LABEL: Record<string, string> = {
  static: '静帧',
  multiframe: '多帧',
  video_native: '原生视频',
}

const ROLE_LABEL: Record<string, string> = {
  opening: '开场',
  development: '展开',
  turning_point: '转折',
  climax: '高潮',
  closing: '收束',
}

/** Recompute the timeline after a local edit. Never mutates the source data. */
function retime(shots: PlanShot[]): PlanShot[] {
  let t = 0
  return shots.map((s, i) => {
    const duration = Math.max(0, s.source_out_sec - s.source_in_sec)
    const next = { ...s, shot_id: `${s.shot_id.split('#')[0]}#${i}`, timeline_start_sec: round(t), planned_duration_sec: round(duration) }
    t = round(t + duration)
    return next
  })
}

const round = (n: number) => Math.round(n * 1000) / 1000

function ClaimLine({ claim }: { claim: PlanClaim }) {
  return (
    <div className="mb-1 text-xs" data-testid="claim">
      <span data-testid={`claim-kind-${claim.kind}`}>
        <Badge tone={KIND_TONE[claim.kind] ?? 'slate'}>{KIND_LABEL[claim.kind] ?? claim.kind}</Badge>
      </span>{' '}
      <span className="text-slate-300">{claim.text}</span>
      {claim.evidence_refs.length > 0 && (
        <span className="ml-2 text-slate-500">
          ← {claim.evidence_refs.map((r) => CHANNEL_LABEL[r.channel] ?? r.channel).join(', ')}
        </span>
      )}
    </div>
  )
}

function ShotRow({
  shot,
  index,
  total,
  onMove,
  onTrim,
}: {
  shot: PlanShot
  index: number
  total: number
  onMove: (from: number, to: number) => void
  onTrim: (index: number, delta: number) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded border border-ink-700 p-2 text-xs" data-testid={`plan-shot-${index}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-slate-500" data-testid="shot-index">#{index + 1}</span>
        <span className="text-slate-300">{shot.segment_id.slice(0, 8)}</span>
        <Badge tone="blue">{ROLE_LABEL[shot.narrative_role] ?? shot.narrative_role}</Badge>
        <span className="text-slate-500" data-testid="shot-window">
          {shot.source_in_sec.toFixed(1)}s–{shot.source_out_sec.toFixed(1)}s
        </span>
        <span className="text-slate-400" data-testid="shot-duration">
          {shot.planned_duration_sec.toFixed(1)}s
        </span>
        <span className="text-slate-500">时间轴 {shot.timeline_start_sec.toFixed(1)}s</span>
        <span className="ml-auto flex gap-1">
          <Button variant="ghost" data-testid="move-up" disabled={index === 0}
                  onClick={() => onMove(index, index - 1)}>↑</Button>
          <Button variant="ghost" data-testid="move-down" disabled={index === total - 1}
                  onClick={() => onMove(index, index + 1)}>↓</Button>
          <Button variant="ghost" data-testid="trim-shorter"
                  onClick={() => onTrim(index, -0.5)}>短 0.5s</Button>
          <Button variant="ghost" data-testid="toggle-detail"
                  onClick={() => setOpen((v) => !v)}>{open ? '收起' : '证据'}</Button>
        </span>
      </div>
      <div className="mt-1 text-slate-400" data-testid="selection-reason">{shot.selection_reason}</div>
      {shot.needs_review.length > 0 && (
        <div className="mt-1 text-amber-300" data-testid="shot-needs-review">
          待复核：{shot.needs_review.join('；')}
        </div>
      )}
      {open && (
        <div className="mt-2 border-t border-ink-700 pt-2" data-testid="shot-evidence">
          {shot.claims.length === 0 ? (
            <div className="text-slate-500" data-testid="no-claims">
              该镜头没有任何模型读数，因此没有画面描述。
            </div>
          ) : (
            shot.claims.map((c, i) => <ClaimLine key={i} claim={c} />)
          )}
          <div className="mt-1 text-slate-500">
            转场建议：{shot.transition_hint || '（无）'} · 节奏建议：{shot.pacing_hint || '（无）'}
          </div>
        </div>
      )}
    </div>
  )
}

function VariantCard({
  variant,
  onEdit,
}: {
  variant: PlanVariant
  onEdit: (variant: PlanVariant) => void
}) {
  const move = (from: number, to: number) => {
    const next = [...variant.shots]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    onEdit({ ...variant, shots: retime(next) })
  }
  const trim = (index: number, delta: number) => {
    const next = variant.shots.map((s, i) => {
      if (i !== index) return s
      const out = Math.max(s.source_in_sec + 0.5, round(s.source_out_sec + delta))
      return { ...s, source_out_sec: out }
    })
    onEdit({ ...variant, shots: retime(next) })
  }
  const total = variant.shots.reduce((acc, s) => acc + s.planned_duration_sec, 0)

  return (
    <Card className="mb-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-slate-200" data-testid="variant-title">
          {variant.title}
        </h3>
        <Badge tone={variant.status === 'provisional' ? 'yellow' : 'green'}>{variant.status}</Badge>
        <span className="text-xs text-slate-500">{variant.narrative_angle}</span>
        <span className="ml-auto text-xs text-slate-400" data-testid="variant-total">
          合计 {round(total).toFixed(1)}s / 目标 {variant.target_duration_sec}s
        </span>
      </div>
      <div className="mb-2 text-xs text-slate-400">{variant.logline}</div>
      {variant.differences.length > 0 && (
        <div className="mb-2 text-xs text-slate-500" data-testid="variant-differences">
          与其他方案的差异：{variant.differences.join('；')}
        </div>
      )}
      <div className="space-y-2">
        {variant.shots.map((s, i) => (
          <ShotRow key={s.shot_id} shot={s} index={i} total={variant.shots.length}
                   onMove={move} onTrim={trim} />
        ))}
      </div>
      {variant.review_items.length > 0 && (
        <div className="mt-2 text-xs text-amber-300" data-testid="variant-review">
          需人工复核 {variant.review_items.length} 项
        </div>
      )}
    </Card>
  )
}

/**
 * TSB-370: remount on project change.
 *
 * The form holds a draft, an error and a bound digest. Clearing them in an effect
 * works but causes a cascading-render lint error and, worse, leaves one frame
 * where the previous project's draft is still visible under the new id. A keyed
 * remount makes "never crosses projects" structural instead of temporal.
 */
export default function DirectorPlans() {
  const { id = '' } = useParams()
  return <DirectorPlansForm key={id} />
}

function DirectorPlansForm() {
  const { id = '' } = useParams()
  const [theme, setTheme] = useState('')
  const [target, setTarget] = useState(90)
  const [style, setStyle] = useState<PlanPreviewRequest['style']>('chronological')
  const [variantCount, setVariantCount] = useState(2)
  const [maxCandidates, setMaxCandidates] = useState(40)
  const [draft, setDraft] = useState<PlanDraft | null>(null)
  const [edited, setEdited] = useState<PlanVariant[] | null>(null)
  // TSB-370-0: the draft must be bound to the exact input it was built from.
  const [staleDigest, setStaleDigest] = useState<string | null>(null)
  const [error, setError] = useState<{ code: string; message: string } | null>(null)

  const ctx = useQuery({
    queryKey: ['director-context', id],
    queryFn: () => api<DirectorCandidatesOut>(
      `${endpoints.directorCandidates(id)}?eligibility=all&limit=100`),
    enabled: Boolean(id),
  })

  // The digest comes from the read-only input-status endpoint, which runs the
  // same assembly the planner does. Deriving it from /director/candidates
  // coverage (the previous shape of this code) was not a digest at all.
  const status = useQuery({
    queryKey: ['plan-input-status', id, maxCandidates],
    queryFn: () => api<PlanInputStatus>(
      `${endpoints.directorPlanInputStatus(id)}?max_candidates=${maxCandidates}`),
    enabled: Boolean(id),
    // Always refetch: a stale digest is exactly the bug this guards against.
    staleTime: 0,
    refetchOnWindowFocus: true,
  })

  const inputDigest = status.data?.input_digest ?? null
  const digestReady = Boolean(inputDigest)

  const preview = useMutation({
    mutationFn: () => {
      const body: PlanPreviewRequest = {
        expected_input_digest: inputDigest,
        max_candidates: maxCandidates,
        theme,
        target_duration_sec: target,
        style,
        variant_count: variantCount,
        planning_mode: 'offline_rules',
      }
      return api<PlanDraft>(endpoints.directorPlanPreview(id), {
        method: 'POST',
        body: JSON.stringify(body),
      })
    },
    onSuccess: (data) => {
      setDraft(data)
      setEdited(null)
      setError(null)
    },
    onError: (err) => {
      const anyErr = err as { status?: number; message?: string; detail?: { code?: string } }
      const code = anyErr.detail?.code ?? `HTTP_${anyErr.status ?? '?'}`
      // A conflict must never leave the old draft on screen pretending to match
      // the new evidence.
      if (code === 'DIRECTOR_INPUT_CHANGED' || anyErr.status === 409) {
        setStaleDigest(inputDigest)
        setDraft(null)
        setEdited(null)
      }
      setError({ code, message: anyErr.message ?? '生成草案失败' })
    },
  })

  const variants = edited ?? draft?.variants ?? []

  const exportText = useMemo(() => {
    if (!draft) return ''
    return buildMarkdown(draft, variants)
  }, [draft, variants])

  // Coverage must be truthful *before* the first plan exists, otherwise the form
  // looks like it is planning over an empty library.
  const coverage = draft?.coverage ?? (ctx.data?.coverage
    ? {
        library_segments: ctx.data.coverage.library_segments,
        segments_with_any_valid_reading: ctx.data.coverage.segments_with_any_valid_reading,
        no_valid_model_reading: ctx.data.coverage.no_valid_model_reading,
        video_native_valid: ctx.data.coverage.video_native_valid,
      }
    : {
        library_segments: 0, segments_with_any_valid_reading: 0,
        no_valid_model_reading: 0, video_native_valid: 0,
      })

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/director`} className="text-sm text-slate-400 hover:text-slate-200">
          ← 返回候选镜头
        </Link>
        <h1 className="text-lg font-semibold text-slate-200">剪辑结构草案</h1>
        <span className="text-xs text-slate-500">
          只读 · 离线规则生成 · 不是 AI 看过素材后的结论
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        <Card>
          <div className="mb-2 text-xs text-slate-400" data-testid="coverage-line">
            素材库 {coverage.library_segments} 个镜头 ·
            有可靠读数 {coverage.segments_with_any_valid_reading} 个 ·
            无有效读数 {coverage.no_valid_model_reading} 个 ·
            原生视频有效 {coverage.video_native_valid} 个
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="想表达的主题">
              <input className={inputClass} value={theme} data-testid="input-theme"
                     onChange={(e) => setTheme(e.target.value)} placeholder="例如：出发到抵达的情绪变化" />
            </Field>
            <Field label="目标时长（秒）">
              <input className={inputClass} type="number" value={target} data-testid="input-target"
                     onChange={(e) => setTarget(Number(e.target.value) || 1)} />
            </Field>
            <Field label="结构偏好">
              <select className={inputClass} value={style} data-testid="input-style"
                      onChange={(e) => setStyle(e.target.value as PlanPreviewRequest['style'])}>
                <option value="chronological">按行程顺序</option>
                <option value="highlights_first">重点优先</option>
                <option value="atmospheric">氛围优先</option>
              </select>
            </Field>
            <Field label="候选镜头上限">
              <select className={inputClass} value={maxCandidates} data-testid="input-max-candidates"
                      onChange={(e) => setMaxCandidates(Number(e.target.value))}>
                <option value={20}>20</option>
                <option value={40}>40</option>
                <option value={60}>60</option>
                <option value={100}>100</option>
              </select>
            </Field>
            <Field label="方案数">
              <select className={inputClass} value={variantCount} data-testid="input-variants"
                      onChange={(e) => setVariantCount(Number(e.target.value))}>
                <option value={1}>1</option>
                <option value={2}>2</option>
                <option value={3}>3</option>
              </select>
            </Field>
            <Button data-testid="generate"
                    disabled={preview.isPending || !digestReady}
                    onClick={() => preview.mutate()}>
              {preview.isPending ? '生成中…' : '开始离线策划'}
            </Button>
          </div>
          <div className="mt-2 text-xs text-slate-500" data-testid="readonly-note">
            本页不保存、不应用、不上传。刷新会丢失未导出的草案。
          </div>
          <div className="mt-1 text-xs text-slate-500" data-testid="digest-state">
            {status.isError
              ? '无法读取输入摘要，已禁用生成。'
              : digestReady
                ? `输入摘要 ${inputDigest?.slice(0, 12)}… · 已绑定当前证据`
                : '正在读取输入摘要…'}
          </div>
          {staleDigest && (
            <div className="mt-1 text-xs text-amber-300" data-testid="stale-banner">
              素材或人工选择在上次生成后发生变化，旧草案已清除。
              <button className="ml-2 underline" data-testid="reload-input"
                      onClick={() => { status.refetch(); setStaleDigest(null) }}>
                重新加载证据
              </button>
            </div>
          )}
        </Card>

        {ctx.isError && (
          <Card>
            <div className="text-sm text-red-400" data-testid="context-error">
              加载项目素材失败。
            </div>
          </Card>
        )}

        {error && (
          <Card>
            <div className="text-sm text-red-400" data-testid="plan-error">
              错误码 {error.code}：{error.message}
              {(error.code === 'DIRECTOR_INPUT_CHANGED' || error.code === 'HTTP_409') && (
                <div className="mt-1 text-xs text-slate-400" data-testid="conflict-hint">
                  素材或人工选择已变化，请重新获取后再生成。你填写的主题与时长已保留。
                </div>
              )}
            </div>
          </Card>
        )}

        {preview.isPending && <Spinner />}

        {draft && draft.generation_status === 'insufficient_evidence' && (
          <Card>
            <div className="text-sm text-amber-300" data-testid="insufficient">
              证据不足：这个项目没有足够的合格镜头来形成剪辑结构。系统没有编造任何镜头。
            </div>
          </Card>
        )}

        {draft && variants.length > 0 && (
          <>
            <div className="text-xs text-slate-500" data-testid="draft-status">
              草案状态：{draft.generation_status} · 生成方式：{draft.planning_mode}
              {draft.warnings.length > 0 && (
                <span className="ml-2 text-amber-300">{draft.warnings.join('；')}</span>
              )}
            </div>
            {variants.map((v, i) => (
              <VariantCard key={v.variant_id} variant={v} onEdit={(nv) => {
                const base = edited ?? draft.variants
                const next = [...base]
                next[i] = nv
                setEdited(next)
              }} />
            ))}
            <Card>
              <div className="mb-2 text-xs text-slate-400">导出剪辑指导（只读，不写回系统）</div>
              <div className="flex gap-2">
                <Button data-testid="export-json" onClick={() => download('tsb369-plan.json',
                  JSON.stringify({ ...draft, variants }, null, 2), 'application/json')}>
                  导出 JSON
                </Button>
                <Button data-testid="export-md" onClick={() => download('tsb369-plan.md',
                  exportText, 'text/markdown')}>
                  导出 Markdown
                </Button>
              </div>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs text-slate-500"
                   data-testid="export-preview">{exportText.slice(0, 1200)}</pre>
            </Card>
          </>
        )}
      </div>
    </div>
  )
}

function download(name: string, content: string, type: string) {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

export function buildMarkdown(draft: PlanDraft, variants: PlanVariant[]): string {
  const lines: string[] = []
  lines.push('# 剪辑结构草案')
  lines.push('')
  lines.push(`- 项目：${draft.project_id}`)
  lines.push(`- 生成方式：${draft.planning_mode}（离线规则，不是 AI 看过素材后的结论）`)
  lines.push(`- 草案状态：${draft.generation_status}`)
  lines.push(`- 输入摘要：\`${draft.source_input_digest}\``)
  lines.push(
    `- 覆盖：素材库 ${draft.coverage.library_segments} 个镜头，` +
    `有可靠读数 ${draft.coverage.segments_with_any_valid_reading} 个，` +
    `无有效读数 ${draft.coverage.no_valid_model_reading} 个，` +
    `原生视频有效 ${draft.coverage.video_native_valid} 个`,
  )
  if (draft.limitations.length) lines.push(`- 限制：${draft.limitations.join('；')}`)
  lines.push('')
  for (const v of variants) {
    lines.push(`## ${v.title}`)
    lines.push('')
    lines.push(`${v.logline}`)
    lines.push('')
    lines.push(`状态：${v.status} · 角度：${v.narrative_angle}`)
    lines.push('')
    for (const [i, s] of v.shots.entries()) {
      lines.push(
        `${i + 1}. **${s.segment_id}** ${ROLE_LABEL[s.narrative_role] ?? s.narrative_role} · ` +
        `源片 ${s.source_in_sec.toFixed(1)}–${s.source_out_sec.toFixed(1)}s · ` +
        `时长 ${s.planned_duration_sec.toFixed(1)}s · 时间轴 ${s.timeline_start_sec.toFixed(1)}s`,
      )
      lines.push(`   - 选择理由：${s.selection_reason}`)
      for (const c of s.claims) {
        const refs = c.evidence_refs.length
          ? `（证据：${c.evidence_refs.map((r) => r.channel).join(', ')}）`
          : ''
        lines.push(`   - [${KIND_LABEL[c.kind] ?? c.kind}] ${c.text}${refs}`)
      }
      if (s.needs_review.length) lines.push(`   - 待复核：${s.needs_review.join('；')}`)
      lines.push('')
    }
  }
  lines.push('---')
  lines.push('本草案为只读建议，未写入任何正式剪辑计划。')
  return lines.join('\n')
}
