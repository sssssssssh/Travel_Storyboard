// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Export from './Export'
import { api } from '../lib/api'
vi.mock('../lib/api', () => ({
  api: vi.fn(), fetchAllSegments: vi.fn(async () => []),
  endpoints: { editPlan: () => '/plan', exportCsv: () => '/csv', exportJson: () => '/json', exportOtio: () => '/otio' },
  downloadUrl: vi.fn(), mediaUrl: vi.fn(), roughcutUrl: vi.fn(),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const shot = { asset_id: 'a', segment_id: 's', source_in_sec: 1, source_out_sec: 5,
  target_duration_sec: 3, story_role: '开场', transition_hint: 'fade', music_cue: 'piano',
  audio_hint: 'ambient', color_hint: 'warm', evidence: 'dynamic', confidence: .9,
  review_status: 'confirmed', order: 0 }
function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api).mockResolvedValue({ shots: Array.from({ length: 10 }, (_, i) => ({ ...shot, segment_id: `s${i}`, order: i })) })
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
