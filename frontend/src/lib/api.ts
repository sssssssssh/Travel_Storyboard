let token: string | null = null

export function setToken(value: string | null): void {
  token = value
}

export function getToken(): string | null {
  return token
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (token) headers.set('X-Access-Token', token)
  if (options.body && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(path, { ...options, headers })
  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      // ignore json parse errors
    }
    const message = typeof detail === 'string' ? detail : JSON.stringify(detail)
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export function mediaUrl(rel: string | null | undefined): string {
  if (!rel) return ''
  return `/api/media/file?path=${encodeURIComponent(rel)}&token=${encodeURIComponent(token ?? '')}`
}

export const endpoints = {
  token: '/api/system/token',
  health: '/api/system/health',
  config: '/api/system/config',
  projects: '/api/projects',
  project: (id: string) => `/api/projects/${id}`,
  assets: (id: string, extra = '') => `/api/projects/${id}/assets${extra}`,
  segments: (id: string, extra = '') => `/api/projects/${id}/segments${extra}`,
  annotate: (id: string) => `/api/segments/${id}/annotate`,
  jobs: (id: string) => `/api/projects/${id}/jobs`,
  createJob: (id: string, type: string) => `/api/projects/${id}/jobs/${type}`,
  job: (id: string) => `/api/jobs/${id}`,
  packages: (id: string) => `/api/projects/${id}/packages`,
  packageDownload: (id: string) => `/api/packages/${id}/download`,
  reviewRequests: (id: string) => `/api/projects/${id}/review-requests`,
  importReviewRequests: (id: string) => `/api/projects/${id}/review-requests/import`,
  buildProxies: (id: string) => `/api/projects/${id}/review-requests/proxies`,
  analyzeStory: (id: string) => `/api/projects/${id}/story/analyze`,
  editPlan: (id: string) => `/api/projects/${id}/edit-plan`,
  exportJson: (id: string) => `/api/projects/${id}/export/json`,
  exportCsv: (id: string) => `/api/projects/${id}/export/csv`,
  exportOtio: (id: string) => `/api/projects/${id}/export/otio`,
}

export function downloadUrl(path: string): string {
  return `${path}?token=${encodeURIComponent(token ?? '')}`
}
