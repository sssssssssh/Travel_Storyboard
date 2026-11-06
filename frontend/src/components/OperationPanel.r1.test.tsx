// @vitest-environment jsdom
/**
 * TSB-367-R1 R1-05 — the minimal human operation entry points.
 *
 * What this locks down, at the component level so it does not depend on the whole
 * Export page:
 *   * the panel targets what the caller says, and shows that target;
 *   * every listed operation type can be proposed through the panel;
 *   * preview → confirm → applied, with the diff coming from the server;
 *   * reject creates nothing;
 *   * a 409 keeps the pending operations and never overwrites server state;
 *   * a failed apply never renders as success.
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

function proposal(overrides: Record<string, unknown> = {}) {
  return {
    operation_id: 'op-1',
    project_id: 'p',
    board_id: 'b',
    base_version_id: 'v2',
    result_version_id: null,
    status: 'proposed',
    schema_version: 1,
    operations: [],
    diff: {
      shots: {
        added: [],
        removed: [],
        reordered: [],
        trimmed: [{
          shot_id: 'shot-aaa',
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
    ...overrides,
  }
}

function renderPanel(opts: {
  ops?: Record<string, unknown>[]
  targetLabel?: string
} = {}) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={qc}>
      <OperationPanel
        projectId="p"
        boardId="b"
        baseVersionId="v2"
        pendingOperations={opts.ops ?? [
          { type: 'trim_shot', shot_id: 'shot-aaa', source_in_sec: 0.5, source_out_sec: 1.5 },
        ]}
        targetLabel={opts.targetLabel}
      />
    </QueryClientProvider>,
  )
}

const OPERATION_TYPES = [
  'trim_shot', 'remove_shot', 'move_shot', 'lock_shot', 'replace_shot', 'insert_shot',
] as const

test.each(OPERATION_TYPES)(
  'the panel can propose a %s operation and shows the server diff',
  async (type) => {
    vi.mocked(api).mockResolvedValueOnce(proposal())
    renderPanel({
      ops: [{ type, shot_id: 'shot-aaa', target_shot_id: 'shot-aaa' }],
      targetLabel: '镜头 shot-aaa',
    })

    // The target is explicit, never implicit.
    expect(screen.getByTestId('operation-target').textContent).toContain('shot-aaa')

    fireEvent.click(screen.getByTestId('operation-propose'))
    await waitFor(() => expect(screen.getByTestId('operation-diff')).toBeTruthy())
    expect(screen.getByTestId('operation-diff').textContent).toContain('裁剪')
  },
)

test('confirming applies once and reports the new version', async () => {
  vi.mocked(api)
    .mockResolvedValueOnce(proposal())
    .mockResolvedValueOnce(proposal({ status: 'applied', result_version_id: 'v3' }))

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-apply')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-apply'))
  await waitFor(() => expect(screen.getByTestId('operation-applied')).toBeTruthy())

  const applyCalls = vi.mocked(api).mock.calls.filter((c) => String(c[0]).endsWith('/apply'))
  expect(applyCalls).toHaveLength(1)
})

test('rejecting creates no version', async () => {
  vi.mocked(api)
    .mockResolvedValueOnce(proposal())
    .mockResolvedValueOnce(proposal({ status: 'rejected' }))

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-reject')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-reject'))
  await waitFor(() => {
    const rejectCalls = vi.mocked(api).mock.calls.filter((c) => String(c[0]).endsWith('/reject'))
    expect(rejectCalls).toHaveLength(1)
  })
  await waitFor(() => expect(screen.queryByTestId('operation-applied')).toBeNull())
})

test('a 409 keeps the pending operations and does not overwrite server state', async () => {
  const conflict = Object.assign(new Error('版本冲突'), { status: 409 })
  vi.mocked(api).mockRejectedValueOnce(conflict)

  renderPanel({ ops: [{ type: 'remove_shot', shot_id: 'shot-aaa' }] })
  fireEvent.click(screen.getByTestId('operation-propose'))

  await waitFor(() => expect(screen.getByTestId('operation-conflict')).toBeTruthy())
  expect(screen.getByTestId('operation-panel').textContent).toContain('1 项待提交')
})

test('a failed apply is an error, never a success', async () => {
  vi.mocked(api)
    .mockResolvedValueOnce(proposal())
    .mockRejectedValueOnce(Object.assign(new Error('并发冲突'), { status: 409 }))

  renderPanel()
  fireEvent.click(screen.getByTestId('operation-propose'))
  await waitFor(() => expect(screen.getByTestId('operation-apply')).toBeTruthy())

  fireEvent.click(screen.getByTestId('operation-apply'))
  await waitFor(() => expect(screen.getByTestId('operation-error')).toBeTruthy())
  expect(screen.queryByTestId('operation-applied')).toBeNull()
})

test('with no pending operations the propose button is disabled', () => {
  vi.mocked(api).mockResolvedValue(proposal())
  renderPanel({ ops: [] })
  const button = screen.getByTestId('operation-propose') as HTMLButtonElement
  expect(button.disabled).toBe(true)
})
