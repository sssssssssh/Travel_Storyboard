/**
 * TSB-371-A — footage review: watch the shot, then check what the model claimed.
 *
 * The page exists to support a human judgement, so it must not pre-judge:
 *   - headings say "模型判断，待人工核对", never "AI 识别结果";
 *   - a shot with no reading says so instead of showing placeholder prose;
 *   - `freshness_unverified` is shown as a caveat on every item;
 *   - the clip is silent and the page says the audio was never analysed;
 *   - the source-absolute time of each claim is shown and clickable, so the
 *     reviewer can jump to the exact second the claim is about.
 */
import { useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, endpoints, withToken } from '../lib/api'
import type { EvaluationSamples, EvalSampleItem, EvalClipStatus } from '../lib/types'
import { Badge, Button, Card, Spinner } from '../components/ui'

const CHANNEL_LABEL: Record<string, string> = {
  static: '静帧',
  multiframe: '多帧',
  video_native: '原生视频',
  none: '未分析',
}

const READING_LABEL = [
  { key: 'scene_tags', label: '场景标签' },
  { key: 'subjects', label: '主体' },
  { key: 'visible_actions', label: '动作' },
  { key: 'lighting', label: '光线' },
  { key: 'composition', label: '构图' },
  { key: 'mood', label: '情绪' },
]

const STATE_LABEL: Record<string, { text: string; tone: string }> = {
  current: { text: '已验证', tone: 'green' },
  freshness_unverified: { text: '新鲜度未验证', tone: 'yellow' },
  stale: { text: '已过期', tone: 'red' },
  failed: { text: '分析失败', tone: 'red' },
  not_analyzed: { text: '未分析', tone: 'slate' },
}

function stateBadge(state: string) {
  const s = STATE_LABEL[state] ?? { text: state, tone: 'slate' }
  return <Badge tone={s.tone}>{s.text}</Badge>
}

export default function FootageReview() {
  // Remount on project change: clearing state in an effect both trips the
  // cascading-render lint rule and shows one frame of the previous project.
  const { id = '' } = useParams()
  return <FootageReviewInner key={id} />
}

function FootageReviewInner() {
  const { id = '' } = useParams()
  const [index, setIndex] = useState(0)
  const videoRef = useRef<HTMLVideoElement | null>(null)

  const samples = useQuery({
    queryKey: ['eval-samples', id],
    queryFn: () => api<EvaluationSamples>(
      `${endpoints.evaluationSamples(id)}?limit=5`),
    enabled: Boolean(id),
    staleTime: 0,
  })

  const items = samples.data?.items ?? []
  const current: EvalSampleItem | undefined = items[index]

  const clip = useQuery({
    queryKey: ['eval-clip', id, current?.segment_id],
    queryFn: () => api<EvalClipStatus>(endpoints.evaluationClips(id), {
      method: 'POST',
      body: JSON.stringify({ segment_id: current?.segment_id }),
    }),
    enabled: Boolean(id && current),
    retry: false,
  })

  const clipError = clip.error as { status?: number; detail?: { code?: string } } | null

  /** source_abs = clip_start + playback_rel — the mapping the server reports. */
  const seekToSourceSec = (absSec: number) => {
    const v = videoRef.current
    if (!v || !clip.data) return
    const rel = absSec - clip.data.clip_start_abs_sec
    if (rel < 0 || rel > (v.duration || clip.data.duration_sec) + 0.2) return
    v.currentTime = Math.max(0, rel)
    void v.play().catch(() => {})
  }

  const summary = samples.data?.summary
  const headerNote = useMemo(() => {
    if (!summary) return ''
    return `素材库 ${summary.library_segments} 个镜头 · 有读数 ${summary.analysed_segments} 个 · ` +
      `本轮抽样 ${summary.item_count} 项（${summary.distinct_segments} 个镜头）`
  }, [summary])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center gap-4 border-b border-ink-800 px-6 py-3">
        <Link to={`/project/${id}/director/plan`} className="text-sm text-slate-400 hover:text-slate-200">
          ← 返回剪辑草案
        </Link>
        <h1 className="text-lg font-semibold text-slate-200">镜头核对</h1>
        <span className="text-xs text-slate-500">
          模型判断，待人工核对 · 视频为静音派生片段
        </span>
      </div>

      <div className="space-y-3 overflow-auto p-6">
        <Card>
          <div className="text-xs text-slate-400" data-testid="eval-summary">{headerNote}</div>
          {summary && summary.freshness_unverified > 0 && (
            <div className="mt-1 text-xs text-amber-300" data-testid="freshness-warning">
              其中 {summary.freshness_unverified} 项读数的新鲜度未验证——系统无法在不重新分析的前提下
              确认它们仍对应当前素材。
            </div>
          )}
          {summary && summary.source_unavailable > 0 && (
            <div className="mt-2 rounded border border-red-900 bg-red-950/30 p-2 text-xs text-red-300"
                 data-testid="source-warning">
              <div>
                {summary.source_unavailable} 项的原片当前不可访问，无法核对画面（不计入准确率）。
              </div>
              {(summary.missing_sources?.length ?? 0) > 0 && (
                <div className="mt-1">
                  <span className="text-red-400">找不到的文件：</span>
                  {(summary.missing_sources ?? []).map((n: string) => (
                    <code key={n} className="mr-1 rounded bg-ink-900 px-1 py-0.5 text-slate-300"
                          data-testid="missing-file-name">{n}</code>
                  ))}
                </div>
              )}
              <Link className="mt-1 inline-block underline" to={`/project/${id}/evaluation/missing`}>
                查看详情（大小、记录时间、涉及的镜头）
              </Link>
            </div>
          )}
        </Card>

        {samples.isLoading && <Spinner />}
        {samples.isError && (
          <Card>
            <div className="text-sm text-red-400" data-testid="samples-error">加载样本失败。</div>
          </Card>
        )}

        {samples.data && items.length === 0 && (
          <Card>
            <div className="text-sm text-amber-300" data-testid="no-samples">
              这个项目还没有任何模型读数，因此没有可核对的画面判断。
              未分析的镜头不会生成描述。
            </div>
          </Card>
        )}

        {current && (
          <>
            <Card>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-slate-400" data-testid="shard-progress">
                  第 {index + 1} / {items.length} 个
                </span>
                <Badge tone="blue">{CHANNEL_LABEL[current.channel] ?? current.channel}</Badge>
                {stateBadge(current.evidence_state)}
                <span className="text-slate-500" data-testid="source-window">
                  源片 {current.source_window.start_abs_sec.toFixed(1)}s–
                  {current.source_window.end_abs_sec.toFixed(1)}s
                </span>
                {!current.eligible_for_planning && (
                  <Badge tone="red">不可用于策划（{current.eligibility}）</Badge>
                )}
                <span className="ml-auto flex gap-1">
                  <Button variant="ghost" data-testid="prev-sample" disabled={index === 0}
                          onClick={() => setIndex((i) => Math.max(0, i - 1))}>← 上一个</Button>
                  <Button variant="ghost" data-testid="next-sample"
                          disabled={index >= items.length - 1}
                          onClick={() => setIndex((i) => Math.min(items.length - 1, i + 1))}>
                    下一个 →
                  </Button>
                </span>
              </div>

              {!current.source_available ? (
                <div className="rounded border border-red-800 p-3 text-sm text-red-300"
                     data-testid="source-unavailable">
                  原片不可访问（{current.source_status}），无法播放核对。
                  这不是模型描述错误，不会计入准确率。
                </div>
              ) : clip.isLoading ? (
                <Spinner />
              ) : clipError ? (
                <div className="rounded border border-amber-800 p-3 text-sm text-amber-300"
                     data-testid="clip-error">
                  片段生成失败：{clipError.detail?.code ?? `HTTP_${clipError.status}`}
                </div>
              ) : clip.data ? (
                <div>
                  <video ref={videoRef} data-testid="clip-video" controls preload="metadata"
                         className="w-full max-h-80 rounded bg-black"
                         src={withToken(clip.data.media_url)} />
                  <div className="mt-1 text-xs text-slate-500" data-testid="clip-note">
                    静音片段（{clip.data.duration_sec.toFixed(1)}s）。
                    {clip.data.audio_note}
                    {clip.data.status === 'stale' && ' 源文件已变化，此片段可能不是当前素材。'}
                  </div>
                </div>
              ) : null}
            </Card>

            <Card>
              <div className="mb-2 text-xs text-slate-400">
                模型读数（原文，未经人工确认为事实）
              </div>
              <div className="text-xs text-slate-500" data-testid="result-meta">
                result_id {current.result_id?.slice(0, 8)} · 模型 {current.model.id || '未知'}
                {' · '}prompt {current.model.prompt_version || '未知'}
              </div>
              {current.readings && Object.keys(current.readings).length > 0 ? (
                <div className="mt-2 space-y-1 text-xs" data-testid="review-readings">
                  {READING_LABEL.map(({ key, label }) => {
                    const value = (current.readings as Record<string, unknown>)[key]
                    if (!value) return null
                    const values = Array.isArray(value) ? value : [value]
                    return (
                      <div key={key} className="flex gap-2">
                        <span className="w-16 shrink-0 text-slate-500">{label}</span>
                        <span className="text-slate-300">
                          {values.map((v, i) => (
                            <span key={i} className="mr-1 inline-block rounded bg-ink-800 px-1.5 py-0.5">
                              {String(v)}
                            </span>
                          ))}
                        </span>
                      </div>
                    )
                  })}
                </div>
              ) : (
                <div className="mt-2 text-xs text-slate-500" data-testid="no-readings">
                  这条读数没有可回放的文字描述。
                </div>
              )}
              <div className="mt-2">
                <Button data-testid="seek-start"
                        onClick={() => seekToSourceSec(current.source_window.start_abs_sec)}>
                  跳到镜头开头
                </Button>
              </div>
            </Card>
          </>
        )}

        {samples.data && (samples.data.controls?.length ?? 0) > 0 && (
          <Card>
            <div className="text-xs text-slate-400" data-testid="controls-title">
              对照样本（未分析，不进入准确率分母）
            </div>
            <ul className="mt-1 text-xs text-slate-500">
              {samples.data.controls.map((c) => (
                <li key={c.item_id} data-testid="control-item">
                  {c.segment_id.slice(0, 8)} · {c.source_window.start_abs_sec.toFixed(1)}s–{c.source_window.end_abs_sec.toFixed(1)}s
                  · {c.source_status}
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </div>
  )
}
