import type { SegmentListItem, SegmentListOut } from './types'

let token: string | null = null

const STORAGE_KEY = 'tsb.accessToken'

/**
 * Bootstrap the access token.
 *
 * The server no longer exposes an unauthenticated token endpoint (audit
 * P0-01). Instead the launcher opens `/?token=...`; we consume that value once,
 * persist it in sessionStorage, and strip it from the address bar so it does not
 * linger in history or get copied around. A token already in sessionStorage
 * (e.g. after a refresh) is reused.
 */
export function bootstrapToken(): string | null {
  let fromUrl: string | null = null
  try {
    const params = new URLSearchParams(window.location.search)
    fromUrl = params.get('token')
    if (fromUrl) {
      params.delete('token')
      const qs = params.toString()
      const next = `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`
      window.history.replaceState({}, '', next)
      window.sessionStorage.setItem(STORAGE_KEY, fromUrl)
    }
  } catch {
    // URL/history unavailable — fall through to sessionStorage below.
  }

  let stored: string | null = null
  try {
    stored = window.sessionStorage.getItem(STORAGE_KEY)
  } catch {
    // storage unavailable (private mode) — no persisted token to reuse.
  }

  token = fromUrl ?? stored ?? null
  return token
}

export function setToken(value: string | null): void {
  token = value
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, value)
    else window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // storage unavailable (private mode) — keep the in-memory value only
  }
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
    const message = typeof detail === 'string' ? detail
      : detail && typeof detail === 'object' && 'message' in detail ? String(detail.message)
      : JSON.stringify(detail)
    throw new ApiError(res.status, message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** Build a URL with the token as a query param (for <img>/<video>/downloads). */
export function withToken(path: string): string {
  const url = new URL(path, window.location.origin)
  if (token) url.searchParams.set('token', token)
  return `${url.pathname}${url.search}`
}

/** A representative frame image, served by frame id (DB-authorized). */
export function frameUrl(frameId: string | null | undefined): string {
  if (!frameId) return ''
  return withToken(`/api/frames/${encodeURIComponent(frameId)}/image`)
}

/**
 * A stored project artifact (thumbnail path, proxy, contact sheet).
 * `projectId` is required: the media route rejects paths outside the project.
 */
export function mediaUrl(projectId: string, rel: string | null | undefined): string {
  if (!rel || !projectId) return ''
  const url = new URL(`/api/projects/${encodeURIComponent(projectId)}/media`, window.location.origin)
  url.searchParams.set('path', rel)
  if (token) url.searchParams.set('token', token)
  return `${url.pathname}${url.search}`
}

export function reviewProxyUrl(projectId: string, requestId: string): string {
  return withToken(
    `/api/projects/${encodeURIComponent(projectId)}/review-requests/${encodeURIComponent(requestId)}/proxy`,
  )
}

export function roughcutUrl(projectId: string): string {
  return withToken(`/api/projects/${encodeURIComponent(projectId)}/roughcut`)
}

export const endpoints = {
  health: '/api/system/health',
  config: '/api/system/config',
  projects: '/api/projects',
  project: (id: string) => `/api/projects/${id}`,
  assets: (id: string, extra = '') => `/api/projects/${id}/assets${extra}`,
  segments: (id: string, extra = '') => `/api/projects/${id}/segments${extra}`,
  annotate: (id: string) => `/api/segments/${id}/annotate`,
  resetTrim: (id: string) => `/api/segments/${id}/reset-trim`,
  jobs: (id: string) => `/api/projects/${id}/jobs`,
  createJob: (id: string, type: string) => `/api/projects/${id}/jobs/${type}`,
  cancelJob: (id: string) => `/api/jobs/${id}/cancel`,
  job: (id: string) => `/api/jobs/${id}`,
  packages: (id: string) => `/api/projects/${id}/packages`,
  packageDownload: (id: string) => `/api/packages/${id}/download`,
  reviewRequests: (id: string) => `/api/projects/${id}/review-requests`,
  reviewDisposition: (id: string, requestId: string) => `/api/projects/${id}/review-requests/${requestId}/disposition`,
  importReviewRequests: (id: string) => `/api/projects/${id}/review-requests/import`,
  buildProxies: (id: string) => `/api/projects/${id}/review-requests/proxies`,
  analyzeStory: (id: string) => `/api/projects/${id}/story/analyze`,
  // --- M5 vision (TSB-304/305/306) ---
  visionStatus: '/api/vision/status',
  visionPreview: (id: string) => `/api/projects/${id}/vision/preview`,
  visionAnalyze: (id: string) => `/api/projects/${id}/jobs/vision_analyze`,
  visionResults: (id: string) => `/api/projects/${id}/vision-results`,
  segmentVisionResult: (segmentId: string) => `/api/segments/${segmentId}/vision-result`,
  visionCandidates: (id: string) => `/api/projects/${id}/vision-candidates`,
  editPlan: (id: string) => `/api/projects/${id}/edit-plan`,
  exportJson: (id: string) => `/api/projects/${id}/export/json`,
  exportCsv: (id: string) => `/api/projects/${id}/export/csv`,
  exportOtio: (id: string) => `/api/projects/${id}/export/otio`,
}

/** Download URL that appends the token correctly (& not a second ?). */
export function downloadUrl(path: string): string {
  return withToken(path)
}

const PAGE_SIZE = 500

/**
 * Fetch every segment of a project by paging.
 *
 * The export view used to request `limit=500` and silently drop the rest, which
 * a large library (a few dozen long clips) can exceed. Paging keeps the list
 * complete; `max` is a safety valve against an unbounded loop.
 */
export async function fetchAllSegments(
  projectId: string,
  extraQuery = '',
  max = 20000,
): Promise<SegmentListItem[]> {
  const all: SegmentListItem[] = []
  let offset = 0
  for (;;) {
    const suffix = `${extraQuery ? `${extraQuery}&` : '?'}offset=${offset}&limit=${PAGE_SIZE}`
    const page = await api<SegmentListOut>(endpoints.segments(projectId, suffix))
    all.push(...page.items)
    offset += page.items.length
    if (page.items.length === 0 || offset >= page.total || all.length >= max) break
  }
  return all
}
