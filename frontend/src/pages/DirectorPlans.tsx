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
import { api, endpoints, fetchAllSegments, mediaUrl } from '../lib/api'
import type {
  DirectorCandidatesOut,
  PlanClaim,
  PlanDraft,
  PlanInputStatus,
  PlanPreviewRequest,
  PlanShot,
  PlanVariant,
  SegmentListItem,
  AbBlindView,
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

type ExecutionShot = PlanShot & { editor_note?: string; retained?: boolean }
type ExecutionVariant = Omit<PlanVariant, 'shots'> & { shots: ExecutionShot[] }
type ExecutionVersion = {
  id: string; saved_at: string; original: PlanDraft; variants: ExecutionVariant[]; theme: string
}

function readVersions(projectId: string): ExecutionVersion[] {
  const raw = localStorage.getItem(`tsb.director.execution.${projectId}`)
  if (!raw) return []
  const parsed = JSON.parse(raw) as ExecutionVersion[]
  if (!Array.isArray(parsed) || parsed.some((v) => v.original?.project_id !== projectId)) {
    throw new Error('执行版本与当前项目不匹配')
  }
  return parsed
}

function compareExecution(original: PlanVariant, edited: ExecutionVariant) {
  const previous = new Map(original.shots.map((s, i) => [s.shot_id, { shot: s, index: i }]))
  const editedIds = new Set(edited.shots.map((s) => s.shot_id))
  const changes = original.shots.flatMap((shot, i) =>
    editedIds.has(shot.shot_id) ? [] : [`移除原镜头 #${i + 1}`])
  edited.shots.forEach((shot, index) => {
    const old = previous.get(shot.shot_id)
    if (!old) return
    const label = `镜头 #${old.index + 1}`
    if (old.index !== index) changes.push(`${label} → #${index + 1}`)
    if (old.shot.source_in_sec !== shot.source_in_sec || old.shot.source_out_sec !== shot.source_out_sec) {
      changes.push(`${label} 截取区间：${old.shot.source_in_sec}–${old.shot.source_out_sec}s → ${shot.source_in_sec}–${shot.source_out_sec}s`)
    }
    if (shot.editor_note) changes.push(`${label} 备注：${shot.editor_note}`)
    if (shot.retained) changes.push(`${label} 已保留`)
  })
  return changes
}

/** Recompute the timeline after a local edit. Never mutates the source data. */
function retime(shots: ExecutionShot[]): ExecutionShot[] {
  let t = 0
  return shots.map((s) => {
    const duration = Math.max(0, s.source_out_sec - s.source_in_sec)
    const next = { ...s, timeline_start_sec: round(t), planned_duration_sec: round(duration) }
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
  projectId,
  source,
  onUpdate,
  onRemove,
}: {
  shot: ExecutionShot
  index: number
  total: number
  onMove: (from: number, to: number) => void
  onTrim: (index: number, delta: number) => void
  projectId: string
  source?: SegmentListItem
  onUpdate: (change: Partial<ExecutionShot>) => void
  onRemove: () => void
}) {
  const [open, setOpen] = useState(false)
  const [windowError, setWindowError] = useState(false)
  const editBoundary = (key: 'source_in_sec' | 'source_out_sec', raw: string) => {
    const value = raw.trim() ? Number(raw) : NaN
    const start = key === 'source_in_sec' ? value : shot.source_in_sec
    const end = key === 'source_out_sec' ? value : shot.source_out_sec
    const valid = source?.status !== 'needs_review' && Number.isFinite(start)
      && Number.isFinite(end) && start >= (source?.start_sec ?? shot.source_in_sec)
      && end <= (source?.end_sec ?? shot.source_out_sec) && end - start >= 0.05
    setWindowError(!valid)
    if (valid) onUpdate({ [key]: value })
  }
  return (
    <div className="rounded border border-ink-700 p-2 text-xs" data-testid={`plan-shot-${index}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-slate-500" data-testid="shot-index">#{index + 1}</span>
        {source?.thumbnail && <img className="h-12 w-20 rounded object-cover"
          src={mediaUrl(projectId, source.thumbnail)} alt="镜头缩略图" loading="lazy" />}
        <span className="text-slate-300" title={shot.segment_id}>
          {source?.asset_display_name ?? shot.asset_id} · {shot.segment_id.slice(0, 8)}
        </span>
        <Badge tone="blue">{ROLE_LABEL[shot.narrative_role] ?? shot.narrative_role}</Badge>
        <span className="text-slate-500" data-testid="shot-window">
          {shot.source_in_sec.toFixed(3)}s–{shot.source_out_sec.toFixed(3)}s
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
          <Button variant="ghost" data-testid="trim-shorter" disabled={shot.retained || shot.planned_duration_sec <= 0.5}
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
          <div className="mt-2 flex flex-wrap items-end gap-2">
            {(['source_in_sec', 'source_out_sec'] as const).map((key) => (
              <label key={key}>{key === 'source_in_sec' ? '源片入点（秒）' : '源片出点（秒）'}
                <input key={shot[key]} type="number" step="0.001" disabled={shot.retained}
                       className={inputClass} defaultValue={shot[key]}
                       onBlur={(e) => {
                         editBoundary(key, e.target.value)
                         e.target.value = String(shot[key])
                       }} />
              </label>
            ))}
            <label>剪辑备注
              <input className={inputClass} value={shot.editor_note ?? ''} maxLength={400}
                     onChange={(e) => onUpdate({ editor_note: e.target.value })} />
            </label>
            <Button variant="ghost" onClick={() => onUpdate({ retained: !shot.retained })}>
              {shot.retained ? '解除保留' : '保留镜头'}
            </Button>
            <Button variant="ghost" disabled={shot.retained} onClick={onRemove}>移出方案</Button>
          </div>
          {windowError && <p className="text-amber-300" role="alert">截取区间必须位于当前可用镜头范围内。</p>}
        </div>
      )}
    </div>
  )
}

function VariantCard({
  variant,
  onEdit,
  originals,
  projectId,
  sources,
}: {
  variant: ExecutionVariant
  onEdit: (variant: ExecutionVariant) => void
  originals: PlanVariant | undefined
  projectId: string
  sources: Map<string, SegmentListItem>
}) {
  const update = (shots: ExecutionShot[]) => {
    const timed = retime(shots)
    const total = round(timed.reduce((acc, shot) => acc + shot.planned_duration_sec, 0))
    onEdit({ ...variant, shots: timed, total_duration_sec: total,
      duration_gap_sec: round(variant.target_duration_sec - total),
      review_items: variant.review_items.filter((item) =>
        timed.some((shot) => shot.segment_id === item.segment_id)) })
  }
  const move = (from: number, to: number) => {
    const next = [...variant.shots]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    update(next)
  }
  const trim = (index: number, delta: number) => {
    const next = variant.shots.map((s, i) => {
      if (i !== index) return s
      const out = Math.max(s.source_in_sec + 0.5, round(s.source_out_sec + delta))
      return { ...s, source_out_sec: out }
    })
    update(next)
  }
  const total = variant.shots.reduce((acc, s) => acc + s.planned_duration_sec, 0)
  const changes = originals ? compareExecution(originals, variant) : []

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
      <div className="mb-2 flex flex-wrap gap-2 text-xs text-slate-400">
        {Object.entries(ROLE_LABEL).map(([role, label]) => {
          const indices = variant.shots.flatMap((shot, index) => shot.narrative_role === role ? [index + 1] : [])
          return <span key={role}>{label} {indices.length ? indices.map((i) => `#${i}`).join('、') : '未安排'}</span>
        })}
      </div>
      {variant.differences.length > 0 && (
        <div className="mb-2 text-xs text-slate-500" data-testid="variant-differences">
          与其他方案的差异：{variant.differences.join('；')}
        </div>
      )}
      <div className="space-y-2">
        {variant.shots.map((s, i) => (
          <ShotRow key={s.shot_id} shot={s} index={i} total={variant.shots.length}
                   onMove={move} onTrim={trim} projectId={projectId} source={sources.get(s.segment_id)}
                   onUpdate={(change) => update(variant.shots.map((shot) =>
                     shot.shot_id === s.shot_id ? { ...shot, ...change } : shot))}
                   onRemove={() => update(variant.shots.filter((shot) => shot.shot_id !== s.shot_id))} />
        ))}
      </div>
      {changes.length > 0 && <ul className="mt-2 text-xs text-amber-300" data-testid="execution-diff">
        {changes.map((change, i) => <li key={i}>{change}</li>)}
      </ul>}
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
  const [maxCandidates, setMaxCandidates] = useState(100)
  const [draft, setDraft] = useState<PlanDraft | null>(null)
  const [edited, setEdited] = useState<ExecutionVariant[] | null>(null)
  const [versions, setVersions] = useState<ExecutionVersion[]>(() => {
    try { return readVersions(id) } catch { return [] }
  })
  const [workingTheme, setWorkingTheme] = useState('')
  const [generationRequest, setGenerationRequest] = useState<PlanPreviewRequest | null>(null)
  const [uploadAcknowledged, setUploadAcknowledged] = useState(false)
  const [reviewer, setReviewer] = useState('')
  const [versionMessage, setVersionMessage] = useState('')
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
    mutationFn: async (mode: 'offline_rules' | 'model' = 'offline_rules') => {
      const body: PlanPreviewRequest = {
        expected_input_digest: inputDigest,
        max_candidates: maxCandidates,
        theme,
        target_duration_sec: target,
        style,
        variant_count: variantCount,
        planning_mode: mode,
        ...(mode === 'model' ? { upload_acknowledged: uploadAcknowledged } : {}),
      }
      const data = await api<PlanDraft>(endpoints.directorPlanPreview(id), {
        method: 'POST',
        body: JSON.stringify(body),
      })
      return { data, body }
    },
    onSuccess: ({ data, body }) => {
      setDraft(data)
      setEdited(null)
      setError(null)
      setGenerationRequest(body)
      setWorkingTheme(body.theme ?? '')
      setUploadAcknowledged(false)
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

  const pair = useMutation({
    mutationFn: async () => {
      if (!draft || !generationRequest) throw new Error('请重新生成模型方案')
      const offline = await api<PlanDraft>(endpoints.directorPlanPreview(id), {
        method: 'POST',
        body: JSON.stringify({ ...generationRequest, planning_mode: 'offline_rules',
          upload_acknowledged: false }),
      })
      return api<AbBlindView>(endpoints.evaluationAbSessions(id), {
        method: 'POST',
        body: JSON.stringify({ offline_plan: offline, model_plan: draft, reviewer }),
      })
    },
  })
  const sourceSegments = useQuery({
    queryKey: ['plan-source-segments', id],
    queryFn: () => fetchAllSegments(id),
    enabled: Boolean(id && draft?.variants.length),
  })
  const sources = useMemo(() => new Map(
    (sourceSegments.data ?? []).map((s) => [s.id, s])), [sourceSegments.data])

  const variants = useMemo(() => edited ?? draft?.variants ?? [], [edited, draft])

  const exportText = useMemo(() => {
    if (!draft) return ''
    return buildMarkdown(draft, variants, sources, workingTheme)
  }, [draft, variants, sources, workingTheme])

  const saveVersion = () => {
    if (!draft || !edited) return
    try {
      const version = { id: crypto.randomUUID(), saved_at: new Date().toISOString(),
        original: draft, variants: edited, theme: workingTheme }
      const next = [...readVersions(id), version]
      localStorage.setItem(`tsb.director.execution.${id}`, JSON.stringify(next))
      setVersions(next)
      setVersionMessage(`已保存本机执行版本 ${next.length}`)
    } catch {
      setVersionMessage('无法保存本机版本。当前编辑仍可导出。')
    }
  }

  const openVersion = (version: ExecutionVersion) => {
    setDraft(version.original)
    setEdited(version.variants)
    setWorkingTheme(version.theme)
    setGenerationRequest(null)
    pair.reset()
  }

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
        <span className="text-xs text-slate-500">剪辑执行建议</span>
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
                    onClick={() => { pair.reset(); preview.mutate('offline_rules') }}>
              {preview.isPending ? '生成中…' : '开始离线策划'}
            </Button>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <label className="text-xs text-slate-400">
              <input type="checkbox" checked={uploadAcknowledged}
                     onChange={(e) => setUploadAcknowledged(e.target.checked)} />
              {' '}授权本次发送主题与候选证据文本
            </label>
            <Button data-testid="generate-model" variant="ghost"
                    disabled={!uploadAcknowledged || preview.isPending || !digestReady}
                    onClick={() => { pair.reset(); preview.mutate('model') }}>生成模型方案</Button>
          </div>
          <div className="mt-2 text-xs text-slate-500" data-testid="readonly-note">
            执行版本保存在当前浏览器。此页不保存到正式故事板。
          </div>
          {status.data && <div className="text-xs text-slate-500">
            本次候选 {status.data.segment_count} 个
            {Boolean(status.data.withheld.candidate_budget) &&
              ` · 预算未纳入 ${status.data.withheld.candidate_budget} 个`}
          </div>}
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

        {versions.length > 0 && <Card>
          <div className="mb-2 text-xs text-slate-400">本机执行版本</div>
          {versions.map((version, index) => <Button key={version.id} variant="ghost"
            data-testid="open-execution-version" onClick={() => openVersion(version)}>
            版本 {index + 1} · {new Date(version.saved_at).toLocaleString()}
          </Button>)}
        </Card>}

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
            {workingTheme && <div className="text-sm text-slate-300">主题：{workingTheme}</div>}
            {draft.planning_mode === 'model' && generationRequest && <Card>
              <Field label="评价者标识（可选）">
                <input className={inputClass} value={reviewer} maxLength={64}
                       onChange={(e) => setReviewer(e.target.value)} />
              </Field>
              <Button variant="ghost" disabled={pair.isPending || preview.isPending}
                      onClick={() => pair.mutate()}>用原始方案创建同条件盲评</Button>
              {pair.data && <Link className="ml-2 text-accent"
                to={`/project/${id}/evaluation/ab?session=${encodeURIComponent(pair.data.session_id)}`}>打开盲评</Link>}
              {pair.isError && <div className="text-xs text-red-400" role="alert">{pair.error.message}</div>}
            </Card>}
            {variants.map((v, i) => (
              <VariantCard key={v.variant_id} variant={v} originals={draft.variants[i]}
                           projectId={id} sources={sources} onEdit={(nv) => {
                const base = edited ?? draft.variants
                const next = [...base]
                next[i] = nv
                setEdited(next)
              }} />
            ))}
            {(draft.withheld.excluded_segments > 0 || draft.withheld.needs_review_segments > 0) &&
              <div className="text-xs text-amber-300">
                人工排除 {draft.withheld.excluded_segments ?? 0} 个，待复核 {draft.withheld.needs_review_segments ?? 0} 个。
                <Link className="ml-2 underline" to={`/project/${id}/director`}>查看不建议使用的原因</Link>
              </div>}
            <Card>
              <div className="mb-2 text-xs text-slate-400">导出剪辑指导（只读，不写回系统）</div>
              <div className="flex gap-2">
                {edited && <>
                  <Button data-testid="save-execution-version" onClick={saveVersion}>保存本机执行版本</Button>
                  <Button variant="ghost" data-testid="show-original-version"
                          onClick={() => setEdited(null)}>查看原始方案</Button>
                </>}
                <Button data-testid="export-json" onClick={() => download('tsb369-plan.json',
                  JSON.stringify(edited ? { kind: 'execution_copy', original: draft, variants }
                    : draft, null, 2), 'application/json')}>
                  导出 JSON
                </Button>
                <Button data-testid="export-md" onClick={() => download('tsb369-plan.md',
                  exportText, 'text/markdown')}>
                  导出 Markdown
                </Button>
              </div>
              {versionMessage && <p className="text-xs text-slate-400" role="status">{versionMessage}</p>}
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

export function buildMarkdown(
  draft: PlanDraft, variants: ExecutionVariant[],
  sources = new Map<string, SegmentListItem>(), workingTheme = '',
): string {
  const lines: string[] = []
  lines.push('# 剪辑结构草案')
  lines.push('')
  lines.push(`- 项目：${draft.project_id}`)
  lines.push(draft.planning_mode === 'model'
    ? '- 生成方式：模型策划（基于已有镜头证据）'
    : '- 生成方式：offline_rules（离线规则，不是 AI 看过素材后的结论）')
  if (workingTheme) lines.push(`- 剪辑主题：${workingTheme}`)
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
    lines.push(`预计时长：${v.total_duration_sec.toFixed(3)}s / 目标 ${v.target_duration_sec}s`)
    for (const [role, title] of Object.entries(ROLE_LABEL)) {
      const orders = v.shots.flatMap((shot, index) => shot.narrative_role === role ? [index + 1] : [])
      lines.push(`- ${title}：${orders.length ? orders.map((i) => `镜头 #${i}`).join('、') : '未安排'}`)
    }
    lines.push('')
    for (const [i, s] of v.shots.entries()) {
      lines.push(
        `${i + 1}. **${s.segment_id}** ${ROLE_LABEL[s.narrative_role] ?? s.narrative_role} · ` +
        `源片 ${s.source_in_sec.toFixed(3)}–${s.source_out_sec.toFixed(3)}s · ` +
        `时长 ${s.planned_duration_sec.toFixed(3)}s · 时间轴 ${s.timeline_start_sec.toFixed(3)}s`,
      )
      lines.push(`   - 素材：${sources.get(s.segment_id)?.asset_display_name ?? s.asset_id} · ${s.asset_id}`)
      lines.push(`   - 选择理由：${s.selection_reason}`)
      if (s.transition_hint) lines.push(`   - 转场建议：${s.transition_hint}`)
      if (s.pacing_hint) lines.push(`   - 节奏建议：${s.pacing_hint}`)
      if (s.editor_note) lines.push(`   - 人工备注：${s.editor_note}`)
      if (s.retained) lines.push('   - 已保留')
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
