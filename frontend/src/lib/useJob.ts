import { useQuery } from '@tanstack/react-query'
import { api, endpoints } from './api'
import type { JobOut } from './types'

/** Poll a single job while it is pending/running. */
export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api<JobOut>(endpoints.job(jobId!)),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status === 'pending' || status === 'running' ? 1500 : false
    },
  })
}
