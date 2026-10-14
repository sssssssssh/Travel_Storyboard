import { useState } from 'react'
import type { SetStateAction } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, downloadUrl, endpoints, fetchAllSegments, mediaUrl, roughcutUrl } from '../lib/api'
import type { BuildPlanResult, EditPlanOut, JobOut, SegmentListItem } from '../lib/types'
import { formatTimeCode } from '../lib/format'
import { useJob } from '../lib/useJob'
import { Badge, Button, EmptyState, Spinner, inputClass } from '../components/ui'

interface DraftShot {
  asset_id: string
  segment_id: string
  source_in_sec: number
  source_out_sec: number
  target_duration_sec: number
  story_role: string
  transition_hint: string
  music_cue: string
  audio_hint: string
  color_hint: string
  review_status: string
  evidence: string
  confidence: number
}

const ROLES = ['', '开场', '铺垫', '过渡', '高潮', '结尾']

export default function Export() {
  const { id = '' } = useParams()
  return <ExportEditor key={id} id={id} />
}

function ExportEditor({ id }: { id: string }) {
  const qc = useQueryClient()
  const [editor, setEditor] = useState<{draft: DraftShot[]; dirty: boolean; server: EditPlanOut | null}>({
    draft: [], dirty: false, server: null,
  })
  const { draft, dirty } = editor
  function setDraft(value: SetStateAction<DraftShot[]>) {
    setEditor((current) => ({ ...current, dirty: true,
      draft: typeof value === 'function' ? value(current.draft) : value }))
  }
  const [result, setResult] = useState<BuildPlanResult | null>(null)
  const [roughcutJobId, setRoughcutJobId] = useState<string | null>(null)
  const roughcutJob = useJob(roughcutJobId)

  const segments = useQuery({
    queryKey: ['segments', id, 'export'],
    queryFn: () => fetchAllSegments(id),
  })

  const plan = useQuery({
    queryKey: ['edit-plan', id],
    queryFn: () => api<EditPlanOut>(endpoints.editPlan(id)),
  })

  function loadServer(data: EditPlanOut) {
    // Keep every persisted edit field; array position remains authoritative.
    return [...data.shots].sort((a, b) => a.order - b.order).map((shot) => ({
      asset_id: shot.asset_id, segment_id: shot.segment_id,
      source_in_sec: shot.source_in_sec, source_out_sec: shot.source_out_sec,
      target_duration_sec: shot.target_duration_sec, story_role: shot.story_role,
      transition_hint: shot.transition_hint, music_cue: shot.music_cue,
      audio_hint: shot.audio_hint, color_hint: shot.color_hint,
      evidence: shot.evidence, confidence: shot.confidence, review_status: shot.review_status,
    }))
  }
  if (plan.data && plan.data !== editor.server && !dirty) {
    setEditor({ draft: loadServer(plan.data), dirty: false, server: plan.data })
  }

  const save = useMutation({
    mutationFn: (shots: DraftShot[]) =>
      api<BuildPlanResult>(endpoints.editPlan(id), {
        method: 'POST',
        body: JSON.stringify({ shots: shots.map((s, i) => ({ order: i, ...s })) }),
      }),
    onSuccess: async (r, submitted) => {
      setResult(r)
      if (r.errors.length) return
      await qc.invalidateQueries({ queryKey: ['edit-plan', id] })
      setEditor((current) => current.draft === submitted ? { ...current, dirty: false } : current)
    },
  })

  const roughcut = useMutation({
    mutationFn: () => api<JobOut>(endpoints.createJob(id, 'roughcut'), { method: 'POST' }),
    onSuccess: (j) => setRoughcutJobId(j.id),
  })

  function addToPlan(s: SegmentListItem) {
    setDraft((d) => [
      ...d,
      {
        asset_id: s.asset_id,
        segment_id: s.id,
        source_in_sec: s.start_sec,
        source_out_sec: s.end_sec,
        target_duration_sec: Math.round((s.end_sec - s.start_sec) * 10) / 10,
        story_role: '',
        transition_hint: '',
        music_cue: '', audio_hint: '', color_hint: '', review_status: 'pending',
        evidence: 'static',
        confidence: 0.8,
      },
    ])
  }

  function move(from: number, to: number) {
    setDraft((d) => {
      const next = [...d]
      const [item] = next.splice(from, 1)
      next.splice(to, 0, item)
      return next
    })
  }

  const total = draft.reduce((acc, s) => acc + s.target_duration_sec, 0)
  const rcPath = roughcutJob.data?.resume_token?.path as string | undefined
  const rcIncomplete = Boolean(roughcutJob.data?.resume_token?.incomplete)

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-ink-800 px-6 py-4">
        <div className="flex items-center gap-3">
          <Link to={`/project/${id}`} className="text-sm text-slate-400 hover:text-slate-200">← 返回项目</Link>
          <h1 className="text-lg font-semibold text-white">故事板 / 导出</h1>
          <span className="text-sm text-slate-500">计划 {draft.length} 个镜头 · 约 {total.toFixed(1)}s</span>
        </div>
        <div className="flex items-center gap-2">
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportCsv(id))}>CSV</a>
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportJson(id))}>JSON</a>
          <a className="rounded-md border border-ink-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-ink-800" href={downloadUrl(endpoints.exportOtio(id))}>OTIO</a>
          <Button variant="ghost" disabled={!plan.data || save.isPending} onClick={() => {
            if (plan.data) setEditor({ draft: loadServer(plan.data), dirty: false, server: plan.data })
          }}>重新加载服务端版本</Button>
          <Button disabled={plan.isLoading || save.isPending} onClick={() => save.mutate(draft)}>
            保存剪辑计划
          </Button>
          {/* TSB-261: a `cancelling` job still occupies the project, so the
              button stays disabled until the worker has really stopped. */}
          <Button variant="ghost" disabled={roughcut.isPending || ['running', 'pending', 'cancelling'].includes(roughcutJob.data?.status ?? '')} onClick={() => roughcut.mutate()}>
            生成粗剪
          </Button>
        </div>
      </div>

      {roughcutJob.data && (
        <div className="border-b border-ink-800 px-6 py-2 text-sm">
          <Badge
            tone={
              roughcutJob.data.status === 'succeeded'
                ? 'green'
                : roughcutJob.data.status === 'partial_failed'
                  ? 'yellow'
                  : roughcutJob.data.status === 'failed'
                    ? 'red'
                    : 'blue'
            }
          >
            {roughcutJob.data.status}
          </Badge>
          {' '}粗剪 {roughcutJob.data.stats.done ?? 0}/{roughcutJob.data.stats.total ?? 0}
          {rcIncomplete && <span className="ml-2 text-amber-400">⚠ 缺镜头，仅为不完整预览</span>}
          {roughcutJob.data.error && <span className="ml-2 text-red-400">{roughcutJob.data.error}</span>}
          {rcPath && (
            <a
              className="ml-2 text-accent hover:underline"
              href={roughcutUrl(id)}
              target="_blank"
              rel="noreferrer"
            >
              预览粗剪
            </a>
          )}
        </div>
      )}

      {save.error && <div className="px-6 py-2 text-sm text-red-400">保存失败，草稿已保留：{save.error.message}</div>}
      {dirty && <div className="px-6 py-2 text-xs text-amber-300">草稿有未保存修改</div>}
      {result && (
        <div className="border-b border-ink-800 px-6 py-2 text-sm">
          {result.errors.length === 0 ? (
            <span className="text-emerald-300">已保存 {result.created} 个镜头，总时长 {result.total_duration_sec}s</span>
          ) : (
            <span className="text-red-400">校验失败（未保存）：{result.errors.map((e) => e.message).join('；')}</span>
          )}
        </div>
      )}

      <div className="flex flex-1 overflow-hidden">
        {/* Segment picker */}
        <div className="w-1/2 overflow-auto border-r border-ink-800 px-4 py-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">候选镜头</h2>
          {segments.isLoading ? (
            <Spinner />
          ) : !segments.data || segments.data.length === 0 ? (
            <EmptyState title="暂无片段" hint="先运行「检测片段」" />
          ) : (
            <ul className="space-y-2">
              {segments.data.map((s) => (
                <li key={s.id} className="flex items-center gap-3 rounded-md border border-ink-700 bg-ink-900 p-2">
                  <div className="h-10 w-16 shrink-0 overflow-hidden rounded bg-ink-800">
                    {s.thumbnail ? <img src={mediaUrl(id, s.thumbnail)} alt="" className="h-full w-full object-cover" /> : null}
                  </div>
                  <div className="min-w-0 flex-1 text-xs text-slate-400">
                    <div className="truncate text-slate-300">{s.asset_display_name}</div>
                    <div>{formatTimeCode(s.start_sec)} – {formatTimeCode(s.end_sec)}</div>
                  </div>
                  <Button variant="ghost" disabled={s.status === 'needs_review'} onClick={() => addToPlan(s)}>{s.status === 'needs_review' ? '需要复核' : '+'}</Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Draft plan */}
        <div className="w-1/2 overflow-auto px-4 py-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">剪辑计划（按顺序）</h2>
          {draft.length === 0 ? (
            <EmptyState title="还没有镜头" hint="从左侧点击 + 加入计划" />
          ) : (
            <ul className="space-y-2">
              {draft.map((s, i) => (
                <li key={i} className="rounded-md border border-ink-700 bg-ink-900 p-2">
                  <div className="mb-1 flex items-center justify-between text-xs text-slate-400">
                    <span className="font-medium text-slate-300">#{i + 1}</span>
                    <span>{s.source_in_sec.toFixed(1)}s – {s.source_out_sec.toFixed(1)}s</span>
                    <div className="flex gap-1">
                      <button className="px-1 text-slate-400 hover:text-white" onClick={() => move(i, i - 1)} disabled={i === 0}>↑</button>
                      <button className="px-1 text-slate-400 hover:text-white" onClick={() => move(i, i + 1)} disabled={i === draft.length - 1}>↓</button>
                      <button className="px-1 text-red-400 hover:text-red-300" onClick={() => setDraft((d) => d.filter((_, x) => x !== i))}>✕</button>
                    </div>
                  </div>
                  <div className="mb-2 flex gap-2 text-xs text-slate-400">
                    {(['source_in_sec', 'source_out_sec', 'target_duration_sec'] as const).map((field) => (
                      <label key={field}>{field === 'source_in_sec' ? '入点秒' : field === 'source_out_sec' ? '出点秒' : '目标时长秒'}
                        <input className={inputClass} type="number" min="0" step="0.01" value={s[field]}
                          onChange={(e) => setDraft((d) => d.map((x, xi) => xi === i ? { ...x, [field]: Number(e.target.value) } : x))} />
                      </label>
                    ))}
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      className={inputClass + ' max-w-[120px]'}
                      value={s.story_role}
                      onChange={(e) => setDraft((d) => d.map((x, xi) => (xi === i ? { ...x, story_role: e.target.value } : x)))}
                    >
                      {ROLES.map((r) => <option key={r} value={r}>{r || '用途…'}</option>)}
                    </select>
                    <input
                      className={inputClass}
                      placeholder="转场提示（可选）"
                      value={s.transition_hint}
                      onChange={(e) => setDraft((d) => d.map((x, xi) => (xi === i ? { ...x, transition_hint: e.target.value } : x)))}
                    />
                  </div>
                  <div className="mt-2 flex gap-2">
                    {([['music_cue', '配乐提示'], ['audio_hint', '音频提示'], ['color_hint', '调色提示']] as const).map(([field, label]) => (
                      <input key={field} className={inputClass} placeholder={label} value={s[field]}
                        onChange={(e) => setDraft((d) => d.map((x, xi) => xi === i ? { ...x, [field]: e.target.value } : x))} />
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {plan.data && plan.data.shots.length > 0 && (
            <div className="mt-4">
              <h3 className="mb-2 text-xs font-medium uppercase text-slate-500">已保存的计划（{plan.data.shots.length} 镜头）</h3>
              <ol className="space-y-1 text-xs text-slate-400">
                {plan.data.shots.map((s) => (
                  <li key={s.order}>
                    <Badge tone="blue">#{s.order}</Badge> {s.source_name} {s.source_in_sec}s–{s.source_out_sec}s · {s.story_role || '未设用途'}
                  </li>
                ))}
              </ol>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
