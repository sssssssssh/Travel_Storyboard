// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../lib/api'
import type { MotionCandidateOut, VisionResultOut, VisionStatusOut } from '../lib/types'
import VisionPanel from './VisionPanel'

vi.mock('../lib/api', async (original) => ({
  ...(await original<typeof import('../lib/api')>()),
  api: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const configuredStatus: VisionStatusOut = {
  configured: true,
  mode: 'on',
  model_name: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  schema_version: '1.0',
  supports_images: true,
  supports_multi_image: true,
  supports_json_schema: false,
  supports_native_video: false,
  max_images: 4,
  max_calls_per_job: 200,
  allow_insecure_http: true,
}

const preview = {
  model_name: 'GLM-5.3-Flash',
  configured: true,
  segments_to_analyze: 6,
  estimated_images: 12,
  estimated_requests: 6,
  coverage: {
    library_segments: 100,
    eligible: 96,
    excluded: 3,
    invalid: 1,
    considered: 6,
    not_covered: 90,
    images_sent: 12,
  },
  sends_source_video: false,
  strips_metadata: true,
}

const result: VisionResultOut = {
  id: 'r1',
  segment_id: 'abcdef12-3456-7890-abcd-ef1234567890',
  asset_id: 'a1',
  status: 'succeeded',
  model_provider: 'openai_compatible',
  model_id: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  schema_version: '1.0',
  source_signature: 'sig',
  visual: {
    scene_tags: ['water', 'nature'],
    subjects: ['wooden pier'],
    lighting: 'daylight',
    composition: 'wide',
    mood: ['calm'],
  },
  evidence: {
    state: 'observed',
    observed_confidence: 0.72,
    limitations: ['single_frame_cannot_prove_camera_motion'],
  },
  request_usage: { total_tokens: 30 },
  is_current: true,
  error_code: '',
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
}

const candidate: MotionCandidateOut = {
  segment_id: 'bbbbbbbb-1111-2222-3333-444444444444',
  asset_id: 'a1',
  start_sec: 0,
  end_sec: 5,
  frame_count: 1,
  sources: ['coverage'],
  reasons: ['only one frame, motion cannot be judged'],
  temporal_coverage_insufficient: true,
  suggested_priority: 'medium',
}

function mockApi(routes: Record<string, unknown>) {
  // Exact path match (query string stripped), with a prefix fallback so a route
  // key that omits a trailing segment still resolves.
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
        <VisionPanel projectId="p" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('unconfigured model shows guidance and sends no request', async () => {
  mockApi({
    '/api/vision/status': { ...configuredStatus, configured: false, mode: 'off', model_name: '' },
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
  })
  setup()
  await screen.findByText('模型未配置')
  // The offline fallback must stay reachable without a model (A01/A16).
  expect(screen.getByText(/离线分析包/)).toBeTruthy()
  const urls = vi.mocked(api).mock.calls.map((c) => String(c[0]))
  expect(urls.some((u) => u.includes('vision/preview'))).toBe(false)
  expect(screen.queryByRole('button', { name: '开始分析' })).toBeNull()
})

test('configured but mode=off refuses to upload and explains why', async () => {
  mockApi({
    '/api/vision/status': { ...configuredStatus, mode: 'off' },
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
  })
  setup()
  await screen.findByText(/TRAVELSB_MODEL_MODE=off/)
  expect(screen.getByText(/不上传任何画面/)).toBeTruthy()
})

test('consent checkbox gates the analyze button', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
  })
  setup()
  const button = await screen.findByRole('button', { name: '开始分析' })
  expect((button as HTMLButtonElement).disabled).toBe(true)

  fireEvent.click(screen.getByRole('checkbox'))
  await waitFor(() =>
    expect((screen.getByRole('button', { name: '开始分析' }) as HTMLButtonElement).disabled).toBe(false),
  )
})

test('preview states the send volume and the uncovered remainder', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
  })
  setup()
  await screen.findByText(/本次上传预估/, {}, { timeout: 4000 })
  expect(screen.getByText(/未覆盖/)).toBeTruthy()
  expect(screen.getByText(/不上传原片视频/)).toBeTruthy()
  // EXIF stripping must never be presented as face/plate anonymisation.
  expect(screen.getByText(/人脸\/车牌/)).toBeTruthy()
})

test('submitting sends the acknowledged flag and the limit', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/jobs/vision_analyze': { id: 'job-1', status: 'pending', stats: {} },
    '/api/jobs/job-1': {
      id: 'job-1',
      status: 'pending',
      stats: {},
      project_id: 'p',
      job_type: 'vision_analyze',
    },
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }))
  await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalled())
  const call = vi.mocked(api).mock.calls.find((c) => String(c[0]).includes('vision_analyze'))!
  expect(call[0]).toBe('/api/projects/p/jobs/vision_analyze')
  const body = JSON.parse(call[1]!.body as string)
  expect(body.upload_acknowledged).toBe(true)
  expect(body.limit).toBe(6)
})

test('renders semantics with provenance and an explicit unreviewed label', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 1, items: [result] },
    '/api/projects/p/vision-candidates': [],
  })
  setup()
  await screen.findByText('water, nature')
  expect(screen.getAllByText('GLM-5.3-Flash').length).toBeGreaterThan(0)
  expect(screen.getByText(/prompt 1.0/)).toBeTruthy()
  expect(screen.getByText('observed')).toBeTruthy()
  // An AI result must be visibly marked as awaiting human confirmation.
  expect(screen.getByText('模型生成，待人工确认')).toBeTruthy()
})

test('rule-based candidates are labelled as measurement, not a shake verdict', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [candidate],
  })
  setup()
  await screen.findByText(/动态复核候选/)
  expect(screen.getByText(/帧差本身不能证明抖动或甩镜/)).toBeTruthy()
  expect(screen.getByText('运动不可判定')).toBeTruthy()
  expect(screen.getAllByText(/coverage/).length).toBeGreaterThan(0)
})

test('partial failure surfaces the per-item reason', async () => {  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/jobs/vision_analyze': { id: 'job-2', status: 'pending', stats: {} },
    '/api/jobs/job-2': {
      id: 'job-2',
      project_id: 'p',
      job_type: 'vision_analyze',
      status: 'partial_failed',
      stats: {
        total: 3,
        analyzed: 2,
        skipped: 0,
        failed: 1,
        images_sent: 4,
        calls: 3,
        coverage: preview.coverage,
      },
      failures: {
        count: 1,
        items: [{ kind: 'vision', ref: 'abcdef12-3456', message: '[MODEL_TIMEOUT] timeout' }],
      },
      error: '1 ge jingtou shibai',
    },
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }))
  await screen.findByText('partial_failed')
  expect(screen.getByText(/MODEL_TIMEOUT/)).toBeTruthy()
  // The coverage line legitimately appears in both the preview and the job progress.
  expect(screen.getAllByText(/未覆盖/).length).toBeGreaterThan(1)
})

// --- TSB-307: multi-frame motion review ----------------------------------

const motionReview = {
  id: 'm1',
  segment_id: 'bbbbbbbb-1111-2222-3333-444444444444',
  asset_id: 'a1',
  method: 'multiframe',
  status: 'succeeded',
  model_id: 'GLM-5.3-Flash',
  prompt_version: '1.0',
  start_sec: 0,
  end_sec: 5,
  motion_type: 'pan',
  stability_risk: 'medium',
  action_summary: 'view rotates left across the lake',
  analysis: {},
  evidence: {
    method: 'multiframe',
    frames: [
      { time_sec: 1 },
      { time_sec: 2 },
    ],
    evidence_timestamps_sec: [1, 2],
    limitations: ['frames far apart'],
    needs_human_review: true,
    audio_analyzed: false,
    video_analyzed: false,
  },
  request_usage: { total_tokens: 42 },
  is_current: true,
  human_verdict: '',
  human_note: '',
  human_at: null,
  error_code: '',
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
}

test('motion results are labelled as multi-frame inference, not video', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [motionReview],
  })
  setup()
  await screen.findByText(/多帧动态推断结果/)
  // The distinction from continuous video must be visible, not implied away.
  expect(screen.getByText('多帧动态推断，非连续视频')).toBeTruthy()
  expect(screen.getByText(/音频未分析/)).toBeTruthy()
  expect(screen.getByText(/证据时间戳/)).toBeTruthy()
  expect(screen.getByText('pan')).toBeTruthy()
})

test('an unknown motion verdict is marked for human confirmation', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [
      { ...motionReview, motion_type: 'unknown', stability_risk: 'unknown' },
    ],
  })
  setup()
  await screen.findByText('unknown')
  expect(screen.getByText('需人工确认')).toBeTruthy()
})

test('a recorded human verdict replaces the verdict buttons', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [{ ...motionReview, human_verdict: 'confirm' }],
  })
  setup()
  await screen.findByText(/人工已确认：confirm/)
  expect(screen.queryByRole('button', { name: '确认成立' })).toBeNull()
})

test('multi-frame review requires consent before it can be started', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { limit: 50, offset: 0, count: 0, items: [] },
    '/api/projects/p/vision-candidates': [candidate],
    '/api/projects/p/motion-reviews': [],
  })
  setup()
  const button = await screen.findByRole('button', { name: '多帧复核' })
  expect((button as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText(/需先勾选上方同意项/)).toBeTruthy()

  fireEvent.click(screen.getByRole('checkbox'))
  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: '多帧复核' }) as HTMLButtonElement).disabled,
    ).toBe(false),
  )
})

// T314-07: a verdict must be projected by evidence validity, never just truthiness.
test.each(['confirmed_current', 'needs_reconfirmation', 'not_reviewed'])(
  'human state %s is visible with audit history and notes', async (humanState) => {
    const history = humanState === 'not_reviewed' ? [] : [{
      id: 'v1', motion_review_id: 'm0', input_signature: 'sig', verdict: 'reject',
      note: 'original reviewed carefully', created_at: '2026-10-15T00:00:00Z',
      is_effective: humanState === 'confirmed_current',
    }]
    mockApi({
      '/api/vision/status': configuredStatus,
      '/api/projects/p/vision/preview': preview,
      '/api/projects/p/vision-results': { count: 0, items: [] },
      '/api/projects/p/vision-candidates': [],
      '/api/projects/p/motion-reviews': [{ ...motionReview,
        input_signature: 'sig', human_state: humanState,
        human_verdict: humanState === 'confirmed_current' ? 'reject' : '',
        human_note: humanState === 'confirmed_current' ? 'original reviewed carefully' : '',
        human_invalid_reason: humanState === 'needs_reconfirmation' ? 'EVIDENCE_CHANGED' : '',
        human_history: history,
      }],
    })
    setup()
    const labels = { confirmed_current: '当前有效', needs_reconfirmation: '证据改变，需重新确认', not_reviewed: '尚未人工判定' }
    await screen.findByText(labels[humanState as keyof typeof labels])
    if (history.length) {
      expect(screen.getByText(/历史判断/)).toBeTruthy()
      expect(screen.getAllByText(/original reviewed carefully/).length).toBeGreaterThan(0)
    }
    if (humanState !== 'confirmed_current') {
      expect(screen.getByRole('button', { name: '确认成立' })).toBeTruthy()
    }
  },
)

test('verdict sends the displayed evidence version and human note, and shows conflict', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [{ ...motionReview, input_signature: 'input-v1', human_state: 'not_reviewed', human_history: [] }],
  })
  const base = vi.mocked(api).getMockImplementation()!
  vi.mocked(api).mockImplementation((url, options) => String(url).endsWith('/verdict')
    ? Promise.reject(new Error('409: 证据已更新，请刷新后重新确认')) : base(url, options))
  setup()
  fireEvent.change(await screen.findByLabelText('人工判定备注'), { target: { value: 'checked source' } })
  fireEvent.click(screen.getByRole('button', { name: '确认成立' }))
  await screen.findByText(/证据已更新/)
  const call = vi.mocked(api).mock.calls.find(([url]) => String(url).endsWith('/verdict'))
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({
    verdict: 'confirm', note: 'checked source', motion_review_id: 'm1', input_signature: 'input-v1',
  })
})

test('coverage distinguishes planned, successful and cached shots', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [],
    '/api/projects/p/jobs/vision_analyze': { id: 'coverage-job', status: 'pending', stats: {} },
    '/api/jobs/coverage-job': { id: 'coverage-job', status: 'partial_failed', stats: {
      analyzed: 8, skipped: 2, failed: 1,
      coverage: { ...preview.coverage, eligible: 99, considered: 11, planned: 11,
        analyzed: 8, cached_valid: 2, not_covered: 89 },
    } },
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }))
  await screen.findByText(/实际成功理解 8 个/)
  expect(screen.getByText(/有效缓存 2 个/)).toBeTruthy()
  expect(screen.getByText(/计划分析 11 个/)).toBeTruthy()
})

test('insecure transport is labelled as a development exception', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [],
  })
  setup()
  await screen.findByText(/开发测试例外/)
  expect(screen.getByText(/生产验收须使用 HTTPS 或加密隧道/)).toBeTruthy()
})

test('visual job cleanup residuals stay visible after model success', async () => {
  mockApi({
    '/api/vision/status': configuredStatus,
    '/api/projects/p/vision/preview': preview,
    '/api/projects/p/vision-results': { count: 0, items: [] },
    '/api/projects/p/vision-candidates': [],
    '/api/projects/p/motion-reviews': [],
    '/api/projects/p/jobs/vision_analyze': { id: 'cleanup-job', status: 'pending', stats: {} },
    '/api/jobs/cleanup-job': { id: 'cleanup-job', status: 'succeeded', stats: {
      analyzed: 1, cleanup: { ok: false, residual_count: 1, residual_bytes: 123,
        error_code: 'CLEANUP_FAILED', residual_paths: ['vision/staging/job/frame.jpg'] },
    } },
  })
  setup()
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: '开始分析' }))
  await screen.findByText(/有暂存残留，需安全重试/)
  expect(screen.getByText(/123 字节/)).toBeTruthy()
})
