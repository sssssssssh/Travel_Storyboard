import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type {
  CoverageOut,
  JobOut,
  MotionCandidateOut,
  MotionReviewOut,
  VisionPreviewOut,
  VisionResultOut,
  VisionResultsPage,
  VisionStatusOut,
} from '../lib/types'
import { useJob } from '../lib/useJob'
import { Badge, Button, Card, Spinner } from './ui'
import CleanupNotice from './CleanupNotice'

/**
 * M5 image-semantics panel (TSB-305).
 *
 * Deliberately additive: the legacy direct-model entry, the offline package and
 * `ai_review.py` all keep working when no model is configured. Nothing here
 * writes an EditPlanShot or a human rating — results are shown as
 * "模型生成，待人工确认" and the human fields stay the human's.
 */
export default function VisionPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const [limit, setLimit] = useState(6)
  const [acknowledged, setAcknowledged] = useState(false)

  const job = useJob(jobId)

  const status = useQuery({
    queryKey: ['vision-status'],
    queryFn: () => api<VisionStatusOut>(endpoints.visionStatus),
  })

  const results = useQuery({
    queryKey: ['vision-results', projectId],
    queryFn: () => api<VisionResultsPage>(`${endpoints.visionResults(projectId)}?limit=50`),
    refetchInterval: ['running', 'pending', 'cancelling'].includes(job.data?.status ?? '') ? 2000 : false,
  })

  const candidates = useQuery({
    queryKey: ['vision-candidates', projectId],
    queryFn: () => api<MotionCandidateOut[]>(endpoints.visionCandidates(projectId)),
  })

  const preview = useQuery({
    queryKey: ['vision-preview', projectId, limit],
    queryFn: () =>
      api<VisionPreviewOut>(endpoints.visionPreview(projectId), {
        method: 'POST',
        body: JSON.stringify({ limit }),
      }),
    enabled: !!status.data?.configured,
  })

  const analyze = useMutation({
    mutationFn: () =>
      api<JobOut>(endpoints.visionAnalyze(projectId), {
        method: 'POST',
        body: JSON.stringify({ limit, upload_acknowledged: acknowledged }),
      }),
    onSuccess: (j) => {
      setJobId(j.id)
      qc.invalidateQueries({ queryKey: ['vision-results', projectId] })
    },
  })

  const configured = status.data?.configured ?? false
  const modeOn = status.data?.mode === 'on'
  const canRun = configured && modeOn && acknowledged && !analyze.isPending

  return (
    <Card className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-200">视觉理解（M5）</h3>
        <div className="flex items-center gap-2">
          {status.isLoading ? (
            <Spinner />
          ) : configured ? (
            <Badge tone="green">模型已配置</Badge>
          ) : (
            <Badge tone="yellow">模型未配置</Badge>
          )}
          {status.data && <Badge tone={modeOn ? 'green' : 'slate'}>mode={status.data.mode}</Badge>}
          {status.data?.model_name && <Badge tone="blue">{status.data.model_name}</Badge>}
        </div>
      </div>

      {status.data?.allow_insecure_http && (
        <p className="text-sm text-amber-300">
          开发测试例外：已允许非本地明文 HTTP。生产验收须使用 HTTPS 或加密隧道，并关闭明文例外。
        </p>
      )}

      {!configured && (
        <p className="text-sm text-slate-400">
          未配置视觉模型。离线分析包与 <code className="text-slate-300">ai_review.py</code> 规则路线仍然可用，
          它们不需要模型。
        </p>
      )}

      {configured && !modeOn && (
        <p className="text-sm text-amber-300">
          模型已配置，但 <code>TRAVELSB_MODEL_MODE=off</code>。出于隐私默认不上传任何画面，
          需显式设为 <code>on</code> 才会发送脱敏后的代表帧。
        </p>
      )}

      {configured && (
        <>
          {preview.data?.coverage && (
            <div className="rounded-md border border-ink-700 bg-ink-800/60 p-3 text-xs text-slate-300">
              <div className="mb-1 font-medium text-slate-200">本次上传预估（未发生任何调用）</div>
              <div>
                将分析 <strong>{preview.data.segments_to_analyze}</strong> 个镜头，发送{' '}
                <strong>{preview.data.estimated_images}</strong> 张脱敏 JPEG，
                预计 <strong>{preview.data.estimated_requests}</strong> 次请求 →{' '}
                {preview.data.model_name || '（未指定模型）'}
              </div>
              <div className="mt-1 text-slate-400">
                库内共 {preview.data.coverage.library_segments} 个镜头，可分析{' '}
                {preview.data.coverage.eligible} 个，本次未覆盖{' '}
                {preview.data.coverage.not_covered} 个。
              </div>
              <div className="mt-1 text-slate-400">
                不上传原片视频；代表帧经重编码去除 EXIF/GPS。
                注意：EXIF 清除<strong>不</strong>等于人脸/车牌已脱敏。
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              镜头数
              <input
                type="number"
                min={1}
                max={200}
                value={limit}
                onChange={(e) => setLimit(Math.max(1, Number(e.target.value) || 1))}
                className="w-20 rounded-md border border-ink-700 bg-ink-800 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              我确认将上述数量的脱敏代表帧发送至该模型服务
            </label>
            <Button disabled={!canRun} onClick={() => analyze.mutate()}>
              {analyze.isPending ? '提交中…' : '开始分析'}
            </Button>
          </div>
        </>
      )}

      {analyze.error && (
        <div className="rounded-md border border-red-800 bg-red-900/30 p-2 text-xs text-red-300">
          无法提交任务：{String((analyze.error as Error).message)}
        </div>
      )}

      {job.data && <JobProgress job={job.data} />}

      <ResultsTable
        items={results.data?.items ?? []}
        loading={results.isLoading}
        onReload={() => qc.invalidateQueries({ queryKey: ['vision-results', projectId] })}
      />

      <MotionCandidateList
        items={candidates.data ?? []}
        projectId={projectId}
        acknowledged={acknowledged}
        onStarted={(jobId) => setJobId(jobId)}
      />

      <MotionReviewList projectId={projectId} />
    </Card>
  )
}

function JobProgress({ job }: { job: JobOut }) {
  const stats = job.stats ?? {}
  const coverage = stats.coverage as CoverageOut | undefined
  const tone =
    job.status === 'succeeded' ? 'green' : job.status === 'partial_failed' ? 'yellow' : job.status === 'failed' ? 'red' : 'blue'

  return (
    <div className="space-y-1 rounded-md border border-ink-700 bg-ink-800/60 p-2 text-xs">
      <div className="flex items-center gap-2">
        <Badge tone={tone}>{job.status}</Badge>
        <span className="text-slate-300">
          分析 {stats.analyzed ?? 0} / 跳过 {stats.skipped ?? 0} / 失败 {stats.failed ?? 0}
          {typeof stats.images_sent === 'number' && <> · 发送图片 {stats.images_sent}</>}
          {typeof stats.calls === 'number' && <> · 调用 {stats.calls}</>}
        </span>
      </div>
      {coverage && (
        <div className="text-slate-400">
          覆盖：可分析 {coverage.eligible} 个，计划分析 {coverage.planned ?? coverage.considered} 个，
          实际成功理解 {coverage.analyzed ?? stats.analyzed ?? 0} 个，
          有效缓存 {coverage.cached_valid ?? stats.skipped ?? 0} 个，
          未覆盖 {coverage.not_covered} 个（库内共 {coverage.library_segments} 个）。
        </div>
      )}
      {stats.usage && Object.keys(stats.usage).length > 0 && (
        <div className="text-slate-500">
          用量：{Object.entries(stats.usage).map(([k, v]) => `${k}=${v}`).join(' · ')}
        </div>
      )}
      {(stats.cleanup || job.status === 'cancelled') && <CleanupNotice cleanup={stats.cleanup} />}
      {job.error && <div className="text-amber-300">{job.error}</div>}
      {!!job.failures?.items?.length && (
        <ul className="list-inside list-disc text-amber-300">
          {job.failures.items.slice(0, 5).map((f, i) => (
            <li key={i}>
              {f.ref ? `${f.ref.slice(0, 8)}: ` : ''}
              {f.message}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ResultsTable({
  items,
  loading,
  onReload,
}: {
  items: VisionResultOut[]
  loading: boolean
  onReload: () => void
}) {
  if (loading) return <Spinner label="读取语义结果…" />
  if (!items.length) {
    return (
      <p className="text-sm text-slate-500">
        还没有语义结果。分析完成后，每条结果都会带上模型/提示词版本与证据。
      </p>
    )
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-400">共 {items.length} 条（最新在前）</span>
        <Button variant="ghost" onClick={onReload}>
          刷新
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead className="text-slate-400">
            <tr>
              <th className="p-1">镜头</th>
              <th className="p-1">场景</th>
              <th className="p-1">光照 / 构图</th>
              <th className="p-1">证据</th>
              <th className="p-1">来源</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className="border-t border-ink-800 align-top">
                <td className="p-1 font-mono text-slate-400">{r.segment_id.slice(0, 8)}</td>
                <td className="p-1 text-slate-200">
                  {(r.visual?.scene_tags ?? []).join(', ') || '—'}
                  {!!r.visual?.subjects?.length && (
                    <div className="text-slate-500">{r.visual.subjects.slice(0, 3).join('；')}</div>
                  )}
                </td>
                <td className="p-1 text-slate-300">
                  {r.visual?.lighting ?? '—'} / {r.visual?.composition ?? '—'}
                </td>
                <td className="p-1">
                  <Badge tone={r.evidence?.state === 'observed' ? 'green' : 'yellow'}>
                    {r.evidence?.state ?? 'unknown'}
                  </Badge>
                  {typeof r.evidence?.observed_confidence === 'number' && (
                    <span className="ml-1 text-slate-500">
                      {r.evidence.observed_confidence.toFixed(2)}
                    </span>
                  )}
                  {r.evidence?.review?.needs_motion_review && (
                    <Badge tone="yellow">需动态复核</Badge>
                  )}
                  {!!r.evidence?.limitations?.length && (
                    <div className="text-slate-500">{r.evidence.limitations[0]}</div>
                  )}
                </td>
                <td className="p-1 text-slate-400">
                  <div>{r.model_id}</div>
                  <div className="text-slate-600">prompt {r.prompt_version}</div>
                  <div className="text-slate-600">schema {r.schema_version}</div>
                  <Badge tone="slate">模型生成，待人工确认</Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function MotionCandidateList({
  items,
  projectId,
  acknowledged,
  onStarted,
}: {
  items: MotionCandidateOut[]
  projectId: string
  acknowledged: boolean
  onStarted: (jobId: string) => void
}) {
  const qc = useQueryClient()

  const start = useMutation({
    mutationFn: (segmentIds: string[]) =>
      api<JobOut>(endpoints.motionReview(projectId), {
        method: 'POST',
        body: JSON.stringify({
          segment_ids: segmentIds,
          frame_count: 4,
          upload_acknowledged: acknowledged,
        }),
      }),
    onSuccess: (j) => {
      onStarted(j.id)
      qc.invalidateQueries({ queryKey: ['motion-reviews', projectId] })
    },
  })

  if (!items.length) return null
  const toneFor = (s: string) => (s === 'high' ? 'red' : s === 'medium' ? 'yellow' : 'slate')

  return (
    <div className="space-y-2 border-t border-ink-800 pt-3">
      <div className="flex items-center gap-2">
        <h4 className="text-sm font-semibold text-slate-200">动态复核候选（M5.2）</h4>
        <Badge tone="slate">{items.length}</Badge>
      </div>
      <p className="text-xs text-slate-500">
        规则来源只是<strong>可测量的帧间差异</strong>，表示「疑似画面变化，需核验」；
        帧差本身不能证明抖动或甩镜。单帧镜头标记为无法判断运动，而不是被排除。
      </p>
      <ul className="space-y-1 text-xs">
        {items.slice(0, 10).map((c) => (
          <li key={c.segment_id} className="rounded border border-ink-800 bg-ink-800/40 p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-slate-400">{c.segment_id.slice(0, 8)}</span>
              <Badge tone={toneFor(c.suggested_priority)}>{c.suggested_priority}</Badge>
              {c.sources.map((s) => (
                <Badge key={s} tone="blue">
                  {s}
                </Badge>
              ))}
              <span className="text-slate-500">
                {c.start_sec.toFixed(1)}–{c.end_sec.toFixed(1)}s · {c.frame_count} 帧
              </span>
              {c.temporal_coverage_insufficient && <Badge tone="yellow">运动不可判定</Badge>}
              <Button
                variant="ghost"
                disabled={!acknowledged || start.isPending}
                onClick={() => start.mutate([c.segment_id])}
              >
                多帧复核
              </Button>
            </div>
            <div className="mt-1 text-slate-400">{c.reasons.join('；')}</div>
          </li>
        ))}
      </ul>
      {!acknowledged && (
        <p className="text-xs text-amber-300">需先勾选上方同意项才能发起多帧复核。</p>
      )}
      {start.error && (
        <p className="text-xs text-red-300">提交失败：{String((start.error as Error).message)}</p>
      )}
    </div>
  )
}

/**
 * Timestamped multi-frame results (TSB-307).
 *
 * The label "多帧动态推断" is deliberate and unavoidable: these readings come
 * from stills at known timestamps, not from continuous video, and the interface
 * must never imply the latter.
 */
function MotionReviewList({ projectId }: { projectId: string }) {
  const qc = useQueryClient()
  const [notes, setNotes] = useState<Record<string, string>>({})
  const reviews = useQuery({
    queryKey: ['motion-reviews', projectId],
    queryFn: () => api<MotionReviewOut[]>(endpoints.motionReviews(projectId)),
  })

  const verdict = useMutation({
    mutationFn: ({ review, value }: { review: MotionReviewOut; value: string }) =>
      api<MotionReviewOut>(endpoints.motionVerdict(review.segment_id), {
        method: 'POST',
        body: JSON.stringify({ verdict: value, note: notes[review.id] ?? '',
          motion_review_id: review.id, input_signature: review.input_signature }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['motion-reviews', projectId] }),
  })

  const items = reviews.data ?? []
  if (!items.length) return null

  return (
    <div className="space-y-2 border-t border-ink-800 pt-3">
      <div className="flex items-center gap-2">
        <h4 className="text-sm font-semibold text-slate-200">多帧动态推断结果</h4>
        <Badge tone="yellow">多帧动态推断，非连续视频</Badge>
      </div>
      <ul className="space-y-1 text-xs">
        {items.slice(0, 10).map((m) => (
          <li key={m.id} className="rounded border border-ink-800 bg-ink-800/40 p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-slate-400">{m.segment_id.slice(0, 8)}</span>
              <Badge tone={m.motion_type === 'unknown' ? 'yellow' : 'green'}>{m.motion_type}</Badge>
              <Badge tone={m.stability_risk === 'high' ? 'red' : 'slate'}>
                稳定风险 {m.stability_risk}
              </Badge>
              <span className="text-slate-500">
                {m.start_sec.toFixed(1)}–{m.end_sec.toFixed(1)}s · 方法 {m.method}
              </span>
              {m.evidence?.needs_human_review && <Badge tone="yellow">需人工确认</Badge>}
              {m.human_verdict && <Badge tone="green">人工已确认：{m.human_verdict}</Badge>}
              <Badge tone={m.human_state === 'confirmed_current' ? 'green' : 'yellow'}>
                {m.human_state === 'confirmed_current' ? '当前有效'
                  : m.human_state === 'needs_reconfirmation' ? '证据改变，需重新确认' : '尚未人工判定'}
              </Badge>
            </div>
            <div className="mt-1 text-slate-300">{m.action_summary}</div>
            {m.human_note && <div className="text-slate-300">人工备注：{m.human_note}</div>}
            {m.human_invalid_reason && <div className="text-amber-300">失效原因：{m.human_invalid_reason}</div>}
            {!!m.human_history?.length && (
              <details className="mt-1 text-slate-400">
                <summary>历史判断（最近 {m.human_history.length} 条）</summary>
                <ul>{m.human_history.map((h) => (
                  <li key={h.id}>{h.created_at} · {h.verdict} · {h.is_effective ? '当前有效' : '历史证据'}
                    {' · '}{h.note} · 证据 {h.motion_review_id.slice(0, 8)}</li>
                ))}</ul>
              </details>
            )}
            <div className="mt-1 text-slate-500">
              证据时间戳：{(m.evidence?.evidence_timestamps_sec ?? []).map((t) => `${t.toFixed(1)}s`).join(', ') || '—'}
              {' · '}音频未分析
            </div>
            {!!m.evidence?.limitations?.length && (
              <div className="text-slate-500">局限：{m.evidence.limitations.join('；')}</div>
            )}
            {(m.human_state === 'needs_reconfirmation' || !m.human_verdict) && (
              <div className="mt-1 flex flex-wrap gap-2">
                <input aria-label="人工判定备注" placeholder="人工判定备注" maxLength={2000}
                  className="rounded bg-ink-900 px-2" value={notes[m.id] ?? ''}
                  onChange={(e) => setNotes((old) => ({ ...old, [m.id]: e.target.value }))} />
                <Button disabled={verdict.isPending} variant="ghost" onClick={() => verdict.mutate({ review: m, value: 'confirm' })}>
                  确认成立
                </Button>
                <Button disabled={verdict.isPending} variant="ghost" onClick={() => verdict.mutate({ review: m, value: 'reject' })}>
                  判定不成立
                </Button>
                <Button disabled={verdict.isPending} variant="ghost" onClick={() => verdict.mutate({ review: m, value: 'unclear' })}>
                  仍不确定
                </Button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {verdict.error && <div role="alert" className="text-xs text-red-300">{String(verdict.error)}</div>}
    </div>
  )
}
