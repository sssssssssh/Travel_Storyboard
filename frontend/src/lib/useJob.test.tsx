// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, test, vi } from 'vitest'
import type { ReactNode } from 'react'
import { api } from './api'
import { useJob } from './useJob'
vi.mock('./api', () => ({ api: vi.fn(), endpoints: { job: (id: string) => `/api/jobs/${id}` } }))
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })

test('cancelling keeps polling until cleanup and work have actually stopped', async () => {
  vi.useFakeTimers()
  vi.mocked(api).mockResolvedValueOnce({ status: 'running' })
    .mockResolvedValueOnce({ status: 'cancelling' }).mockResolvedValue({ status: 'cancelled' })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const result = renderHook(() => useJob('test-job'), { wrapper })
  await act(async () => { await vi.advanceTimersByTimeAsync(10) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1600) })
  await act(async () => { await vi.advanceTimersByTimeAsync(1600) })
  expect(result.result.current.data?.status).toBe('cancelled')
  const count = vi.mocked(api).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
  expect(vi.mocked(api).mock.calls).toHaveLength(count)
  client.clear()
})
