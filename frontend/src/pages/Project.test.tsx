// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import Project from './Project'
import { api } from '../lib/api'
import type { JobOut } from '../lib/types'
vi.mock('../lib/api', async (original) => ({ ...await original<typeof import('../lib/api')>(), api: vi.fn() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })
function setup(active = true, status = 'running', cleanupStats?: JobOut['stats']['cleanup']) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (options?.method === 'POST') return { id: 'new-job', status: 'pending' } as never
    if (path.endsWith('/jobs')) return (active ? [{ id: 'hash', job_type: 'scan', status,
      stats: { cleanup: cleanupStats, done: 0, total: 2, phase: 'hashing', current_file: '旅行.mp4', signature_mode: 'full',
        needs_review: 2, source_changed: 1, bytes_read: 1048576, total_bytes: 8388608, processed_files: 0, total_files: 2 }, resume_token: {}, error: '' }] : []) as never
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
  expect(screen.getByRole('link', { name: '需要人工复核 2 个镜头' })).toBeTruthy()
  expect(screen.getByText(/源文件已变化 1 项/)).toBeTruthy()
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

test('defaults to quick verification and explains the per-task 200GB read cost', async () => {
  setup(false)
  await screen.findByText('test')
  expect((screen.getByLabelText('后台完整校验（每文件完整读取一遍）') as HTMLInputElement).checked).toBe(false)
  expect((screen.getByLabelText('严格复核（处理后再完整读取一遍）') as HTMLInputElement).checked).toBe(false)
  expect(screen.getByText(/200GB.*400GB/)).toBeTruthy()
  fireEvent.click(screen.getByText('扫描建库'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/projects/p/jobs/scan', expect.objectContaining({ method: 'POST' })))
  const request = vi.mocked(api).mock.calls.find((call) => call[1]?.method === 'POST')![1]!
  expect(JSON.parse(request.body as string).options).toEqual({ full_hash: false, strict_full_hash: false })
})


test('cancelled jobs with no cleanup result never claim recovery', async () => {
  setup(true, 'cancelled')
  await screen.findByText('任务已停止；尚无本次清理统计')
  expect(screen.queryByText(/暂存资源已回收/)).toBeNull()
})

test('failed cleanup on a stopped job offers scoped safety retry', async () => {
  setup(true, 'cancelled', { ok: false, retryable: true, error_code: 'CLEANUP_FAILED', residual_count: 2 })
  await screen.findByText(/有暂存残留，需安全重试/)
  fireEvent.click(screen.getByText('安全重试清理'))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/api/projects/p/jobs/hash/cleanup-retry', { method: 'POST' }))
})

test('cancelling jobs cannot offer cleanup retry or claim cleanup completed', async () => {
  setup(true, 'cancelling', { ok: false, retryable: true })
  await screen.findByText(/已收到取消请求/)
  expect(screen.queryByText('安全重试清理')).toBeNull()
  expect(screen.queryByText(/已清理：/)).toBeNull()
})

// TSB-323: a real fast scan completed between polls; the empty list and disabled
// detect button stayed stale because only active jobs invalidated their data.
test.each([false, true])('refreshes project and assets when scan finishes (active poll seen=%s)', async (seenActive) => {
  let completed = false
  vi.mocked(api).mockImplementation(async (path) => {
    if (path.endsWith('/jobs')) return [] as never
    if (path.includes('/assets')) return { items: [], total: completed ? 2 : 0 } as never
    return { id: 'p', name: 'fast scan', stats: { asset_count: completed ? 2 : 0,
      asset_succeeded: completed ? 2 : 0, asset_failed: 0, segment_count: 0,
      total_duration_sec: 3, total_size_bytes: 3 } } as never
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={qc}><MemoryRouter initialEntries={['/p/p']}><Routes><Route path="/p/:id" element={<Project />} /></Routes></MemoryRouter></QueryClientProvider>)
  await screen.findByText('素材 0 段')
  expect((screen.getByRole('button', { name: '检测片段' }) as HTMLButtonElement).disabled).toBe(true)
  if (seenActive) {
    qc.setQueryData(['jobs', 'p'], [{ id: 'scan', job_type: 'scan', status: 'running', stats: {}, resume_token: {} }])
    await screen.findByText('运行中')
  }
  const assetReads = vi.mocked(api).mock.calls.filter(([path]) => path.includes('/assets')).length
  completed = true
  qc.setQueryData(['jobs', 'p'], [{ id: 'scan', job_type: 'scan', status: 'succeeded', stats: { done: 2, total: 2 }, resume_token: {} }])
  await screen.findByText('素材 2 段')
  await waitFor(() => expect((screen.getByRole('button', { name: '检测片段' }) as HTMLButtonElement).disabled).toBe(false))
  expect(vi.mocked(api).mock.calls.filter(([path]) => path.includes('/assets')).length).toBeGreaterThan(assetReads)
  qc.clear()
})
