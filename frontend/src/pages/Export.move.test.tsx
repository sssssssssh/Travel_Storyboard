// @vitest-environment jsdom
/**
 * TSB-367-UAT-01 — UAT-F01: the move up/down buttons build the wrong anchor.
 *
 * Server contract (backend `_apply_move_shot`):
 *   after_shot_id = B  ⇒ target goes *after* B
 *   before_shot_id = B ⇒ target goes *before* B
 *
 * With [A, B, C] and B selected:
 *   up   must produce [B, A, C]  ⇒ B goes before A
 *   down must produce [A, C, B]  ⇒ B goes after C
 *
 * The reference implementation did the opposite of that for "up" (it anchored
 * to the previous shot with `after`, which is a no-op) and, on the last row,
 * made "down" move the shot *up* using `before` on the previous shot.
 *
 * These tests assert the *request the panel would send*, which is the only way
 * to catch a wrong-anchor bug from the UI side: a no-op move still yields a
 * perfectly "successful" API call.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Export from './Export'
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

const SHOT_IDS = ['shot-A', 'shot-B', 'shot-C']

function baseShot(i: number) {
  return {
    asset_id: 'a', segment_id: `s${i}`,
    source_in_sec: i, source_out_sec: i + 4, target_duration_sec: 3,
    story_role: 'role', transition_hint: 'fade', music_cue: 'piano',
    audio_hint: 'ambient', color_hint: 'warm', evidence: 'dynamic',
    confidence: 0.9, review_status: 'confirmed', order: i,
    shot_id: SHOT_IDS[i],
  }
}

function setup() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const planBody = {
    shots: SHOT_IDS.map((_, i) => baseShot(i)),
    version_id: 'v1', version_no: 1, board_id: 'b1',
  }
  const boardBody = {
    board_id: 'b1', project_id: 'x', current_version_id: 'v1', version_id: 'v1',
    version_no: 1, parent_version_id: null, schema_version: '1.0', created_at: null,
    actor: 'user', reason: 'initial', job_id: null, restore_of_version_id: null,
    brief: {}, sections: [], shots: [], constraints: {},
    validation_summary: { state: 'ok', issues: [] },
  }
  vi.mocked(api).mockImplementation(async (path: string) => {
    if (path === '/board') return boardBody
    if (path === '/versions') return [boardBody]
    if (path === '/ops') {
      return {
        operation_id: 'op-1', project_id: 'x', board_id: 'b1',
        base_version_id: 'v1', result_version_id: null, status: 'proposed',
        schema_version: 1, operations: [], diff: { shots: { added: [], removed: [], reordered: [], trimmed: [], locked: [], replaced: [] }, sections: { added: [], removed: [], reordered: false }, constraints: { changed: false } },
        reason: '', actor: 'user', error_code: '', error_detail: {},
        created_at: null, updated_at: null, requires_confirmation: true,
      }
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

/** Click the draft row for a shot id so the operation target is that shot. */
async function selectShot(shotId: string) {
  const rows = await screen.findAllByTestId(/^draft-shot-/)
  const index = SHOT_IDS.indexOf(shotId)
  fireEvent.click(rows[index])
  await waitFor(() => {
    expect(screen.getByTestId('selected-shot').textContent).toContain(shotId.slice(0, 8))
  })
}

/** The `move_shot` operation the panel would POST, if any was built. */
function pendingMoveOperation(): Record<string, unknown> | null {
  const calls = vi.mocked(api).mock.calls.filter(
    (c) => String(c[0]) === '/ops' && (c[1] as { method?: string })?.method === 'POST',
  )
  if (calls.length === 0) return null
  const body = JSON.parse((calls[calls.length - 1][1] as { body: string }).body)
  return body.operations?.[0] ?? null
}

async function clickPropose() {
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(pendingMoveOperation()).not.toBeNull())
}

test('UAT-F01 up on the middle shot anchors BEFORE the previous shot', async () => {
  setup()
  await selectShot('shot-B')
  fireEvent.click(screen.getByRole('button', { name: '上移' }))
  await clickPropose()

  const op = pendingMoveOperation()!
  expect(op.type).toBe('move_shot')
  expect(op.shot_id).toBe('shot-B')
  // [A,B,C] → up on B must put B before A.
  expect(op.before_shot_id).toBe('shot-A')
  expect(op.after_shot_id).toBeUndefined()
})

test('UAT-F01 down on the middle shot anchors AFTER the next shot', async () => {
  setup()
  await selectShot('shot-B')
  fireEvent.click(screen.getByRole('button', { name: '下移' }))
  await clickPropose()

  const op = pendingMoveOperation()!
  expect(op.shot_id).toBe('shot-B')
  // [A,B,C] → down on B must put B after C.
  expect(op.after_shot_id).toBe('shot-C')
  expect(op.before_shot_id).toBeUndefined()
})

test('UAT-F01 up is disabled for the first shot', async () => {
  setup()
  await selectShot('shot-A')
  const up = screen.getByRole('button', { name: '上移' }) as HTMLButtonElement
  expect(up.disabled).toBe(true)
})

test('UAT-F01 down is disabled for the last shot', async () => {
  setup()
  await selectShot('shot-C')
  const down = screen.getByRole('button', { name: '下移' }) as HTMLButtonElement
  expect(down.disabled).toBe(true)
})

test('UAT-F01 up on the last shot moves it one step up, not a no-op', async () => {
  setup()
  await selectShot('shot-C')
  fireEvent.click(screen.getByRole('button', { name: '上移' }))
  await clickPropose()

  const op = pendingMoveOperation()!
  expect(op.shot_id).toBe('shot-C')
  // [A,B,C] → up on C must put C before B, i.e. [A,C,B].
  expect(op.before_shot_id).toBe('shot-B')
  expect(op.after_shot_id).toBeUndefined()
})

test('UAT-F01 a disabled move never issues a request', async () => {
  setup()
  await selectShot('shot-A')
  const up = screen.getByRole('button', { name: '上移' }) as HTMLButtonElement
  expect(up.disabled).toBe(true)
  fireEvent.click(up)
  // No pending operation should exist, so "预览变更" stays disabled too.
  const propose = screen.getByTestId('operation-propose') as HTMLButtonElement
  expect(propose.disabled).toBe(true)
  expect(pendingMoveOperation()).toBeNull()
})
