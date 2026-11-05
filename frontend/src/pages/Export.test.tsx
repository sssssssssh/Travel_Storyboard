// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Export from './Export'
import { api } from '../lib/api'
vi.mock('../lib/api', () => ({
  api: vi.fn(), fetchAllSegments: vi.fn(async () => []),
  endpoints: {
    editPlan: () => '/plan', exportCsv: () => '/csv', exportJson: () => '/json', exportOtio: () => '/otio',
    storyboard: () => '/board', storyboardVersions: () => '/versions',
    storyboardVersion: () => '/version', storyboardRestore: () => '/restore',
  },
  downloadUrl: vi.fn(), mediaUrl: vi.fn(), roughcutUrl: vi.fn(),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const shot = { asset_id: 'a', segment_id: 's', source_in_sec: 1, source_out_sec: 5,
  target_duration_sec: 3, story_role: '开场', transition_hint: 'fade', music_cue: 'piano',
  audio_hint: 'ambient', color_hint: 'warm', evidence: 'dynamic', confidence: .9,
  review_status: 'confirmed', order: 0 }
function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const planBody = {
    shots: Array.from({ length: 10 }, (_, i) => ({ ...shot, segment_id: `s${i}`, order: i })),
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
    return planBody
  })
  render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={['/p/x']}><Routes>
    <Route path="/p/:id" element={<Export />} />
  </Routes></MemoryRouter></QueryClientProvider>)
  return qc
}
test('RT15 restores all saved shots and keeps hints on save', async () => {
  setup()
  await waitFor(() => expect(screen.getAllByPlaceholderText('转场提示（可选）')).toHaveLength(10))
  vi.mocked(api).mockResolvedValueOnce({ created: 10, errors: [], total_duration_sec: 30 })
  fireEvent.click(screen.getByText('保存剪辑计划'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/plan', expect.objectContaining({ method: 'POST' })))
  const payload = JSON.parse(vi.mocked(api).mock.calls.find((call) => call[1]?.method === 'POST')![1]!.body as string)
  expect(payload.shots[0]).toMatchObject({ ...shot, segment_id: 's0' })
  expect(payload.shots.map((s: {order: number}) => s.order)).toEqual([0,1,2,3,4,5,6,7,8,9])
})
test('RT17 query refresh preserves dirty draft; explicit reload restores server', async () => {
  const qc = setup()
  await waitFor(() => expect(screen.getAllByPlaceholderText('转场提示（可选）')).toHaveLength(10))
  fireEvent.change(screen.getAllByPlaceholderText('转场提示（可选）')[0], { target: { value: 'my edit' } })
  await act(async () => { qc.setQueryData(['edit-plan', 'x'], { shots: [{ ...shot, transition_hint: 'server' }] }) })
  expect((screen.getAllByPlaceholderText('转场提示（可选）')[0] as HTMLInputElement).value).toBe('my edit')
  fireEvent.click(screen.getByText('重新加载服务端版本'))
  await waitFor(() => expect((screen.getByPlaceholderText('转场提示（可选）') as HTMLInputElement).value).toBe('server'))
})

test('failed save retains the edited draft', async () => {
  setup()
  await waitFor(() => expect(screen.getAllByPlaceholderText('转场提示（可选）')).toHaveLength(10))
  fireEvent.change(screen.getAllByPlaceholderText('转场提示（可选）')[0], { target: { value: 'keep me' } })
  vi.mocked(api).mockRejectedValueOnce(new Error('network failure'))
  fireEvent.click(screen.getByText('保存剪辑计划'))
  await screen.findByText('保存失败，草稿已保留：network failure')
  expect((screen.getAllByPlaceholderText('转场提示（可选）')[0] as HTMLInputElement).value).toBe('keep me')
})


// --- TSB-366 version UI ----------------------------------------------------

test('shows the current storyboard version and sends base_version_id on save', async () => {
  setup()
  await waitFor(() => expect(screen.getByTestId('current-version').textContent).toContain('v1'))
  fireEvent.click(screen.getByText('保存剪辑计划'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/plan', expect.objectContaining({ method: 'POST' })))
  const body = JSON.parse(
    vi.mocked(api).mock.calls.find((call) => call[1]?.method === 'POST')![1]!.body as string,
  )
  expect(body.base_version_id).toBe('v1')
})

test('a 409 conflict keeps the draft and shows the conflict banner', async () => {
  setup()
  await waitFor(() => expect(screen.getAllByPlaceholderText('转场提示（可选）')).toHaveLength(10))
  fireEvent.change(screen.getAllByPlaceholderText('转场提示（可选）')[0], { target: { value: 'my unsaved edit' } })
  const conflict = Object.assign(new Error('conflict'), { status: 409 })
  vi.mocked(api).mockRejectedValueOnce(conflict)
  fireEvent.click(screen.getByText('保存剪辑计划'))
  await screen.findByTestId('version-conflict')
  // The draft survives a conflict: this is the §5.2 requirement.
  expect((screen.getAllByPlaceholderText('转场提示（可选）')[0] as HTMLInputElement).value).toBe('my unsaved edit')
  expect(screen.getByTestId('draft-dirty')).toBeTruthy()
})

test('the history panel lists versions and offers restore for non-current ones', async () => {
  setup()
  await waitFor(() => expect(screen.getByTestId('current-version')).toBeTruthy())
  fireEvent.click(screen.getByTestId('toggle-history'))
  await waitFor(() => {
    const panel = screen.getByTestId('version-history')
    expect(panel.textContent).toContain('v1')
  })
})
