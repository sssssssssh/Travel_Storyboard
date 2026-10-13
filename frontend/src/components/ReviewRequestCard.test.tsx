// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { api } from '../lib/api'
import type { ReviewRequestOut } from '../lib/types'
import ReviewRequestCard from './ReviewRequestCard'
vi.mock('../lib/api', async (original) => ({ ...await original<typeof import('../lib/api')>(), api: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
const current = { id: 'new-segment', asset_id: 'a', display_name: '旅行.mp4', start_sec: 2, end_sec: 6,
  detected_start_sec: 0, detected_end_sec: 6, user_rating: 'pending', story_role: '', note: '',
  status: 'candidate', change_reason: '', thumbnail: null }
const old: ReviewRequestOut = { id: 'old-request', project_id: 'p', asset_id: 'a', segment_id: 'old-segment',
  start_sec: 3, end_sec: 3.6, priority: 'high', reason: 'check motion', source: 'ai', status: 'needs_review',
  proxy_ref: 'historic.mp4', created_at: '2026-10-14', plan_shot_ids: ['shot-1'],
  validation_error: { error_code: 'SEGMENT_NEEDS_REVIEW', message: '需要人工选择当前镜头' },
  segment_context: { ...current, id: 'old-segment', user_rating: 'favorite', note: '我的备注',
    story_role: '高潮', status: 'needs_review', change_reason: '检测边界发生变化' }, current_segments: [current] }
function setup(request = old) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = render(<QueryClientProvider client={qc}><MemoryRouter><ReviewRequestCard projectId="p" request={request} /></MemoryRouter></QueryClientProvider>)
  return { ...view, update: (request: ReviewRequestOut) => view.rerender(<QueryClientProvider client={qc}><MemoryRouter><ReviewRequestCard projectId="p" request={request} /></MemoryRouter></QueryClientProvider>) }
}
test('shows historic range, annotation, preview origin and edit-plan blocker; never auto selects', () => {
  setup()
  expect(screen.getByText(/我的备注/)).toBeTruthy()
  expect(screen.getByText(/变化原因/)).toBeTruthy()
  expect(screen.getByText(/预览 0 秒对应原片 3s/)).toBeTruthy()
  expect(screen.getByRole('link', { name: '人工更新剪辑计划' }).getAttribute('href')).toBe('/project/p/export')
  expect((screen.getByLabelText('选择当前有效镜头') as HTMLSelectElement).value).toBe('')
  expect((screen.getByRole('button', { name: '创建新请求并替代旧请求' }) as HTMLButtonElement).disabled).toBe(true)
  expect(api).not.toHaveBeenCalled()
})
test('explicit selection creates a new request, preserves edited range during query refresh', async () => {
  vi.mocked(api).mockResolvedValue({ ...old, id: 'new-request', status: 'pending', supersedes_request_id: old.id })
  const view = setup()
  fireEvent.change(screen.getByLabelText('选择当前有效镜头'), { target: { value: 'new-segment' } })
  expect(screen.getByText(/最新检测区间 0–6s/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('新请求原片入点（秒）'), { target: { value: '3' } })
  fireEvent.change(screen.getByLabelText('新请求原片出点（秒）'), { target: { value: '3.6' } })
  await act(async () => view.update({ ...old, current_segments: [{ ...current, end_sec: 5 }] }))
  expect((screen.getByLabelText('新请求原片出点（秒）') as HTMLInputElement).value).toBe('3.6')
  fireEvent.click(screen.getByRole('button', { name: '创建新请求并替代旧请求' }))
  await screen.findByRole('status')
  const call = vi.mocked(api).mock.calls[0]
  expect(call[0]).toBe('/api/projects/p/review-requests/old-request/disposition')
  expect(JSON.parse(call[1]!.body as string)).toEqual({ action: 'replace', asset_id: 'a', segment_id: 'new-segment', start_sec: 3, end_sec: 3.6 })
})
test.each([['暂不处理', 'defer'], ['放弃旧请求', 'dismiss']])('explicit %s preserves old preview and annotations', async (label, action) => {
  vi.mocked(api).mockResolvedValue({ ...old, status: action === 'defer' ? 'needs_review' : 'dismissed' })
  setup()
  fireEvent.click(screen.getByRole('button', { name: label }))
  await waitFor(() => expect(api).toHaveBeenCalled())
  expect(JSON.parse(vi.mocked(api).mock.calls[0][1]!.body as string)).toEqual({ action })
  expect(screen.getByText(/我的备注/)).toBeTruthy()
  expect(screen.getByRole('link', { name: /历史对照/ })).toBeTruthy()
})
test('concurrent rejection stays actionable; closed history has no mutation buttons', async () => {
  vi.mocked(api).mockRejectedValue(new Error('请求超出最新镜头裁剪'))
  const view = setup()
  fireEvent.change(screen.getByLabelText('选择当前有效镜头'), { target: { value: 'new-segment' } })
  fireEvent.click(screen.getByRole('button', { name: '创建新请求并替代旧请求' }))
  expect((await screen.findByRole('alert')).textContent).toContain('最新镜头裁剪')
  view.update({ ...old, status: 'superseded', superseded_by_request_id: 'replacement' })
  expect(screen.queryByRole('button', { name: '创建新请求并替代旧请求' })).toBeNull()
  expect(screen.getByText(/已替代为请求 replacement/)).toBeTruthy()
})
