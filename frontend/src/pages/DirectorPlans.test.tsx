// @vitest-environment jsdom
/**
 * TSB-369-C — the plan page must be visibly a *draft* and provably read-only.
 *
 * The two failure modes this guards against are not crashes:
 *   - a page that quietly offers to save the plan into the storyboard;
 *   - a page that presents structural suggestions as if the system had watched
 *     the footage.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import DirectorPlans, { buildMarkdown } from './DirectorPlans'
import { api } from '../lib/api'
import type { PlanDraft, PlanShot, PlanVariant } from '../lib/types'

vi.mock('../lib/api', () => ({
  api: vi.fn(),
  fetchAllSegments: vi.fn(async () => []),
  endpoints: {
    directorCandidates: (id: string) => `/api/projects/${id}/director/candidates`,
    directorContext: (id: string) => `/api/projects/${id}/director/context`,
    directorPlanPreview: (id: string) => `/api/projects/${id}/director/plans/preview`,
    directorPlanInputStatus: (id: string) => `/api/projects/${id}/director/plans/input-status`,
  },
}))

const DIGEST = 'f'.repeat(64)
const inputStatus = {
  project_id: 'p1',
  input_digest: DIGEST,
  max_candidates: 40,
  coverage: { library_segments: 99, segments_with_any_valid_reading: 10,
              no_valid_model_reading: 89, video_native_valid: 0 },
  withheld: {}, limitations: [],
  base_storyboard_version_id: null, storyboard_absent: true,
  segment_count: 99, segments_with_any_valid_reading: 10, locked_segments: 0,
  evidence_channels: { static: 8, multiframe: 4, video_native: 0 },
}

afterEach(() => {
  cleanup()
  localStorage.clear()
  vi.clearAllMocks()
})

test('TSB-372 saves an execution revision while preserving the original cut', async () => {
  setup()
  await generate()
  fireEvent.click(screen.getByTestId('trim-shorter'))
  fireEvent.click(screen.getByTestId('save-execution-version'))
  expect(screen.getByTestId('execution-diff').textContent).toContain('截取区间')
  cleanup()
  setup()
  fireEvent.click(screen.getByTestId('open-execution-version'))
  expect(screen.getByTestId('shot-duration').textContent).toContain('4.5')
  fireEvent.click(screen.getByTestId('show-original-version'))
  expect(screen.getByTestId('shot-duration').textContent).toContain('5.0')
})

test('R1 keeps an invalid window draft and blocks export until Escape restores it', async () => {
  setup()
  await generate()
  fireEvent.click(screen.getByTestId('toggle-detail'))
  const field = screen.getByLabelText('源片入点（秒）') as HTMLInputElement
  fireEvent.change(field, { target: { value: '' } })
  fireEvent.blur(field)
  expect(field.value).toBe('')
  expect((screen.getByTestId('export-json') as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByTestId('toggle-detail'))
  fireEvent.click(screen.getByTestId('toggle-detail'))
  const restored = screen.getByLabelText('源片入点（秒）') as HTMLInputElement
  expect(restored.value).toBe('')
  fireEvent.keyDown(restored, { key: 'Escape' })
  expect(restored.value).toBe('0')
  expect((screen.getByTestId('export-json') as HTMLButtonElement).disabled).toBe(false)
})

test.each([
  ['错误', '非负秒数'],
  ['6', '出点必须晚于入点'],
  ['-1', '非负秒数'],
  ['4.99', '至少需要 0.05'],
])('R1 preserves invalid entry %s and its error', async (value, message) => {
  setup()
  await generate()
  fireEvent.click(screen.getByTestId('toggle-detail'))
  const field = screen.getByLabelText('源片入点（秒）') as HTMLInputElement
  fireEvent.change(field, { target: { value } })
  fireEvent.keyDown(field, { key: 'Enter' })
  expect(field.value).toBe(value)
  expect(screen.getByRole('alert').textContent).toContain(message)
  expect((screen.getByTestId('export-md') as HTMLButtonElement).disabled).toBe(true)
})

test('R1 precise entry retimes following cuts and matches both downloads and saved version', async () => {
  const first = shot()
  const next = shot({ shot_id: 'second', segment_id: 'seg-b', source_in_sec: 10,
    source_out_sec: 15, timeline_start_sec: 5 })
  setup(draft({ variants: [variant({ shots: [first, next], total_duration_sec: 10 })] }))
  await generate()
  const row = within(screen.getByTestId('plan-shot-0'))
  fireEvent.click(row.getByTestId('toggle-detail'))
  const start = row.getByLabelText('源片入点（秒）') as HTMLInputElement
  const end = row.getByLabelText('源片出点（秒）') as HTMLInputElement
  fireEvent.change(end, { target: { value: '9' } })
  fireEvent.blur(end)
  expect(screen.getByRole('alert').textContent).toContain('当前可用镜头范围')
  fireEvent.change(end, { target: { value: '5' } })
  fireEvent.change(start, { target: { value: '0.25' } })
  fireEvent.blur(start)
  expect(row.getByTestId('shot-duration').textContent).toBe('4.75s')
  fireEvent.change(start, { target: { value: '0.12' } })
  fireEvent.change(start, { target: { value: '.1234' } })
  fireEvent.change(end, { target: { value: '4.1256' } })
  fireEvent.keyDown(end, { key: 'Enter' })
  expect(start.value).toBe('0.1234')
  expect(end.value).toBe('4.1256')
  expect(screen.getByTestId('plan-shot-1').textContent).toContain('时间轴 4.0022s')
  expect(screen.getByTestId('variant-total').textContent).toContain('9.0022')
  fireEvent.click(row.getByText('保留镜头'))
  expect(start.disabled && end.disabled).toBe(true)
  expect((row.getByText('移出方案') as HTMLButtonElement).disabled).toBe(true)

  const downloads: Blob[] = []
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL(payload: Blob) { downloads.push(payload); return 'blob:unit-test' }
    static revokeObjectURL() {}
  })
  try {
    fireEvent.click(screen.getByTestId('export-json'))
    fireEvent.click(screen.getByTestId('export-md'))
  } finally {
    vi.unstubAllGlobals()
    click.mockRestore()
  }
  function text(blob: Blob): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = reject
      reader.readAsText(blob)
    })
  }
  const json = JSON.parse(await text(downloads[0]))
  expect(json.variants[0].shots[0]).toMatchObject({
    source_in_sec: 0.1234, source_out_sec: 4.1256, planned_duration_sec: 4.0022,
    timeline_start_sec: 0, retained: true,
  })
  expect(json.variants[0].duration_gap_sec).toBe(80.9978)
  expect(await text(downloads[1])).toContain('源片 0.1234–4.1256s')
  fireEvent.click(screen.getByTestId('save-execution-version'))
  const saved = JSON.parse(localStorage.getItem('tsb.director.execution.p1')!)
  expect(saved[0].variants).toEqual(json.variants)
  expect(saved[0].original.variants[0].shots[0]).toEqual(first)
})

function shot(over: Partial<PlanShot> = {}): PlanShot {
  return {
    shot_id: 'v1-s01',
    segment_id: 'seg-1111-2222',
    asset_id: 'asset-1',
    section_id: 'sec-opening',
    source_in_sec: 0,
    source_out_sec: 5,
    planned_duration_sec: 5,
    timeline_start_sec: 0,
    narrative_role: 'opening',
    selection_reason: '有静帧观察',
    claims: [],
    evidence_refs: [],
    needs_review: [],
    transition_hint: '建议直接切入',
    pacing_hint: '建议中等节奏',
    audio_hint: '',
    ...over,
  }
}

function variant(over: Partial<PlanVariant> = {}): PlanVariant {
  return {
    variant_id: 'v1',
    title: '以旅程推进为线索',
    narrative_angle: 'chronological',
    logline: '按素材库既有顺序串起结构',
    status: 'provisional',
    sections: [{ section_id: 'sec-opening', kind: 'opening', title: '开场', narrative_role: 'opening' }],
    shots: [shot()],
    total_duration_sec: 5,
    target_duration_sec: 90,
    duration_gap_sec: 85,
    review_items: [],
    differences: ['按素材与时间顺序排列'],
    ...over,
  }
}

function draft(over: Partial<PlanDraft> = {}): PlanDraft {
  return {
    schema_version: '1.0',
    project_id: 'p1',
    planning_mode: 'offline_rules',
    generation_status: 'provisional',
    source_input_digest: 'a'.repeat(64),
    base_storyboard_version_id: null,
    coverage: {
      library_segments: 99,
      segments_with_any_valid_reading: 10,
      no_valid_model_reading: 89,
      video_native_valid: 0,
    },
    limitations: ['offline_rules_structural_only'],
    variants: [variant()],
    withheld: { needs_review_segments: 0, excluded_segments: 0 },
    needs_more_analysis: true,
    warnings: [],
    ...over,
  }
}

const candidatesPayload = {
  project_id: 'p1', query_version: '1.0', total: 99, offset: 0, limit: 100,
  items: [], limitations: [],
  coverage: {
    library_segments: 99, eligible_segments: 99, needs_review_segments: 0,
    excluded_segments: 0, static_valid: 8, multiframe_valid: 4, video_native_valid: 0,
    segments_with_any_valid_reading: 10, no_valid_model_reading: 89,
    coverage_complete: false,
  },
}

function setup(planResponse: PlanDraft | (() => Promise<PlanDraft>) = draft()) {
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/director/plans/input-status')) return inputStatus
    if (path.includes('/director/candidates')) return candidatesPayload
    if (path.includes('/director/plans/preview')) {
      if (typeof planResponse === 'function') return planResponse()
      return planResponse
    }
    return {}
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/project/p1/director/plan']}>
        <Routes>
          <Route path="/project/:id/director/plan" element={<DirectorPlans />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function generate() {
  // TSB-370-0: generation is disabled until the input digest is loaded. The old
  // helper clicked immediately, which is exactly the state the bug allowed.
  await waitFor(() =>
    expect((screen.getByTestId('generate') as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByTestId('generate'))
  await screen.findByTestId('variant-title')
}

async function clickGenerateExpecting(selector: string) {
  await waitFor(() =>
    expect((screen.getByTestId('generate') as HTMLButtonElement).disabled).toBe(false))
  fireEvent.click(screen.getByTestId('generate'))
  return screen.findByTestId(selector)
}

test('coverage is shown as counts, including the unanalysed shots', async () => {
  setup()
  const line = screen.getByTestId('coverage-line')
  await waitFor(() => expect(line.textContent).toContain('99'))
  expect(line.textContent).toContain('99')
  expect(line.textContent).toContain('10')
  expect(line.textContent).toContain('89')
})

test('the page states it is offline rules, not an AI reading', async () => {
  setup()
  await generate()
  const status = screen.getByTestId('draft-status')
  expect(status.textContent).toContain('offline_rules')
  expect(screen.getByTestId('readonly-note').textContent).toContain('不保存')
})

test('there is no control that could write the plan back', async () => {
  setup()
  await generate()
  const labels = screen.getAllByRole('button').map((b) => b.textContent ?? '')
  for (const forbidden of ['应用', '保存', '提交', '写入正式']) {
    expect(labels.some((l) => l.includes(forbidden))).toBe(false)
  }
})

test('generating issues only the preview POST, and only read-only GETs otherwise', async () => {
  setup()
  await generate()
  const calls = vi.mocked(api).mock.calls
  const posts = calls.filter((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
  // Exactly one write-shaped call: the preview. The digest GET must never be a POST.
  expect(posts).toHaveLength(1)
  expect(String(posts[0][0])).toContain('/plans/preview')

  const allowedGets = ['/director/candidates', '/director/plans/input-status']
  for (const c of calls) {
    const m = (c[1] as RequestInit | undefined)?.method ?? 'GET'
    if (m !== 'GET') continue
    expect(allowedGets.some((p) => String(c[0]).includes(p))).toBe(true)
  }
  // The input-status read must actually have happened (TSB-370-0).
  expect(calls.some((c) => String(c[0]).includes('/plans/input-status'))).toBe(true)
})

test('a shot with no reading shows no invented description', async () => {
  setup()
  await generate()
  fireEvent.click(screen.getByTestId('toggle-detail'))
  expect(screen.getByTestId('no-claims').textContent).toContain('没有任何模型读数')
  expect(screen.queryAllByTestId('claim')).toHaveLength(0)
})

test('claims are labelled by kind so a suggestion is not read as an observation', async () => {
  const v = variant({
    shots: [shot({
      claims: [
        { text: '静帧观察：场景标签 nature', kind: 'observed_signal',
          evidence_refs: [{ channel: 'static', result_id: 'r1', segment_id: 'seg-1111-2222',
                            evidence_state: 'observed', event_start_sec: null, event_end_sec: null }] },
        { text: '建议以留白收束', kind: 'editorial_suggestion', evidence_refs: [] },
      ],
    })],
  })
  setup(draft({ variants: [v] }))
  await generate()
  fireEvent.click(screen.getByTestId('toggle-detail'))
  expect(screen.getByTestId('claim-kind-observed_signal').textContent).toContain('观察')
  expect(screen.getByTestId('claim-kind-editorial_suggestion').textContent).toContain('建议')
  expect(screen.getAllByTestId('claim')).toHaveLength(2)
})

test('needs_review items are surfaced on the shot', async () => {
  setup(draft({ variants: [variant({ shots: [shot({ needs_review: ['未分析音频'] })] })] }))
  await generate()
  expect(screen.getByTestId('shot-needs-review').textContent).toContain('未分析音频')
})

test('reordering recomputes the timeline locally', async () => {
  const shots = [
    shot({ shot_id: 'a', segment_id: 'seg-a', source_out_sec: 4, planned_duration_sec: 4 }),
    shot({ shot_id: 'b', segment_id: 'seg-b', source_in_sec: 10, source_out_sec: 13,
           planned_duration_sec: 3, timeline_start_sec: 4 }),
  ]
  setup(draft({ variants: [variant({ shots })] }))
  await generate()
  expect(screen.getByTestId('plan-shot-0').textContent).toContain('seg-a')
  fireEvent.click(screen.getAllByTestId('move-down')[0])
  await waitFor(() => {
    const first = screen.getByTestId('plan-shot-0')
    expect(first.textContent).toContain('seg-b')
  })
})

test('trimming shortens the shot and shifts the timeline', async () => {
  const shots = [
    shot({ shot_id: 'a', segment_id: 'seg-a', source_in_sec: 0, source_out_sec: 5,
           planned_duration_sec: 5 }),
    shot({ shot_id: 'b', segment_id: 'seg-b', source_in_sec: 10, source_out_sec: 13,
           planned_duration_sec: 3, timeline_start_sec: 5 }),
  ]
  setup(draft({ variants: [variant({ shots })] }))
  await generate()
  fireEvent.click(screen.getAllByTestId('trim-shorter')[0])
  await waitFor(() => {
    const second = screen.getByTestId('plan-shot-1')
    expect(second.textContent).toContain('时间轴 4.5s')
  })
})

test('insufficient evidence is shown as such rather than an empty page', async () => {
  setup(draft({ generation_status: 'insufficient_evidence', variants: [] }))
  const box = await clickGenerateExpecting('insufficient')
  expect(box.textContent).toContain('没有编造')
})

test('a 409 keeps the user theme and shows a conflict hint', async () => {
  setup(() => Promise.reject(Object.assign(new Error('输入已变化'), { status: 409 })))
  fireEvent.change(screen.getByTestId('input-theme'), { target: { value: '海边日落' } })
  const err = await clickGenerateExpecting('plan-error')
  expect(err.textContent).toBeTruthy()
  await waitFor(() => expect(screen.queryByTestId('conflict-hint')).toBeTruthy())
  expect((screen.getByTestId('input-theme') as HTMLInputElement).value).toBe('海边日落')
})

test('markdown export declares itself a read-only draft', () => {
  const md = buildMarkdown(draft(), [variant()])
  expect(md).toContain('剪辑结构草案')
  expect(md).toContain('未写入任何正式剪辑计划')
  expect(md).toContain('99')
  expect(md).toContain('89')
})

test('markdown export carries claim kinds and evidence channels', () => {
  const v = variant({
    shots: [shot({
      claims: [{ text: '静帧观察：nature', kind: 'observed_signal',
                 evidence_refs: [{ channel: 'static', result_id: 'r1', segment_id: 's',
                                   evidence_state: 'observed', event_start_sec: null,
                                   event_end_sec: null }] }],
    })],
  })
  const md = buildMarkdown(draft({ variants: [v] }), [v])
  expect(md).toContain('[观察]')
  expect(md).toContain('证据：static')
})

test('the export text never mentions the theme as footage content', () => {
  const d = draft({ project_id: 'p1' })
  const md = buildMarkdown(d, [variant()])
  expect(md).not.toContain('极光')
  expect(md).not.toContain('雪山')
})


// ---------------------------------------------------------------------------
// TSB-370-0 — the digest must actually reach the server
// ---------------------------------------------------------------------------

test('the preview request carries the digest from input-status', async () => {
  setup()
  await generate()
  const post = vi.mocked(api).mock.calls.find(
    (c) => (c[1] as RequestInit | undefined)?.method === 'POST')
  const body = JSON.parse(String((post?.[1] as RequestInit).body))
  expect(body.expected_input_digest).toBe(DIGEST)
  expect(body.expected_input_digest).not.toBeUndefined()
})

test('generation is disabled until a digest exists', async () => {
  let resolveStatus: (v: unknown) => void = () => {}
  const pending = new Promise((r) => { resolveStatus = r })
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/plans/input-status')) return pending
    if (path.includes('/director/candidates')) return candidatesPayload
    return {}
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/project/p1/director/plan']}>
        <Routes>
          <Route path="/project/:id/director/plan" element={<DirectorPlans />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await waitFor(() =>
    expect((screen.getByTestId('generate') as HTMLButtonElement).disabled).toBe(true))
  resolveStatus(inputStatus)
  await waitFor(() =>
    expect((screen.getByTestId('generate') as HTMLButtonElement).disabled).toBe(false))
})

test('a 409 clears the old draft and offers a reload', async () => {
  setup()
  await generate()
  expect(screen.getByTestId('variant-title')).toBeTruthy()

  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path.includes('/plans/input-status')) return inputStatus
    if (path.includes('/director/candidates')) return candidatesPayload
    if (path.includes('/plans/preview')) {
      throw Object.assign(new Error('输入已变化'), {
        status: 409, detail: { code: 'DIRECTOR_INPUT_CHANGED' } })
    }
    return {}
  })
  fireEvent.click(screen.getByTestId('generate'))
  await screen.findByTestId('plan-error')
  // The old draft must not survive the conflict.
  expect(screen.queryByTestId('variant-title')).toBeNull()
  expect(screen.getByTestId('stale-banner')).toBeTruthy()
  expect(screen.getByTestId('reload-input')).toBeTruthy()
})

test('the digest request uses the chosen max_candidates', async () => {
  setup()
  await waitFor(() =>
    expect((screen.getByTestId('generate') as HTMLButtonElement).disabled).toBe(false))
  fireEvent.change(screen.getByTestId('input-max-candidates'), { target: { value: '60' } })
  await waitFor(() => {
    const hit = vi.mocked(api).mock.calls.some((c) =>
      String(c[0]).includes('/plans/input-status') && String(c[0]).includes('max_candidates=60'))
    expect(hit).toBe(true)
  })
})
