// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Project from './Project'
import { api } from '../lib/api'
vi.mock('../lib/api', async (original) => ({ ...await original<typeof import('../lib/api')>(), api: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function setup(active = true) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') return { id: 'new-job', status: 'pending' } as never
    if (path.endsWith('/jobs')) return (active ? [{ id: 'hash', job_type: 'scan', status: 'running',
      stats: { done: 0, total: 2, phase: 'hashing', current_file: '旅行.mp4', signature_mode: 'full',
        bytes_read: 1048576, total_bytes: 8388608, processed_files: 0, total_files: 2 }, resume_token: {}, error: '' }] : []) as never
    if (path.includes('/assets')) return { items: [], total: 0 } as never
    return { id: 'p', name: 'test', stats: { asset_count: 2, asset_succeeded: 2, asset_failed: 0, segment_count: 1, total_duration_sec: 3, total_size_bytes: 3 } } as never
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={['/p/p']}><Routes><Route path="/p/:id" element={<Project />} /></Routes></MemoryRouter></QueryClientProvider>)
}
test('shows background hashing file, bytes and signature mode', async () => {
  setup()
  await screen.findByText(/完整 SHA-256/)
  expect(screen.getByText(/旅行.mp4/)).toBeTruthy()
  expect(screen.getByText(/1.0.*8.0/)).toBeTruthy()
})

test('passes force and explicit strict recheck options to the new scan job', async () => {
  setup(false)
  await screen.findByText('test')
  fireEvent.click(screen.getByLabelText('强制重新探测 / 重分析'))
  fireEvent.click(screen.getByLabelText('后台完整校验（每文件完整读取一遍）'))
  fireEvent.click(screen.getByLabelText('严格复核（处理后再完整读取一遍）'))
  fireEvent.click(screen.getByText('扫描建库'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/projects/p/jobs/scan', expect.objectContaining({ method: 'POST' })))
  const request = vi.mocked(api).mock.calls.find((call) => call[1]?.method === 'POST')![1]!
  expect(JSON.parse(request.body as string)).toEqual({ force: true, options: { full_hash: true, strict_full_hash: true } })
})
