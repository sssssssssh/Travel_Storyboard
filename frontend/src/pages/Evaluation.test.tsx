// @vitest-environment jsdom
/**
 * TSB-371 front-end behaviour that a human depends on.
 *
 * The assertions here are about what the page must NOT do: claim an accuracy
 * before anyone rated, reveal which side is the model, or present a missing clip
 * as an empty player.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import FootageRating from './FootageRating'
import FootageReview from './FootageReview'
import StorylineAB from './StorylineAB'
import EvaluationReport from './EvaluationReport'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: vi.fn(),
  endpoints: {
    directorCandidates: (id: string) => `/api/projects/${id}/director/candidates`,
    directorPlanPreview: (id: string) => `/api/projects/${id}/director/plans/preview`,
    directorPlanInputStatus: (id: string) => `/api/projects/${id}/director/plans/input-status`,
    evaluationSamples: (id: string) => `/api/projects/${id}/evaluation/eligible-samples`,
    evaluationClips: (id: string) => `/api/projects/${id}/evaluation/clips`,
    evaluationSessions: (id: string) => `/api/projects/${id}/evaluation/sessions`,
    evaluationAbSessions: (id: string) => `/api/projects/${id}/evaluation/ab-sessions`,
    evaluationReport: (id: string, s: string) => `/api/projects/${id}/evaluation/reports/${s}`,
    frameImage: (f: string) => `/api/frames/${f}/image`,
  },
  withToken: (path: string) => `${path}?token=TEST-TOKEN`,
  frameUrl: (frameId: string | null | undefined) =>
    frameId ? `/api/frames/${frameId}/image?token=TEST-TOKEN` : '',
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

function wrap(path: string, el: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/project/:id/evaluation/rating" element={el} />
          <Route path="/project/:id/evaluation/ab" element={el} />
          <Route path="/project/:id/evaluation/report" element={el} />
          <Route path="/project/:id/evaluation/footage" element={el} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const ITEM = {
  item_id: 'i1', segment_id: 'seg-aaaa', asset_id: 'asset-1', channel: 'static',
  result_id: 'res-1111', evidence_state: 'freshness_unverified',
  source_window: { start_abs_sec: 1, end_abs_sec: 6 },
  source_available: true, review_status: 'PENDING' as const, version: 0,
  ratings: { scene: null, subject: null, action: null, timing: null,
             hallucination: null, usability: null, timing_error_sec: null },
}

const SESSION = {
  session_id: 's1', project_id: 'p1', status: 'IN_PROGRESS',
  sample_seed: 'default', sampling_rule_version: '1.0', created_at: null,
  sample_count: 1, rated_count: 0, items: [ITEM], context: {},
}

const SUMMARY_NONE = {
  evaluated_claims: 0, strict_accuracy: null, partial_or_better_rate: null,
  status: 'MANUAL_REVIEW_PENDING',
  hallucination: { severe: 0, mild: 0, judged: 0, rate: null },
}

// ---------------------------------------------------------------------------
// Rating page
// ---------------------------------------------------------------------------

test('the rating page shows N/A before any human rating', async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/summary')) return SUMMARY_NONE
    if (path.includes('/evaluation/sessions')) return SESSION
    return {}
  })
  wrap('/project/p1/evaluation/rating?session=s1', <FootageRating />)
  const s = await screen.findByTestId('live-summary')
  expect(s.textContent).toContain('N/A')
  expect(s.textContent).not.toContain('100.0%')
})

test('"cannot judge" is a separate button from "wrong"', async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/summary')) return SUMMARY_NONE
    return SESSION
  })
  wrap('/project/p1/evaluation/rating?session=s1', <FootageRating />)
  await screen.findByTestId('dim-scene')
  expect(screen.getByTestId('scene-score-null')).toBeTruthy()
  expect(screen.getByTestId('scene-score-0')).toBeTruthy()
})

test('clicking a score patches exactly one dimension', async () => {
  const calls: { path: string; body: unknown }[] = []
  vi.mocked(api).mockImplementation(async (_path: string, init?: RequestInit) => {
    if (_path.includes('/summary')) return SUMMARY_NONE
    if (init?.method === 'PATCH') {
      calls.push({ path: _path, body: JSON.parse(String(init.body)) })
    }
    return SESSION
  })
  wrap('/project/p1/evaluation/rating?session=s1', <FootageRating />)
  await screen.findByTestId('scene-score-2')
  fireEvent.click(screen.getByTestId('scene-score-2'))
  await waitFor(() => expect(calls).toHaveLength(1))
  expect(calls[0].body).toMatchObject({ scene: 2 })
})

test('the page states it does not modify annotations or the storyboard', async () => {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/summary')) return SUMMARY_NONE
    if (path.includes('/evaluation/sessions')) return SESSION
    return {}
  })
  wrap('/project/p1/evaluation/rating?session=s1', <FootageRating />)
  await screen.findByTestId('dim-scene')
  const body = document.body.textContent ?? ''
  expect(body).toContain('不会修改镜头标注')
})



// ---------------------------------------------------------------------------
// A/B page
// ---------------------------------------------------------------------------

const SIDE_A = {
  side: 'A', sections: [], shots: [], total_duration_sec: 10,
  target_duration_sec: 10, duration_gap_sec: 0, storyline: {},
}
const SIDE_B = { ...SIDE_A, side: 'B' }

test('the blind view shows no provenance before submitting', async () => {
  vi.mocked(api).mockImplementation(async () => ({
    session_id: 'ab1', project_id: 'p1', status: 'IN_PROGRESS', schema: '1.0',
    A: SIDE_A, B: SIDE_B, target_duration_sec: 10, revealed: false,
  }))
  wrap('/project/p1/evaluation/ab?session=ab1', <StorylineAB />)
  await screen.findByTestId('side-A')
  const body = document.body.textContent ?? ''
  expect(body).not.toContain('真实模型')
  expect(body).not.toContain('offline_rules')
})

test('a blocked A/B says so explicitly', async () => {
  vi.mocked(api).mockImplementation(async () => {
    throw Object.assign(new Error('blocked'), {
      status: 422, detail: { code: 'AB_BLOCKED_NO_REAL_MODEL_PLAN' },
    })
  })
  wrap('/project/p1/evaluation/ab?session=ab1', <StorylineAB />)
  const box = await screen.findByTestId('ab-blocked')
  expect(box.textContent).toContain('AB_BLOCKED_NO_REAL_MODEL_PLAN')
})

test('submitting reveals the mapping and states it is one reviewer', async () => {
  vi.mocked(api).mockImplementation(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      return {
        session_id: 'ab1', project_id: 'p1', status: 'SUBMITTED', revealed: true,
        mapping: { A: 'model', B: 'offline_rules' },
        submission: { preference: 'A', scores: {}, note: '' },
        winner_mode: 'model', reviewers: 1,
        interpretation: '仅有 1 位评价者：以下只代表该评价者的偏好，不是人群胜率。',
        A: SIDE_A, B: SIDE_B,
      }
    }
    return {
      session_id: 'ab1', project_id: 'p1', status: 'IN_PROGRESS', schema: '1.0',
      A: SIDE_A, B: SIDE_B, target_duration_sec: 10, revealed: false,
    }
  })
  wrap('/project/p1/evaluation/ab?session=ab1', <StorylineAB />)
  await screen.findByTestId('side-A')
  fireEvent.click(screen.getByTestId('pref-A'))
  fireEvent.click(screen.getByTestId('submit-ab'))
  await waitFor(() => expect(screen.getByTestId('reveal-panel')).toBeTruthy())
  expect(screen.getByTestId('single-reviewer-note').textContent).toContain('不是人群胜率')
})

// ---------------------------------------------------------------------------
// Report page
// ---------------------------------------------------------------------------

const REPORT = {
  session_id: 's1', project_code: 'p1abcd',
  scope: { n: 0, note: '小样本；不能代表全库准确率。' },
  human_metrics: {
    evaluated_claims: 0, strict_accuracy: null, partial_or_better_rate: null,
    by_dimension: { scene: { evaluated: 0, strict_accuracy: null, partial_or_better_rate: null } },
    hallucination: { severe: 0, mild: 0, judged: 0, rate: null },
    counts: {},
  },
  status: 'MANUAL_REVIEW_PENDING',
  gates: { G4_MODEL_PLAN: { status: 'BLOCKED', reason: '无新额度' } },
  ab: { status: 'AB_BLOCKED_NO_REAL_MODEL_PLAN', reason: '无真实模型方案' },
  limitations: ['小样本不代表全库'],
  disclaimer: 'N/A 表示尚无有效人工判断。',
}

test('the report prints N/A and never 100% without ratings', async () => {
  vi.mocked(api).mockImplementation(async () => REPORT)
  wrap('/project/p1/evaluation/report?session=s1', <EvaluationReport />)
  await screen.findByTestId('m-strict')
  expect(screen.getByTestId('m-strict').textContent).toBe('N/A')
  expect(document.body.textContent).not.toContain('100.0%')
  expect(screen.getByTestId('pending-note')).toBeTruthy()
})

test('the report exposes the blocked gates', async () => {
  vi.mocked(api).mockImplementation(async () => REPORT)
  wrap('/project/p1/evaluation/report?session=s1', <EvaluationReport />)
  await screen.findByTestId('gate-list')
  expect(screen.getByTestId('gate-list').textContent).toContain('G4_MODEL_PLAN')
  expect(screen.getByTestId('ab-status').textContent).toContain('AB_BLOCKED')
})


// ---------------------------------------------------------------------------
// TSB-371 regression: a black player because the token never reached <video>
// ---------------------------------------------------------------------------

test('the clip video src carries the access token', async () => {
  // The real bug: the page hand-rolled sessionStorage.getItem('tsb_token'), but the
  // token lives under 'tsb.accessToken', so the src had an empty token and the
  // browser got 401 -> black frame. Asserting the element exists was not enough.
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/clips')) {
      return {
        clip_id: 'c1', project_id: 'p1', segment_id: 'seg-1', asset_id: 'a1',
        status: 'ready', source_window: { start_abs_sec: 0, end_abs_sec: 4 },
        clip_start_abs_sec: 0, duration_sec: 4, audio_included: false,
        audio_note: '静音', max_clip_seconds: 15, source_signature_changed: false,
        segment_window_changed: false, created_at: 0, expires_in_sec: 100,
        media_url: '/api/projects/p1/evaluation/clips/c1/media',
      }
    }
    if (path.includes('/eligible-samples')) {
      return {
        project_id: 'p1', sampling_seed: 'd', sampling_rule_version: '1.0', limit: 5,
        items: [{
          item_id: 'i1', segment_id: 'seg-1', asset_id: 'a1', channel: 'static',
          result_id: 'r1', evidence_state: 'current',
          source_window: { start_abs_sec: 0, end_abs_sec: 4 },
          source_available: true, source_status: 'AVAILABLE', eligibility: 'eligible',
          eligible_for_planning: true, frame_ids: ['frame-1'],
          model: { provider: 'x', id: 'm', prompt_version: 'p' },
        }],
        controls: [],
        summary: {
          library_segments: 1, analysed_segments: 1, distinct_segments: 1,
          distinct_assets: 1, channel_counts: { static: 1 }, source_unavailable: 0,
          freshness_unverified: 0, item_count: 1,
        },
        limitations: [],
      }
    }
    return {}
  })
  wrap('/project/p1/evaluation/footage', <FootageReview />)
  const video = await screen.findByTestId('clip-video')
  const src = video.getAttribute('src') ?? ''
  expect(src).toContain('token=TEST-TOKEN')
  expect(src).not.toContain('token=&')
})
