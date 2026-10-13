// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Review from './Review'
import { api } from '../lib/api'
vi.mock('../lib/api', async (original) => ({ ...await original<typeof import('../lib/api')>(), api: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
test('loads persisted note and warns about a retained stale shot without overwriting note edits', async () => {
  const segment = { id: 's', asset_id: 'a', asset_display_name: 'clip.mp4', note: 'stored note', status: 'needs_review',
    start_sec: 0, end_sec: 10, detected_start_sec: 0, detected_end_sec: 10, is_trimmed: false,
    duration_sec: 10, user_rating: 'favorite', story_role: '高潮', boundary_reason: 'hard_cut',
    thumbnail: null, quality_metrics: { analysis_warning: '旧镜头已保留' }, content_tags: {} }
  vi.mocked(api).mockResolvedValue({ items: [segment], total: 1, offset: 0, limit: 60 })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={['/p/p']}><Routes><Route path="/p/:id" element={<Review />} /></Routes></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: /clip.mp4/ }))
  const input = screen.getByPlaceholderText('这段镜头的故事价值、用途…') as HTMLTextAreaElement
  expect(input.value).toBe('stored note')
  expect(screen.getByText(/需要复核：旧镜头已保留/)).toBeTruthy()
  fireEvent.change(input, { target: { value: 'unsaved latest note' } })
  await act(async () => qc.setQueryData(['segments', 'p', '', '', 0], { items: [{ ...segment, note: 'server update' }], total: 1 }))
  expect(input.value).toBe('unsaved latest note')
})
