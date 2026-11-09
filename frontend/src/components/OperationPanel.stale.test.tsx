// @vitest-environment jsdom
/**
 * TSB-368-0 — UAT-07: the operation panel must not present a stale proposal as
 * if it targeted the user's current selection.
 *
 * Root cause: `pendingOps` lives in Export, but `proposal` / `error` / `conflict`
 * live inside OperationPanel. Changing the selection updates the `targetLabel`
 * prop while the child keeps rendering the *previous* proposal, so the panel
 * says "目标：镜头 B" above a diff that actually belongs to shot A.
 *
 * The server is not at fault — it applies the proposal's own shot_id — but the
 * user can confirm an operation believing it targets B. These tests pin the UI
 * contract regardless of what the server does.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Export from '../pages/Export'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: vi.fn(),
  fetchAllSegments: vi.fn(async () => []),
  endpoints: {
    editPlan: () => '/plan',
    exportCsv: () => '/csv',
    exportJson: () => '/json',
    exportOtio: () => '/otio',
    storyboard: () => '/board',
    storyboardVersions: () => '/versions',
    storyboardVersion: () => '/version',
    storyboardRestore: () => '/restore',
    planOperations: () => '/ops',
    planOperationApply: (_p: string, _b: string, o: string) => `/ops/${o}/apply`,
    planOperationReject: (_p: string, _b: string, o: string) => `/ops/${o}/reject`,
    migrationStatus: () => '/migration-status',
    retryMigration: () => '/retry-migration',
  },
  downloadUrl: vi.fn(),
  mediaUrl: vi.fn(),
  roughcutUrl: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const SHOTS = ['shot-A', 'shot-B', 'shot-C']

function baseShot(i: number) {
  return {
    asset_id: 'a', segment_id: `s${i}`,
    source_in_sec: i * 10, source_out_sec: i * 10 + 4, target_duration_sec: 3,
    story_role: 'role', transition_hint: 'fade', music_cue: 'piano',
    audio_hint: 'ambient', color_hint: 'warm', evidence: 'dynamic',
    confidence: 0.9, review_status: 'confirmed', order: i,
    shot_id: SHOTS[i],
  }
}

/** A proposal whose diff clearly belongs to ONE shot id. */
function proposalFor(shotId: string, kind: 'trim' | 'lock' = 'trim') {
  return {
    operation_id: `op-${shotId}`,
    project_id: 'x', board_id: 'b1',
    base_version_id: 'v1', result_version_id: null,
    status: 'proposed', schema_version: 1,
    operations: [{ type: kind === 'trim' ? 'trim_shot' : 'lock_shot', shot_id: shotId }],
    diff: {
      shots: {
        added: [], removed: [], reordered: [], replaced: [],
        trimmed: kind === 'trim'
          ? [{ shot_id: shotId, before: { in: 0, out: 4 }, after: { in: 0, out: 3.5 } }]
          : [],
        locked: kind === 'lock'
          ? [{ shot_id: shotId, before: [], after: ['range'] }]
          : [],
      },
      sections: { added: [], removed: [], reordered: false },
      constraints: { changed: false },
    },
    reason: 'manual', actor: 'user', error_code: '', error_detail: {},
    created_at: null, updated_at: null, requires_confirmation: true,
  }
}

type Options = {
  proposeDelayMs?: number
  proposeStatus?: number
  onPropose?: () => void
}

function setup(options: Options = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const planBody = {
    shots: SHOTS.map((_, i) => baseShot(i)),
    version_id: 'v1', version_no: 1, board_id: 'b1',
  }
  const boardBody = {
    board_id: 'b1', project_id: 'x', current_version_id: 'v1', version_id: 'v1',
    version_no: 1, parent_version_id: null, schema_version: '1.0', created_at: null,
    actor: 'user', reason: 'initial', job_id: null, restore_of_version_id: null,
    brief: {}, sections: [], shots: [], constraints: {},
    validation_summary: { state: 'ok', issues: [] },
  }

  vi.mocked(api).mockImplementation(async (path: string, init?: RequestInit) => {
    if (path === '/board') return boardBody
    if (path === '/versions') return [boardBody]
    if (path === '/ops' && init?.method === 'POST') {
      const body = JSON.parse(String(init.body)) as {
        operations: { shot_id?: string; type?: string }[]
      }
      const targetShot = body.operations?.[0]?.shot_id ?? 'shot-A'
      const kind = body.operations?.[0]?.type === 'lock_shot' ? 'lock' : 'trim'
      options.onPropose?.()
      if (options.proposeDelayMs) {
        await new Promise((r) => setTimeout(r, options.proposeDelayMs))
      }
      if (options.proposeStatus && options.proposeStatus >= 400) {
        const err = Object.assign(new Error('conflict'), {
          status: options.proposeStatus,
        })
        throw err
      }
      return proposalFor(targetShot, kind)
    }
    if (path.includes('/apply')) {
      return { ...proposalFor('shot-A'), status: 'applied', result_version_id: 'v2' }
    }
    if (path.includes('/reject')) {
      return { ...proposalFor('shot-A'), status: 'rejected' }
    }
    return planBody
  })

  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/p/x']}>
        <Routes>
          <Route path="/p/:id" element={<Export />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return qc
}

async function selectShot(index: number) {
  const rows = await screen.findAllByTestId(/^draft-shot-/)
  fireEvent.click(rows[index])
  await waitFor(() => {
    expect(screen.getByTestId('selected-shot').textContent).toContain(
      SHOTS[index].slice(0, 8),
    )
  })
}

const hasDiff = () => screen.queryAllByTestId('operation-diff').length > 0
const diffText = () =>
  hasDiff() ? screen.getByTestId('operation-diff').textContent ?? '' : ''
const canApply = () => {
  const btn = screen.queryAllByTestId('operation-apply')
  return btn.length > 0 && !(btn[0] as HTMLButtonElement).disabled
}

async function proposeAndWait(shotIndex: number) {
  await selectShot(shotIndex)
  fireEvent.click(screen.getByRole('button', { name: '裁剪（缩短 0.5s）' }))
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(hasDiff()).toBe(true))
}

function postPaths(): string[] {
  return vi.mocked(api).mock.calls
    .filter((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    .map((c) => String(c[0]))
}

describe('UAT-07 stale proposal isolation', () => {
  test('switching selection drops the previous proposal instead of retargeting it', async () => {
    setup()
    await proposeAndWait(0)
    expect(diffText()).toContain(SHOTS[0].slice(0, 8))

    await selectShot(1)

    // The A diff must be gone: it must not still be on screen under a B label.
    await waitFor(() => expect(hasDiff()).toBe(false))
    expect(diffText()).not.toContain(SHOTS[0].slice(0, 8))
    // ...and there must be nothing the user can blindly confirm.
    expect(canApply()).toBe(false)
    // Switching selection must never itself issue a request.
    expect(postPaths().filter((p) => p.includes('/apply'))).toHaveLength(0)
  })

  test('changing the operation type on the same shot drops the old proposal', async () => {
    setup()
    await proposeAndWait(0)
    const trimDiff = diffText()

    // Same shot, different toolbar action.
    fireEvent.click(screen.getByRole('button', { name: '移除' }))

    await waitFor(() => expect(hasDiff()).toBe(false))
    expect(diffText()).not.toBe(trimDiff)
    expect(canApply()).toBe(false)
  })

  test('a late propose response cannot resurrect a panel after the target changed', async () => {
    let released!: () => void
    const gate = new Promise<void>((r) => { released = r })
    const options: Options = {
      onPropose: () => { void gate },
    }
    setup(options)

    await selectShot(0)
    fireEvent.click(screen.getByRole('button', { name: '裁剪（缩短 0.5s）' }))
    fireEvent.click(screen.getByTestId('operation-propose'))
    // The request is in flight; now the user moves on.
    await selectShot(1)
    released()
    await new Promise((r) => setTimeout(r, 60))

    // Whatever the late response said, it must not own the panel now.
    expect(diffText()).not.toContain(SHOTS[0].slice(0, 8))
    expect(canApply()).toBe(false)
  })

  test('a local proposal is never applied automatically', async () => {
    setup()
    await proposeAndWait(0)
    // Give timers a chance to fire any accidental auto-apply.
    await new Promise((r) => setTimeout(r, 80))
    expect(postPaths().filter((p) => p.includes('/apply'))).toHaveLength(0)
  })

  test('rejecting a proposal leaves the storyboard version untouched', async () => {
    setup()
    await proposeAndWait(0)
    const rejectBtn = screen.queryAllByTestId('operation-reject')
    expect(rejectBtn.length).toBeGreaterThan(0)
    fireEvent.click(rejectBtn[0])
    await waitFor(() => expect(hasDiff()).toBe(false))
    expect(postPaths().filter((p) => p.includes('/apply'))).toHaveLength(0)
  })

  test('a dirty draft survives a selection change', async () => {
    setup()
    const rows = await screen.findAllByTestId(/^draft-shot-/)
    const input = rows[0].querySelectorAll('input[type="number"]')[1] as HTMLInputElement
    const original = input.value
    fireEvent.change(input, { target: { value: '2.75' } })
    expect(input.value).toBe('2.75')

    await selectShot(1)

    const rowsAfter = await screen.findAllByTestId(/^draft-shot-/)
    const sameInput = rowsAfter[0].querySelectorAll('input[type="number"]')[1] as HTMLInputElement
    expect(sameInput.value).toBe('2.75')
    expect(sameInput.value).not.toBe(original)
  })

  test('a 409 keeps the user draft and shows a conflict instead of replaying', async () => {
    setup({ proposeStatus: 409 })
    await selectShot(0)
    fireEvent.click(screen.getByRole('button', { name: '裁剪（缩短 0.5s）' }))
    fireEvent.click(screen.getByTestId('operation-propose'))

    await waitFor(() => {
      const conflict = screen.queryAllByTestId('operation-conflict')
      expect(conflict.length).toBeGreaterThan(0)
    })
    expect(canApply()).toBe(false)
    // A conflict must never be retried automatically.
    const proposeCalls = postPaths().filter((p) => p === '/ops')
    expect(proposeCalls).toHaveLength(1)
  })
})
