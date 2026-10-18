import { useQuery } from '@tanstack/react-query'
import { api, endpoints } from './api'
import type { JobOut } from './types'

const ACTIVE = new Set(['pending', 'running', 'cancelling'])

/** The parent endpoint resolves its successor live; one polling loop is enough. */
export function shouldPollJob(data?: JobOut): boolean {
  if (!data || ACTIVE.has(data.status)) return true
  return data.fallback?.state === 'resolved' && ACTIVE.has(data.fallback.status)
}

/** Pausing affects UI refresh only, never the background job. */
export function useJob(jobId: string | null, polling = true) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: ({ signal }) => api<JobOut>(endpoints.job(jobId!), { signal }),
    enabled: !!jobId,
    refetchInterval: (query) => polling && shouldPollJob(query.state.data) ? 1500 : false,
  })
}
