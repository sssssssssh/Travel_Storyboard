// @vitest-environment jsdom
/**
 * TSB-368-B — the candidate browser must not blur the three observation channels
 * and must not offer to change anything.
 *
 * The page is read-only by design: the tests below assert that a render sends
 * only GET requests, and that "not analysed" never looks like "observed".
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import DirectorCandidates from './DirectorCandidates'
import { api } from '../lib/api'
import type {
  DirectorCandidate, DirectorCandidatesOut, ObservationOut, ObservationState,
} from '../lib/types'

vi.mock('../lib/api', () => ({
  api: vi.fn(),
  endpoints: {
    directorCandidates: (id: string) => `/api/projects/${id}/director/candidates`,
    directorContext: (id: string) => `/api/projects/${id}/director/context`,
  },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const obs = (state: ObservationState, resultId: string | null = null): ObservationOut => ({
  state, result_id: resultId, method: '', status: '',
  source_version: '', status_detail: {}, limitations: [],
})

function candidate(over: Partial<DirectorCandidate> = {}): DirectorCandidate {
  return {
    segment_id: 'seg-1', asset_id: 'asset-1', asset_display_name: 'clip1.mp4',
    source_window: { start_sec: 0, end_sec: 5 },
    detected_window: { start_sec: 0, end_sec: 5 },
    duration_sec: 5,
    eligibility: 'eligible',
    exclusion_reasons: [],
    human: { rating: 'pending', rating_source: 'segment', story_role: '', note: '' },
    technical: { quality_metrics: {}, duration_sec: 5, is_trimmed: false },
    observations: {
      static: obs('not_analyzed'),
      multiframe: obs('not_analyzed'),
      video_native: obs('not_analyzed'),
    },
    editorial_hints: { priority: 'unrated', reasons: [] },
    evidence_refs: [],
    limitations: ['no_valid_model_reading'],
    is_representative: true,
    ...over,
  }
}

function payload(items: DirectorCandidate[], over: Partial<DirectorCandidatesOut> = {}): DirectorCandidatesOut {
  return {
    project_id: 'p1', query_version: '1.0',
    total: items.length, offset: 0, limit: 20, items,
    coverage: {
      library_segments: items.length,
      eligible_segments: items.length,
      needs_review_segments: 0,
      excluded_segments: 0,
      static_valid: items.filter((i) => i.observations.static.result_id).length,
      multiframe_valid: 0,
      video_native_valid: items.filter((i) => i.observations.video_native.result_id).length,
      segments_with_any_valid_reading: items.filter(
        (i) => i.observations.static.result_id || i.observations.video_native.result_id,
      ).length,
      no_valid_model_reading: items.filter(
        (i) => !i.observations.static.result_id && !i.observations.video_native.result_id,
      ).length,
      coverage_complete: false,
    },
    limitations: ['partial_analysis_coverage'],
    ...over,
  }
}

function setup(body: DirectorCandidatesOut) {
  vi.mocked(api).mockResolvedValue(body)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/project/p1/director']}>
        <Routes>
          <Route path="/project/:id/director" element={<DirectorCandidates />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

test('an unanalysed shot is shown as 未分析 on all three channels', async () => {
  setup(payload([candidate()]))
  await screen.findByTestId('candidate-list')
  for (const name of ['静帧', '多帧', '原生视频']) {
    const badge = screen.getByTestId(`channel-${name}`)
    expect(badge.getAttribute('data-state')).toBe('not_analyzed')
    expect(badge.textContent).toContain('未分析')
  }
})

test('a static-only shot does not claim native video understanding', async () => {
  setup(payload([candidate({
    observations: {
      static: obs('freshness_unverified', 'r-static'),
      multiframe: obs('not_analyzed'),
      video_native: obs('not_analyzed'),
    },
  })]))
  await screen.findByTestId('candidate-list')
  expect(screen.getByTestId('channel-静帧').getAttribute('data-state')).toBe('freshness_unverified')
  expect(screen.getByTestId('channel-原生视频').getAttribute('data-state')).toBe('not_analyzed')
  expect(screen.getByTestId('channel-原生视频').textContent).toContain('未分析')
})

test('stale and failed states are visually distinct from current', async () => {
  setup(payload([
    candidate({
      segment_id: 'seg-stale',
      observations: {
        static: obs('stale', 'r1'),
        multiframe: obs('failed', 'r2'),
        video_native: obs('current', 'r3'),
      },
    }),
  ]))
  await screen.findByTestId('candidate-list')
  expect(screen.getByTestId('channel-静帧').textContent).toContain('已过期')
  expect(screen.getByTestId('channel-多帧').textContent).toContain('分析失败')
  expect(screen.getByTestId('channel-原生视频').textContent).toContain('已验证')
})

test('coverage shows numerator and denominator, not just a percentage', async () => {
  setup(payload([candidate(), candidate({ segment_id: 'seg-2' })], {
    coverage: {
      library_segments: 10, eligible_segments: 8, needs_review_segments: 1,
      excluded_segments: 1, static_valid: 2, multiframe_valid: 1, video_native_valid: 1,
      segments_with_any_valid_reading: 2, no_valid_model_reading: 8,
      coverage_complete: false,
    },
  }))
  const cov = await screen.findByTestId('coverage')
  expect(cov.textContent).toContain('10')
  expect(cov.textContent).toContain('2')
  expect(screen.getByTestId('coverage-incomplete').textContent).toContain('覆盖不完整')
})

test('human rating and story role are surfaced', async () => {
  setup(payload([candidate({
    human: { rating: 'favorite', rating_source: 'annotation', story_role: '开场', note: '' },
  })]))
  await screen.findByTestId('candidate-list')
  expect(screen.getByTestId('human-rating').textContent).toContain('favorite')
  expect(screen.getByText(/角色: 开场/)).toBeTruthy()
})

test('an excluded shot shows why it was excluded', async () => {
  setup(payload([candidate({
    eligibility: 'excluded', exclusion_reasons: ['human_excluded'],
  })], {
    coverage: {
      library_segments: 1, eligible_segments: 0, needs_review_segments: 0,
      excluded_segments: 1, static_valid: 0, multiframe_valid: 0, video_native_valid: 0,
      segments_with_any_valid_reading: 0, no_valid_model_reading: 1,
      coverage_complete: false,
    },
  }))
  await screen.findByTestId('candidate-list')
  expect(screen.getByTestId('reasons').textContent).toContain('human_excluded')
})

const EMPTY_COVERAGE = {
  library_segments: 0, eligible_segments: 0, needs_review_segments: 0,
  excluded_segments: 0, static_valid: 0, multiframe_valid: 0, video_native_valid: 0,
  segments_with_any_valid_reading: 0, no_valid_model_reading: 0,
  coverage_complete: false,
}

test('a project with no shots says so', async () => {
  setup(payload([], { total: 0, coverage: EMPTY_COVERAGE }))
  expect(await screen.findByTestId('empty-project')).toBeTruthy()
  expect(screen.queryByTestId('empty-filter')).toBeNull()
})

test('a filter with no hits is distinguished from an empty project', async () => {
  // The project HAS shots; the current filter simply matched none of them.
  setup(payload([], {
    total: 0,
    coverage: { ...EMPTY_COVERAGE, library_segments: 12, eligible_segments: 12 },
  }))
  expect(await screen.findByTestId('empty-filter')).toBeTruthy()
  expect(screen.queryByTestId('empty-project')).toBeNull()
})

test('only GET requests are issued — the page cannot modify anything', async () => {
  setup(payload([candidate()]))
  await screen.findByTestId('candidate-list')
  // Change a filter to force another request.
  fireEvent.change(screen.getByTestId('filter-eligibility'), { target: { value: 'all' } })
  await waitFor(() => expect(vi.mocked(api).mock.calls.length).toBeGreaterThanOrEqual(2))

  for (const call of vi.mocked(api).mock.calls) {
    const init = call[1] as RequestInit | undefined
    expect(init?.method ?? 'GET').toBe('GET')
    expect(String(call[0])).toContain('/director/candidates')
  }
})

test('there is no control that could write to the storyboard', async () => {
  setup(payload([candidate()]))
  await screen.findByTestId('candidate-list')
  const labels = screen.getAllByRole('button').map((b) => b.textContent ?? '')
  for (const forbidden of ['应用', '保存', '生成', '确认']) {
    expect(labels.some((l) => l.includes(forbidden))).toBe(false)
  }
})

test('a filter change resets the page offset', async () => {
  setup(payload([candidate()], { total: 40 }))
  await screen.findByTestId('candidate-list')
  fireEvent.click(screen.getByTestId('next-page'))
  await waitFor(() => {
    expect(vi.mocked(api).mock.calls.some((c) => String(c[0]).includes('offset=20'))).toBe(true)
  })
  fireEvent.change(screen.getByTestId('filter-rating'), { target: { value: 'favorite' } })
  await waitFor(() => {
    const calls = vi.mocked(api).mock.calls
    const last = String(calls[calls.length - 1]?.[0])
    expect(last).toContain('offset=0')
  })
})

test('a load failure shows an error state rather than an empty list', async () => {
  vi.mocked(api).mockRejectedValue(new Error('boom'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/project/p1/director']}>
        <Routes>
          <Route path="/project/:id/director" element={<DirectorCandidates />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  const err = await screen.findByTestId('director-error')
  expect(err.textContent).toContain('加载候选镜头失败')
})

test('a shot a human judged wrong is marked, and unreviewed stays silent', async () => {
  // The review must be visible where the choice is made. "unreviewed" must not
  // render anything: absence of judgement is not rejection.
  const wrong = candidate({ segment_id: 'seg-wrong', review_state: 'wrong',
                            review_reasons: ['review_judged_wrong'] })
  const trusted = candidate({ segment_id: 'seg-trusted', review_state: 'trusted' })
  const untouched = candidate({ segment_id: 'seg-none' })

  setup(payload([wrong, trusted, untouched], {
    total: 3,
    limitations: ['some_shots_were_judged_wrong_by_a_human_reviewer'],
  }))

  const mark = await screen.findByTestId('review-wrong')
  expect(mark.textContent).toContain('人工判错')
  expect(screen.getByTestId('review-trusted').textContent).toContain('人工核对通过')
  // Exactly one wrong marker for three shots.
  expect(screen.getAllByTestId('review-wrong')).toHaveLength(1)
})
