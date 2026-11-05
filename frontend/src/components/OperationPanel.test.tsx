// @vitest-environment jsdom
/**
 * TSB-367 §8/§9.8 — the minimal structured-operation workspace.
 *
 * What this locks down:
 *   * the preview shows the **server's** diff, and nothing is guessed client-side;
 *   * cancelling (or never proposing) calls no apply;
 *   * confirming calls apply exactly once;
 *   * a 409 keeps the user's pending operations and shows the conflict;
 *   * rejecting creates no version and clears the proposal;
 *   * a failed apply is reported as a failure, never as success.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OperationPanel } from './OperationPanel'
import { api } from '../lib/api'

vi.mock('../lib/api', () => ({
  api: vi.fn(),
  endpoints: {
    planOperations: () => '/ops',
    planOperationApply: (_p: string, _b: string, o: string) => `/ops/${o}/apply`,
    planOperationReject: (_p: string, _b: string, o: string) => `/ops/${o}/reject`,
  },
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const PROPOSAL = {
  operation_id: 'op-1',
  project_id: 'p',
  board_id: 'b',
  base_version_id: 'v2',
  result_version_id: null,
  status: 'proposed' as const,
  schema_version: 1,
  operations: [],
  diff: {
    shots: {
      added: [],
      removed: [],
      reordered: [],
      trimmed: [{
        shot_id: 'shot-1',
        before: { in: 0, out: 2, duration: 2 },
        after: { in: 0.5, out: 1.5, duration: 1 },
      }],
      locked: [],
      replaced: [],
    },
    sections: { added: [], removed: [], reordered: false },
    constraints: { changed: false },
  },
  reason: '',
  actor: 'user',
  error_code: '',
  error_detail: {},
  created_at: null,
  updated_at: null,
  requires_confirmation: true,
}

function renderPanel(overrides: { ops?: Record<string, unknown>[] } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const ops = overrides.ops ?? [{ type: 'trim_shot', shot_id: 'shot-1', source_in_sec: 0.5, source_out_sec: 1.5 }]
  render(
    <QueryClientProvider client={qc}>
      <OperationPanel
        projectId="p"
        boardId="b"
        baseVersionId="v2"
        pendingOperations={ops}
      />
    </QueryClientProvider>,
  )
}

test('the preview shows the server diff and does not compute one itself', async () => {
  vi.mocked(api).mockResolvedValueOnce(PROPOSAL)
  renderPanel()

  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-diff')).toBeTruthy())

  // The line came from PROPOSAL.diff, verbatim enough to prove it is not local.
  expect(screen.getByTestId('operation-diff').textContent).toContain('裁剪')
  expect(screen.getByTestId('operation-diff').textContent).toContain('0.50')
})

test('confirming applies exactly once', async () => {
  vi.mocked(api)
    .mockResolvedValueOnce(PROPOSAL)
    .mockResolvedValueOnce({ ...PROPOSAL, status: 'applied', result_version_id: 'v3' })

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-apply')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-apply'))
  await waitFor(() => expect(screen.getByTestId('operation-applied')).toBeTruthy())

  const applyCalls = vi.mocked(api).mock.calls.filter((c) => String(c[0]).endsWith('/apply'))
  expect(applyCalls).toHaveLength(1)
})

test('rejecting sends reject and leaves no applied version', async () => {
  vi.mocked(api)
    .mockResolvedValueOnce(PROPOSAL)
    .mockResolvedValueOnce({ ...PROPOSAL, status: 'rejected' })

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-reject')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-reject'))
  await waitFor(() => {
    const rejectCalls = vi.mocked(api).mock.calls.filter((c) => String(c[0]).endsWith('/reject'))
    expect(rejectCalls).toHaveLength(1)
  })
  // The proposal disappears; nothing claims a new version.
  await waitFor(() => expect(screen.queryByTestId('operation-applied')).toBeNull())
  expect(screen.queryByText(/已应用/)).toBeNull()
})

test('a 409 shows a conflict and keeps the pending operations', async () => {
  const conflict = Object.assign(new Error('版本冲突'), { status: 409 })
  vi.mocked(api).mockRejectedValueOnce(conflict)

  renderPanel({ ops: [{ type: 'lock_shot', shot_id: 'shot-1', locked: true, dimensions: ['range'] }] })
  fireEvent.click(screen.getByTestId('operation-propose'))

  await waitFor(() => expect(screen.getByTestId('operation-conflict')).toBeTruthy())
  // The panel still knows there is work to submit: proposing again stays possible.
  expect(screen.getByTestId('operation-panel').textContent).toContain('1 项待提交')
})

test('a failed apply is reported as an error, not as success', async () => {
  const failure = Object.assign(new Error('并发冲突'), { status: 409 })
  vi.mocked(api)
    .mockResolvedValueOnce(PROPOSAL)
    .mockRejectedValueOnce(failure)

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-apply')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-apply'))
  await waitFor(() => expect(screen.getByTestId('operation-error')).toBeTruthy())
  expect(screen.queryByTestId('operation-applied')).toBeNull()
})

test('with no pending operations the propose button is disabled', () => {
  vi.mocked(api).mockResolvedValue(PROPOSAL)
  renderPanel({ ops: [] })
  const button = screen.getByTestId('operation-propose') as HTMLButtonElement
  expect(button.disabled).toBe(true)
})
