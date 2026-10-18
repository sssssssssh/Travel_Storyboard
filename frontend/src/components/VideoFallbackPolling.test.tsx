// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import VideoUnderstandingPanel from './VideoUnderstandingPanel'
import { api } from '../lib/api'
vi.mock('../lib/api', async (original) => ({ ...await original<typeof import('../lib/api')>(), api: vi.fn() }))
const clients: QueryClient[] = []
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.useRealTimers(); vi.resetAllMocks() })
const tick = async (ms = 20) => {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
  // React Query batches notifications; flush effects and their follow-up reads.
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
  await act(async () => { await vi.advanceTimersByTimeAsync(20) })
}
function harness(options: { noFallback?: boolean; fast?: boolean } = {}) {
  vi.useFakeTimers()
  const state = { status: 'pending', missing: false, networkError: false, started: 0, scheduleFailed: false }
  const paths: string[] = []
  vi.mocked(api).mockImplementation(async (url, init) => {
    const path = url.split('?')[0]; paths.push(path)
    if (init?.method === 'POST') { state.started++; return { id: `parent-${state.started}`, status: options.fast ? 'succeeded' : 'pending' } as never }
    if (path.startsWith('/api/jobs/')) {
      if (state.networkError) throw new Error('temporary local service error')
      return { id: path.split('/').pop(), project_id: 'p', status: state.scheduleFailed ? 'partial_failed' : 'succeeded', stats: state.scheduleFailed ? {fallback_schedule_error:'private URL Key payload must not be rendered'} : {},
        fallback: options.noFallback ? { state: 'none' } : { state: state.scheduleFailed ? 'failed' : state.missing ? 'missing' : 'resolved', job_id: state.scheduleFailed ? '' : 'child-1', job_type: 'motion_review', status: state.status,
          analysed: state.status === 'succeeded', result_count: ['succeeded', 'partial_failed'].includes(state.status) ? 1 : 0,
          error_code: state.scheduleFailed ? 'FALLBACK_SCHEDULE_FAILED' : state.status === 'failed' ? 'MODEL_RESPONSE_INVALID' : '' } } as never
    }
    if (path.endsWith('/video-observations')) return [] as never
    if (path.endsWith('/video-coverage')) return { library_segments: 2, segments_with_video_reading: 0, segments_not_covered: 2, failed_attempts: 0, event_candidates: 0, complete: false } as never
    if (path.endsWith('/vision-results')) return { items: [] } as never
    if (path.endsWith('/motion-reviews')) return (path.includes('/p/') && ['succeeded', 'partial_failed'].includes(state.status) ? [{ id: 'motion-1', segment_id: 'selected-shot-1', motion_type: 'pan', action_summary: 'Mock derived frames show a pan', method: 'multiframe' }] : []) as never
    throw new Error(`Unexpected test API route ${path}`)
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } } }); clients.push(client)
  const ui = (projectId: string) => <QueryClientProvider client={client}><MemoryRouter><VideoUnderstandingPanel projectId={projectId} /></MemoryRouter></QueryClientProvider>
  const rendered = render(ui('p'))
  const start = async () => { await tick(); const permission=screen.getByRole('switch',{name:'允许本次回退到多帧分析'}); if(permission.getAttribute('aria-checked')==='false') fireEvent.click(permission); fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button', { name: '开始视频分析' })); await tick() }
  const gets = () => paths.filter(p => p.startsWith('/api/jobs/')).length
  return { state, paths, start, gets, rendered, switchProject: () => rendered.rerender(ui('q')) }
}

test('F01 parent succeeded with pending child keeps fetching parent', async () => {
  const h = harness(); await h.start(); expect(screen.getByTestId('fallback-status').textContent).toContain('已排队'); const n = h.gets(); await tick(1600); expect(h.gets()).toBeGreaterThan(n)
})
test('F02 child pending to running is live and never claims stored results', async () => {
  const h = harness(); await h.start(); h.state.status = 'running'; await tick(1600); expect(screen.getByTestId('fallback-status').textContent).toBe('正在分析'); expect(screen.getByTestId('fallback-analysed').textContent).not.toContain('已入库')
})
test('F03 child succeeded updates then stops the parent polling loop', async () => {
  const h = harness(); await h.start(); h.state.status = 'succeeded'; await tick(1600); expect(screen.getByTestId('fallback-status').textContent).toBe('已完成'); const n = h.gets(); await tick(5000); expect(h.gets()).toBe(n)
})
test('F04 completion invalidates motion cache and renders the selected actual reading', async () => {
  const h = harness(); await h.start(); const n = h.paths.filter(p => p.endsWith('/motion-reviews')).length; h.state.status = 'succeeded'; await tick(1600); expect(h.paths.filter(p => p.endsWith('/motion-reviews')).length).toBeGreaterThan(n); expect(screen.getByText(/Mock derived frames show a pan/)).toBeTruthy(); expect(screen.getByText('selected')).toBeTruthy()
})
test('F05 partial_failed shows only available results and stops polling', async () => {
  const h = harness(); await h.start(); h.state.status = 'partial_failed'; await tick(1600); expect(screen.getByTestId('fallback-status').textContent).toBe('部分失败'); expect(screen.getByTestId('fallback-analysed').textContent).toContain('1'); expect(screen.getByTestId('fallback-analysed').textContent).not.toContain('全部'); expect(screen.getByText(/Mock derived frames/)).toBeTruthy(); const n=h.gets(); await tick(4000); expect(h.gets()).toBe(n)
})
test.each(['failed', 'cancelled', 'missing'])('F06 child %s remains diagnosable and stops polling', async status => {
  const h=harness(); await h.start(); h.state.status=status; h.state.missing=status==='missing'; await tick(1600); expect(screen.getByText(/child-1/)).toBeTruthy(); expect(screen.queryByText('多帧分析结果已入库。')).toBeNull(); if(status==='failed') expect(screen.getByText(/MODEL_RESPONSE_INVALID/)).toBeTruthy(); const n=h.gets(); await tick(4000); expect(h.gets()).toBe(n)
})
test('F07 ordinary completed job does not add polling requests', async () => {
  const h=harness({ noFallback:true }); await h.start(); const n=h.gets(); await tick(5000); expect(h.gets()).toBe(n); expect(screen.queryByTestId('fallback-tracking')).toBeNull()
})
test('F08 first GET already has a terminal parent and pending successor', async () => {
  const h=harness({ fast:true }); await h.start(); expect(h.gets()).toBe(1); h.state.status='succeeded'; await tick(1600); expect(h.gets()).toBe(2); expect(screen.getByTestId('fallback-analysed').textContent).toContain('已入库')
})
test('F09 project switch drops the old job and old results, including its polling', async () => {
  const h=harness(); await h.start(); h.state.status='succeeded'; await tick(1600); expect(screen.getByText(/Mock derived frames/)).toBeTruthy(); const n=h.gets(); h.switchProject(); await tick(5000); expect(h.gets()).toBe(n); expect(screen.queryByTestId('fallback-tracking')).toBeNull(); expect(screen.queryByText(/Mock derived frames/)).toBeNull(); expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
})
test('F09 unmount stops old requests', async () => {
  const h=harness(); await h.start(); h.rendered.unmount(); const n=h.gets(); await tick(5000); expect(h.gets()).toBe(n)
})
test('F10 a fallback adds multi-frame data without inventing native observations', async () => {
  const h=harness(); await h.start(); h.state.status='succeeded'; await tick(1600); expect(screen.getByText(/Mock derived frames/)).toBeTruthy(); expect(screen.queryByTestId('video-observation')).toBeNull(); expect(screen.getByText(/0 个镜头有视频读数/)).toBeTruthy(); expect(screen.getByText('无视频读数')).toBeTruthy()
})
test('network errors preserve the last child status and are visible, then recover', async () => {
  const h=harness(); await h.start(); h.state.networkError=true; await tick(1600); expect(screen.getByRole('alert').textContent).toContain('JOB_STATUS_UNAVAILABLE'); expect(screen.getByTestId('fallback-status').textContent).toContain('已排队'); h.state.networkError=false; h.state.status='succeeded'; await tick(1600); expect(screen.getByTestId('fallback-status').textContent).toBe('已完成')
})
test('a stalled child can pause UI polling without cancelling background work', async () => {
  const h=harness(); await h.start(); fireEvent.click(screen.getByRole('button',{name:'暂停状态刷新'})); const n=h.gets(); await tick(10000); expect(h.gets()).toBe(n); expect(h.state.started).toBe(1); fireEvent.click(screen.getByRole('button',{name:'继续状态刷新'})); await tick(1600); expect(h.gets()).toBeGreaterThan(n)
})

test('fallback permission is separate, defaults off, and is sent only after an explicit choice', async () => {
  const h=harness(); await tick(); const permission=screen.getByRole('switch',{name:'允许本次回退到多帧分析'}); expect(permission.getAttribute('aria-checked')).toBe('false')
  fireEvent.click(permission); await h.start(); const post=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!; expect(JSON.parse(post[1]!.body as string)).toMatchObject({allow_fallback:true,upload_acknowledged:true})
})


test('fallback remains denied in the request unless the separate switch was selected', async () => {
  harness(); await tick(); fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(screen.getByRole('button',{name:'开始视频分析'})); await tick()
  const post=vi.mocked(api).mock.calls.find(([,options])=>options?.method==='POST')!; expect(JSON.parse(post[1]!.body as string).allow_fallback).toBe(false)
})


test('S06 a failed schedule shows only its stable code, with no blank successor or private error', async () => {
  const h=harness(); h.state.scheduleFailed=true; await h.start(); expect(screen.getByTestId('fallback-schedule-error').textContent).toContain('FALLBACK_SCHEDULE_FAILED'); expect(screen.queryByTestId('fallback-tracking')).toBeNull(); expect(screen.queryByText(/private URL Key/)).toBeNull(); const n=h.gets(); await tick(4000); expect(h.gets()).toBe(n)
})

test('a newly accepted job replaces old polling and resets the UI pause', async () => {
  const h=harness(); await h.start(); fireEvent.click(screen.getByRole('button',{name:'暂停状态刷新'})); h.state.status='pending'; fireEvent.click(screen.getByRole('button',{name:'开始视频分析'})); await tick(); const old=h.paths.filter(p=>p==='/api/jobs/parent-1').length; await tick(3200); expect(h.paths.filter(p=>p==='/api/jobs/parent-1').length).toBe(old); expect(h.paths.filter(p=>p==='/api/jobs/parent-2').length).toBeGreaterThan(1); expect(screen.getByRole('button',{name:'暂停状态刷新'})).toBeTruthy()
})
