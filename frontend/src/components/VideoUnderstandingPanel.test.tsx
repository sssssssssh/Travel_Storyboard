// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../lib/api'
import type {
  MotionReviewOut,
  VideoCoverageOut,
  VideoObservationOut,
  VisionResultOut,
} from '../lib/types'
import VideoUnderstandingPanel from './VideoUnderstandingPanel'

vi.mock('../lib/api', async (original) => ({
  ...(await original<typeof import('../lib/api')>()),
  api: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const segmentId = 'abcdef12-3456-7890-abcd-ef1234567890'

const observation: VideoObservationOut = {
  id: 'v1',
  segment_id: segmentId,
  asset_id: 'a1',
  status: 'succeeded',
  method: 'video_native',
  is_current: true,
  model_id: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  schema_version: '1.0',
  protocol: 'video_url_data_uri',
  summary: '湖面有小船移动，随后静止',
  events: [
    {
      event_type: 'subject_action',
      start_rel_sec: 1.5,
      end_rel_sec: 3.25,
      start_abs_sec: 11.5,
      end_abs_sec: 13.25,
      description: '一艘小船快速移动',
      confidence: 0.62,
      confidence_is_calibrated: false,
      evidence: ['video_window'],
      limitations: [],
    },
  ],
  limitations: ['clip is silent'],
  dropped: [],
  clip: { start_sec: 10, end_sec: 15, duration_sec: 5, size_bytes: 4096, sha256: 'abc' },
  segment_window: { start_sec: 10, end_sec: 15 },
  audio_analyzed: false,
  color_unverified: false,
  observed_temporal_quality: 'limited',
  request_usage: { total_tokens: 120 },
  error_code: '',
  error_message: '',
  created_at: '2026-10-16T00:00:00Z',
  updated_at: '2026-10-16T00:00:00Z',
}

const coverage: VideoCoverageOut = {
  library_segments: 99,
  segments_with_video_reading: 8,
  segments_not_covered: 91,
  failed_attempts: 0,
  event_candidates: 12,
  complete: false,
  method: 'video_native',
  audio_analyzed: false,
}

const still: VisionResultOut = {
  id: 'r1',
  segment_id: segmentId,
  asset_id: 'a1',
  status: 'succeeded',
  model_provider: 'openai_compatible',
  model_id: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  schema_version: '1.0',
  source_signature: 'sig',
  visual: { scene_tags: ['water'], subjects: ['boats'] },
  evidence: { state: 'observed' },
  request_usage: { total_tokens: 30 },
  is_current: true,
  error_code: '',
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
}

const motion: MotionReviewOut = {
  id: 'm1',
  segment_id: segmentId,
  asset_id: 'a1',
  method: 'multiframe',
  status: 'succeeded',
  model_id: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  start_sec: 10,
  end_sec: 15,
  motion_type: 'pan',
  stability_risk: 'low',
  action_summary: '镜头向左平移',
  analysis: {},
  evidence: { method: 'multiframe' },
  request_usage: {},
  is_current: true,
  human_verdict: '',
  human_note: '',
  human_at: null,
  input_signature: 'sig',
  human_state: 'not_reviewed',
  human_verdict_id: null,
  human_invalid_reason: '',
  human_history: [],
  error_code: '',
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
}

function mockApi(routes: Record<string, unknown>) {
  vi.mocked(api).mockImplementation((url: string) => {
    const path = String(url).split('?')[0]
    if (path in routes) return Promise.resolve(routes[path])
    const hit = Object.keys(routes).find((k) => path.endsWith(k) || k.endsWith(path))
    if (hit) return Promise.resolve(routes[hit])
    return Promise.resolve({})
  })
}

function setup() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <VideoUnderstandingPanel projectId="p" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const baseRoutes = {
  '/api/projects/p/video-observations': [] as VideoObservationOut[],
  '/api/projects/p/video-coverage': coverage,
  '/api/projects/p/vision-results': { items: [still], total: 1, limit: 50, offset: 0 },
  '/api/projects/p/motion-reviews': [motion],
}

// --- honesty of the default state ------------------------------------------

test('an empty state says no reading exists rather than showing success', async () => {
  mockApi(baseRoutes)
  setup()
  expect(await screen.findByText(/尚无视频读数/)).toBeTruthy()
  expect(screen.queryByTestId('video-observation')).toBeNull()
})

test('the experiment framing is visible up front', () => {
  mockApi(baseRoutes)
  setup()
  expect(screen.getByText(/实验功能/)).toBeTruthy()
  expect(screen.getByText(/短时、无声、去元数据/)).toBeTruthy()
})

test('coverage is reported as a subset, not a total', async () => {
  mockApi(baseRoutes)
  setup()
  expect(await screen.findByText(/未覆盖/)).toBeTruthy()
  expect(screen.getByText(/这不是"已分析全库"，只是子集。/)).toBeTruthy()
})

// --- consent gating ---------------------------------------------------------

test('the run button is disabled until per-call consent is given', async () => {
  mockApi(baseRoutes)
  setup()
  const button = await screen.findByRole('button', { name: '开始视频分析' })
  expect((button as HTMLButtonElement).disabled).toBe(true)

  fireEvent.click(screen.getByRole('checkbox'))
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
})

test('consent must be given explicitly; it is not inherited', async () => {
  mockApi(baseRoutes)
  setup()
  expect(await screen.findByText(/逐次/)).toBeTruthy()
})

// --- mode provenance --------------------------------------------------------

test('a fallback is shown with both requested and effective modes', async () => {
  // `useJob` re-reads the job from /api/jobs/{id} after creation, so the stats
  // must be served there too -- the create response alone is not what renders.
  const job = {
    id: 'job1',
    project_id: 'p',
    job_type: 'video_analyze',
    status: 'succeeded',
    stats: {
      mode_requested: 'video_native',
      mode_effective: 'multiframe',
      fallback_reason: 'VIDEO_CAPABILITY_UNSUPPORTED',
    },
    resume_token: {},
    error: '',
    created_at: '2026-10-16T00:00:00Z',
    started_at: null,
    finished_at: null,
  }
  mockApi({
    ...baseRoutes,
    '/api/projects/p/jobs/video_analyze': job,
    '/api/jobs/job1': job,
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  const notice = await screen.findByTestId('fallback-notice')
  expect(notice.textContent).toContain('VIDEO_CAPABILITY_UNSUPPORTED')
  // The critical wording: a fallback must not read as a video result.
  expect(notice.textContent).toContain('不是视频分析')
})

// --- TSB-354: fallback parent/child tracking --------------------------------

/** A video job whose fallback was handed to `sid`, in the given live status. */
function fallbackJob(status: string, analysed: boolean) {
  return {
    id: 'job1',
    project_id: 'p',
    job_type: 'video_analyze',
    status: 'succeeded',
    stats: {
      mode_requested: 'video_native',
      mode_effective: 'multiframe',
      fallback_reason: 'VIDEO_CAPABILITY_UNSUPPORTED',
      fallback_scheduled: true,
      fallback_job_id: 'sid-1',
      fallback_job_type: 'motion_review',
      fallback_state: 'scheduled',
    },
    fallback: {
      state: 'resolved',
      job_id: 'sid-1',
      job_type: 'motion_review',
      status,
      analysed,
    },
    resume_token: {},
    error: '',
    created_at: '2026-10-16T00:00:00Z',
    started_at: null,
    finished_at: null,
  }
}

async function renderFallback(job: ReturnType<typeof fallbackJob>) {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/jobs/video_analyze': job,
    '/api/jobs/job1': job,
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))
  return screen.findByTestId('fallback-tracking')
}

test('a queued fallback is not reported as analysed', async () => {
  // The defect this guards: the panel used to say "已回退" the instant the
  // successor was queued, letting a user read "handed over" as "analysed".
  const box = await renderFallback(fallbackJob('pending', false))

  expect(box.textContent).toContain('sid-1')
  expect(box.textContent).toContain('已排队，尚未开始')
  const verdict = screen.getByTestId('fallback-analysed')
  expect(verdict.textContent).toContain('尚未完成')
  expect(verdict.textContent).not.toContain('已入库')
})

test('a running fallback is still not reported as analysed', async () => {
  await renderFallback(fallbackJob('running', false))
  expect(screen.getByTestId('fallback-status').textContent).toBe('正在分析')
  expect(screen.getByTestId('fallback-analysed').textContent).toContain('尚未完成')
})

test('only a succeeded fallback reads as analysed', async () => {
  await renderFallback(fallbackJob('succeeded', true))
  expect(screen.getByTestId('fallback-status').textContent).toBe('已完成')
  expect(screen.getByTestId('fallback-analysed').textContent).toContain('已入库')
})

test('a failed fallback is shown as failed, never as analysed', async () => {
  await renderFallback(fallbackJob('failed', false))
  expect(screen.getByTestId('fallback-status').textContent).toBe('已失败')
  const verdict = screen.getByTestId('fallback-analysed')
  expect(verdict.textContent).toContain('尚未完成')
  expect(verdict.textContent).not.toContain('已入库')
})

test('a cancelled fallback is shown as cancelled, never as analysed', async () => {
  await renderFallback(fallbackJob('cancelled', false))
  expect(screen.getByTestId('fallback-status').textContent).toBe('已取消')
  expect(screen.getByTestId('fallback-analysed').textContent).not.toContain('已入库')
})

test('a vanished fallback job is reported rather than assumed successful', async () => {
  const job = {
    ...fallbackJob('pending', false),
    fallback: {
      state: 'missing', job_id: 'sid-1', job_type: 'motion_review',
      status: '', analysed: false,
    },
  }
  mockApi({
    ...baseRoutes,
    '/api/projects/p/jobs/video_analyze': job,
    '/api/jobs/job1': job,
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  const missing = await screen.findByTestId('fallback-missing')
  expect(missing.textContent).toContain('无法确认')
})

test('a fallback that could not be queued reports the reason', async () => {
  const job = {
    ...fallbackJob('pending', false),
    stats: {
      mode_requested: 'video_native',
      mode_effective: 'multiframe',
      fallback_reason: 'VIDEO_CAPABILITY_UNSUPPORTED',
      fallback_scheduled: false,
      fallback_schedule_error: 'JobConflict: 项目已有正在运行的互斥任务',
    },
    fallback: null,
  }
  mockApi({
    ...baseRoutes,
    '/api/projects/p/jobs/video_analyze': job,
    '/api/jobs/job1': job,
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  const err = await screen.findByTestId('fallback-schedule-error')
  expect(err.textContent).toContain('未能排队')
  expect(err.textContent).toContain('JobConflict')
})

test('a job with no fallback shows no tracking block at all', async () => {
  const job = {
    id: 'job1',
    project_id: 'p',
    job_type: 'video_analyze',
    status: 'succeeded',
    stats: { mode_requested: 'video_native', mode_effective: 'video_native' },
    fallback: { state: 'none', job_id: '', job_type: '', status: '', analysed: false },
    resume_token: {},
    error: '',
    created_at: '2026-10-16T00:00:00Z',
    started_at: null,
    finished_at: null,
  }
  mockApi({
    ...baseRoutes,
    '/api/projects/p/jobs/video_analyze': job,
    '/api/jobs/job1': job,
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  await screen.findByText(/任务状态/)
  expect(screen.queryByTestId('fallback-tracking')).toBeNull()
  expect(screen.queryByTestId('fallback-analysed')).toBeNull()
})

// --- refusal explanation ----------------------------------------------------

test('a capability refusal is explained with state and remedy', async () => {
  mockApi(baseRoutes)
  vi.mocked(api).mockImplementation((url: string, options?: RequestInit) => {
    const path = String(url).split('?')[0]
    if (path.endsWith('/jobs/video_analyze') && options?.method === 'POST') {
      return Promise.reject({
        detail: {
          error_code: 'MODEL_CAPABILITY_UNSUPPORTED',
          message: 'VIDEO_CAPABILITY_UNSUPPORTED: endpoint cannot accept video',
          capability_state: 'unsupported',
          verified: false,
          requested_mode: 'video_native',
          remedy: '服务端加 --limit-mm-per-prompt video=1 并重启',
        },
      })
    }
    if (path in baseRoutes) return Promise.resolve((baseRoutes as never)[path])
    return Promise.resolve({})
  })

  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  const refusal = await screen.findByTestId('video-refusal')
  expect(refusal.textContent).toContain('unsupported')
  expect(refusal.textContent).toContain('已验证：否')
  expect(refusal.textContent).toContain('--limit-mm-per-prompt')
})

test('a generic failure is not dressed up as a capability problem', async () => {
  mockApi(baseRoutes)
  vi.mocked(api).mockImplementation((url: string, options?: RequestInit) => {
    const path = String(url).split('?')[0]
    if (path.endsWith('/jobs/video_analyze') && options?.method === 'POST') {
      return Promise.reject(new Error('network down'))
    }
    if (path in baseRoutes) return Promise.resolve((baseRoutes as never)[path])
    return Promise.resolve({})
  })

  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始视频分析' }))

  const refusal = await screen.findByTestId('video-refusal')
  expect(refusal.textContent).toContain('REQUEST_FAILED')
})

// --- the three readings side by side ---------------------------------------

test('all three input modes are labelled with what they actually are', async () => {
  mockApi({ ...baseRoutes, '/api/projects/p/video-observations': [observation] })
  setup()
  await screen.findByTestId('video-observation')

  expect(screen.getByText('抽帧语义')).toBeTruthy()
  expect(screen.getByText('1 张代表帧')).toBeTruthy()
  expect(screen.getByText('多帧动态')).toBeTruthy()
  expect(screen.getByText('5 帧带时间戳')).toBeTruthy()
  expect(screen.getByText('视频观察')).toBeTruthy()
  expect(screen.getByText('派生短片（原生视频）')).toBeTruthy()
})

test('a missing reading shows its reason instead of disappearing', async () => {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/video-observations': [observation],
    '/api/projects/p/vision-results': { items: [], total: 0, limit: 50, offset: 0 },
  })
  setup()
  await screen.findByTestId('video-observation')
  expect(screen.getByText('无静态语义结果')).toBeTruthy()
})

// --- events -----------------------------------------------------------------

test('an event shows both clip-relative and absolute times', async () => {
  mockApi({ ...baseRoutes, '/api/projects/p/video-observations': [observation] })
  setup()
  const event = await screen.findByTestId('video-event')

  // Both coordinate systems, each labelled: mixing them misplaces an event.
  expect(event.textContent).toContain('短片 1.50–3.25s')
  expect(event.textContent).toContain('源片 11.50–13.25s')
})

test('confidence is labelled as uncalibrated', async () => {
  mockApi({ ...baseRoutes, '/api/projects/p/video-observations': [observation] })
  setup()
  const event = await screen.findByTestId('video-event')
  expect(event.textContent).toContain('未校准')
  expect(event.textContent).not.toContain('准确率')
})

test('the honesty badges are rendered', async () => {
  mockApi({ ...baseRoutes, '/api/projects/p/video-observations': [observation] })
  setup()
  const card = await screen.findByTestId('video-observation')
  expect(card.textContent).toContain('音频未分析')
  expect(card.textContent).toContain('quality=limited')
})

test('a zero-event reading says so rather than looking empty', async () => {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/video-observations': [{ ...observation, events: [], summary: '无变化' }],
  })
  setup()
  expect(await screen.findByText(/未检出可报告的时间事件/)).toBeTruthy()
})

test('dropped events are surfaced', async () => {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/video-observations': [
      { ...observation, dropped: [{ index: 0, reason: 'SEGMENT_RANGE_INVALID' }] },
    ],
  })
  setup()
  const dropped = await screen.findByTestId('dropped-events')
  expect(dropped.textContent).toContain('SEGMENT_RANGE_INVALID')
})

test('an HDR source shows the colour-unverified badge', async () => {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/video-observations': [{ ...observation, color_unverified: true }],
  })
  setup()
  const card = await screen.findByTestId('video-observation')
  expect(card.textContent).toContain('色彩未验证')
})

test('events without absolute times omit the source line rather than showing NaN', async () => {
  mockApi({
    ...baseRoutes,
    '/api/projects/p/video-observations': [
      {
        ...observation,
        events: [{ ...observation.events[0], start_abs_sec: null, end_abs_sec: null }],
      },
    ],
  })
  setup()
  const event = await screen.findByTestId('video-event')
  expect(event.textContent).toContain('短片 1.50–3.25s')
  expect(event.textContent).not.toContain('源片')
  expect(event.textContent).not.toContain('NaN')
})
