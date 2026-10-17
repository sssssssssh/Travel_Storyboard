import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, endpoints } from '../lib/api'
import type {
  JobFallbackOut,
  JobOut,
  MotionReviewOut,
  VideoCoverageOut,
  VideoObservationOut,
  VideoRefusalOut,
  VisionResultOut,
} from '../lib/types'
import { useJob } from '../lib/useJob'
import { Badge, Button, Card, Spinner } from './ui'
import CleanupNotice from './CleanupNotice'

/**
 * M5.3 native-video panel (TSB-331).
 *
 * Three jobs, in order of importance:
 *
 * 1. **Never imply a mode that did not run.** Each column is labelled with its
 *    real input (still image / sampled frames / derived clip). A fallback run
 *    shows `mode_requested` AND `mode_effective`; showing only one would let a
 *    multi-frame reading read as a video result.
 * 2. **Show uncertainty as data.** `audio_analyzed=false`, `color_unverified`,
 *    `observed_temporal_quality` and the dropped-event list are all rendered, so
 *    an incomplete reading cannot look like a complete one behind a green badge.
 * 3. **Explain a refusal.** When the endpoint cannot accept video the request is
 *    rejected before enqueueing; the panel surfaces the capability state and the
 *    concrete remedy instead of a generic error.
 *
 * Nothing here writes a human rating, a trim or an EditPlanShot: event candidates
 * are shown for a person to accept or reject, and that decision stays theirs.
 */
export default function VideoUnderstandingPanel({ projectId }: { projectId: string }) {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const [acknowledged, setAcknowledged] = useState(false)
  const [limit, setLimit] = useState(6)
  const [refusal, setRefusal] = useState<VideoRefusalOut | null>(null)

  const job = useJob(jobId)

  // A fast job's first observed state may already be terminal, so refresh on any
  // final status rather than only on a transition.
  useEffect(() => {
    if (job.data && ['succeeded', 'partial_failed', 'failed', 'cancelled'].includes(job.data.status)) {
      for (const key of ['video-observations', 'video-coverage']) {
        qc.invalidateQueries({ queryKey: [key, projectId] })
      }
    }
  }, [job.data, projectId, qc])

  const observations = useQuery({
    queryKey: ['video-observations', projectId],
    queryFn: () => api<VideoObservationOut[]>(endpoints.videoObservations(projectId)),
    refetchInterval: ['running', 'pending', 'cancelling'].includes(job.data?.status ?? '')
      ? 2000
      : false,
  })

  const coverage = useQuery({
    queryKey: ['video-coverage', projectId],
    queryFn: () => api<VideoCoverageOut>(endpoints.videoCoverage(projectId)),
  })

  // The other two families, so the three readings can be compared side by side.
  const stills = useQuery({
    queryKey: ['vision-results', projectId],
    queryFn: () =>
      api<{ items: VisionResultOut[] }>(`${endpoints.visionResults(projectId)}?limit=50`),
  })

  const motion = useQuery({
    queryKey: ['motion-reviews', projectId],
    queryFn: () => api<MotionReviewOut[]>(endpoints.motionReviews(projectId)),
  })

  const start = useMutation({
    mutationFn: () =>
      api<JobOut>(endpoints.videoAnalyze(projectId), {
        method: 'POST',
        body: JSON.stringify({
          mode: 'video_native',
          limit,
          upload_acknowledged: acknowledged,
        }),
      }),
    onSuccess: (j) => {
      setRefusal(null)
      setJobId(j.id)
      qc.invalidateQueries({ queryKey: ['video-observations', projectId] })
    },
    onError: (error: unknown) => {
      // A request-time capability refusal carries structured detail; anything else
      // is shown as a plain message rather than being dressed up as a capability
      // problem.
      const detail = (error as { detail?: VideoRefusalOut })?.detail
      if (detail && typeof detail === 'object' && 'capability_state' in detail) {
        setRefusal(detail)
      } else {
        setRefusal({
          error_code: 'REQUEST_FAILED',
          message: String((error as Error)?.message ?? error),
          capability_state: 'unknown',
          verified: false,
          requested_mode: 'video_native',
        })
      }
    },
  })

  const canRun = acknowledged && !start.isPending
  const rows = observations.data ?? []

  return (
    <Card className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-slate-200">视频理解（M5.3·实验）</h3>
        <div className="flex items-center gap-2">
          {observations.isLoading ? (
            <Spinner />
          ) : (
            <Badge tone={rows.length ? 'green' : 'slate'}>
              {rows.length} 个镜头有视频读数
            </Badge>
          )}
          <Badge tone="yellow">实验功能</Badge>
        </div>
      </div>

      {/* The experiment framing is not decoration: this mode can be unavailable,
          and saying so up front prevents a failed attempt reading as a defect. */}
      <p className="text-xs text-slate-400">
        默认关闭的实验通道。只上传由原片派生的<strong>短时、无声、去元数据</strong>片段；
        端点未验证接受视频时，请求会在入队前被拒绝。
      </p>

      {coverage.data && (
        <div className="rounded-md border border-ink-700 bg-ink-800/60 p-3 text-xs text-slate-300">
          <div className="mb-1 font-medium text-slate-200">覆盖率（如实报告子集）</div>
          <div>
            库内 <strong>{coverage.data.library_segments}</strong> 个镜头，
            已有视频读数 <strong>{coverage.data.segments_with_video_reading}</strong> 个，
            未覆盖 <strong>{coverage.data.segments_not_covered}</strong> 个
            {coverage.data.failed_attempts > 0 && (
              <>，失败 <strong>{coverage.data.failed_attempts}</strong> 次</>
            )}
            ，事件候选 <strong>{coverage.data.event_candidates}</strong> 个。
          </div>
          {!coverage.data.complete && (
            <div className="mt-1 text-amber-300">
              这不是"已分析全库"，只是子集。
            </div>
          )}
        </div>
      )}

      {refusal && (
        <div
          data-testid="video-refusal"
          className="rounded-md border border-amber-700 bg-amber-950/40 p-3 text-xs text-amber-100"
        >
          <div className="font-medium">
            视频通道不可用（{refusal.error_code}，能力状态 {refusal.capability_state}）
          </div>
          <div className="mt-1">
            已验证：{refusal.verified ? '是' : '否'}。{refusal.message}
          </div>
          {refusal.remedy && <div className="mt-1">处理方式：{refusal.remedy}</div>}
        </div>
      )}

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-slate-300">
          最多镜头数
          <input
            type="number"
            min={1}
            max={50}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value) || 1)}
            className="ml-2 w-16 rounded border border-ink-600 bg-ink-900 px-2 py-1 text-slate-200"
          />
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <input
            type="checkbox"
            checked={acknowledged}
            onChange={(e) => setAcknowledged(e.target.checked)}
          />
          我确认本次上传派生短片（可能含人物/车牌；音频已移除）
        </label>
        <Button disabled={!canRun} onClick={() => start.mutate()}>
          {start.isPending ? '提交中…' : '开始视频分析'}
        </Button>
      </div>

      {!acknowledged && (
        <p className="text-xs text-slate-500">
          未勾选同意时按钮不可用。视频上传的同意是<strong>逐次</strong>的，不会沿用上一轮图片分析的勾选。
        </p>
      )}

      {job.data && (
        <div className="rounded-md border border-ink-700 bg-ink-800/60 p-3 text-xs text-slate-300">
          <div>
            任务状态：<strong>{job.data.status}</strong>
            {typeof job.data.stats?.mode_requested === 'string' && (
              <>
                ，请求模式 <code>{job.data.stats.mode_requested}</code>
                ，实际模式 <code>{job.data.stats.mode_effective}</code>
              </>
            )}
          </div>
          {typeof job.data.stats?.fallback_reason === 'string' && job.data.stats.fallback_reason && (
            <div className="mt-1 text-amber-300" data-testid="fallback-notice">
              已回退：{job.data.stats.fallback_reason}。这是回退后的多帧分析，不是视频分析。
            </div>
          )}
          <FallbackTracking fallback={job.data.fallback} stats={job.data.stats} />
          <CleanupNotice cleanup={job.data.stats?.cleanup} />
        </div>
      )}

      {rows.length === 0 && !observations.isLoading && (
        <p className="text-sm text-slate-400">
          尚无视频读数。失败或被拒绝不会在这里留下成功记录。
        </p>
      )}

      <div className="space-y-3">
        {rows.map((row) => (
          <ObservationCard
            key={row.id}
            row={row}
            still={stills.data?.items?.find((s) => s.segment_id === row.segment_id)}
            motion={motion.data?.find((m) => m.segment_id === row.segment_id)}
          />
        ))}
      </div>
    </Card>
  )
}

/**
 * One shot's three readings side by side.
 *
 * The columns are always present, including when a reading is missing: an absent
 * column with its reason is information, whereas hiding it invites the reader to
 * assume the missing mode was not needed.
 */
/** TSB-354: show where the fallback work actually got to.
 *
 *  Before this, the panel only said "已回退" the moment the successor was
 *  *queued*. A queued job is not a finished analysis, so that wording let a user
 *  read "handed over" as "the frames have been analysed". This renders the
 *  successor's live terminal status instead, and only claims the frames were
 *  read once the successor really succeeded.
 */
function FallbackTracking({
  fallback,
  stats,
}: {
  fallback?: JobFallbackOut | null
  stats?: JobOut['stats']
}) {
  // Nothing was handed over at all -- say nothing rather than imply a fallback.
  if (!fallback || fallback.state === 'none') {
    if (typeof stats?.fallback_schedule_error === 'string' && stats.fallback_schedule_error) {
      return (
        <div className="mt-1 text-rose-300" data-testid="fallback-schedule-error">
          回退未能排队：{stats.fallback_schedule_error}
        </div>
      )
    }
    return null
  }

  if (fallback.state === 'missing') {
    return (
      <div className="mt-1 text-rose-300" data-testid="fallback-missing">
        回退任务 {fallback.job_id} 已不存在，无法确认多帧分析是否完成。
      </div>
    )
  }

  const statusText: Record<string, string> = {
    pending: '已排队，尚未开始',
    running: '正在分析',
    succeeded: '已完成',
    failed: '已失败',
    cancelled: '已取消',
    cancelling: '正在取消',
  }
  const label = statusText[fallback.status] ?? fallback.status

  return (
    <div className="mt-1" data-testid="fallback-tracking">
      <div className="text-amber-300">
        已交给 <code>{fallback.job_type}</code> 任务{' '}
        <code data-testid="fallback-job-id">{fallback.job_id}</code>：
        <strong data-testid="fallback-status">{label}</strong>
      </div>
      <div className="mt-0.5 text-slate-400" data-testid="fallback-analysed">
        {fallback.analysed
          ? '多帧分析结果已入库。'
          : '多帧分析尚未完成 —— 此处还不能视为已分析。'}
      </div>
    </div>
  )
}

function ObservationCard({
  row,
  still,
  motion,
}: {
  row: VideoObservationOut
  still?: VisionResultOut
  motion?: MotionReviewOut
}) {
  const events = row.events ?? []
  return (
    <div
      data-testid="video-observation"
      className="rounded-md border border-ink-700 bg-ink-800/40 p-3 text-xs text-slate-300"
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-mono text-slate-400">{row.segment_id.slice(0, 8)}</span>
        <Badge tone="blue">{row.method}</Badge>
        <Badge tone="slate">quality={row.observed_temporal_quality}</Badge>
        {!row.audio_analyzed && <Badge tone="slate">音频未分析</Badge>}
        {row.color_unverified && <Badge tone="yellow">色彩未验证（HDR）</Badge>}
        {row.status !== 'succeeded' && <Badge tone="yellow">{row.status}</Badge>}
      </div>

      <div className="mb-2 grid gap-2 md:grid-cols-3">
        <ReadingColumn
          title="抽帧语义"
          mode="1 张代表帧"
          body={still ? JSON.stringify(still.visual).slice(0, 160) : null}
          missing="无静态语义结果"
        />
        <ReadingColumn
          title="多帧动态"
          mode="5 帧带时间戳"
          body={motion ? `${motion.motion_type} / ${motion.action_summary}`.slice(0, 160) : null}
          missing="无多帧结果"
        />
        <ReadingColumn
          title="视频观察"
          mode="派生短片（原生视频）"
          body={row.summary || null}
          missing="无视频读数"
        />
      </div>

      {row.summary && <div className="mb-2 text-slate-200">{row.summary}</div>}

      {events.length > 0 && (
        <div className="mb-2 space-y-1">
          <div className="font-medium text-slate-200">事件候选（{events.length}）</div>
          {events.map((event, index) => (
            <div
              key={`${event.event_type}-${index}`}
              data-testid="video-event"
              className="rounded border border-ink-700 bg-ink-900/60 p-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone="slate">{event.event_type}</Badge>
                {/* Both coordinate systems are shown, labelled: clip-relative is
                    what the model answered, absolute is where it sits in the
                    source. Showing only one invites the wrong mental model. */}
                <span title="短片内相对秒">短片 {event.start_rel_sec.toFixed(2)}–{event.end_rel_sec.toFixed(2)}s</span>
                {event.start_abs_sec !== null && event.end_abs_sec !== null && (
                  <span title="源视频绝对秒">
                    源片 {event.start_abs_sec.toFixed(2)}–{event.end_abs_sec.toFixed(2)}s
                  </span>
                )}
                <span title="模型自报置信度，未经校准">
                  置信度 {event.confidence.toFixed(2)}（未校准）
                </span>
              </div>
              <div className="mt-1 text-slate-200">{event.description}</div>
            </div>
          ))}
        </div>
      )}

      {events.length === 0 && row.status === 'succeeded' && (
        <div className="mb-2 text-slate-400">该短片未检出可报告的时间事件。</div>
      )}

      {row.limitations?.length > 0 && (
        <div className="mb-1 text-slate-400">
          局限：{row.limitations.join('；')}
        </div>
      )}

      {/* Dropped events are shown, not hidden: they are how a user learns the
          model produced something that could not be trusted. */}
      {row.dropped?.length > 0 && (
        <div className="text-amber-300" data-testid="dropped-events">
          已丢弃 {row.dropped.length} 条不可用事件（
          {row.dropped.map((d) => d.reason).filter(Boolean).join('、')}）
        </div>
      )}

      <div className="mt-1 text-slate-500">
        模型 {row.model_id || '—'}；协议 {row.protocol || '—'}；
        片段 {row.clip?.duration_sec?.toFixed(1) ?? '—'}s /{' '}
        {row.clip?.size_bytes ?? '—'} 字节
      </div>
    </div>
  )
}

function ReadingColumn({
  title,
  mode,
  body,
  missing,
}: {
  title: string
  mode: string
  body: string | null
  missing: string
}) {
  return (
    <div className="rounded border border-ink-700 bg-ink-900/40 p-2">
      <div className="font-medium text-slate-200">{title}</div>
      <div className="text-slate-500">{mode}</div>
      <div className="mt-1 text-slate-300">{body ?? <em className="text-slate-500">{missing}</em>}</div>
    </div>
  )
}
