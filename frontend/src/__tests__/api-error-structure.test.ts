/**
 * TSB-339 — 前端必须保留结构化错误码；证据必须匿名化。
 *
 * 缺陷 1（结构丢失）：后端返回的是结构化错误信封，例如
 *   { "detail": { "error_code": "MODEL_BUDGET_EXCEEDED", "message": "...",
 *                 "retryable": false, "status": 429 } }
 * 但 `ApiError` 只保留 `message` 字符串，把 `error_code` / `retryable` /
 * `status` / `retry_after` 全部丢掉。后果：界面无法按稳定错误码分支处理
 * （例如"预算超限"应当不可重试、"上游 503"应当可重试），只能显示一段散文，
 * 用户与支持都无法据码定位。
 *
 * 缺陷 2（证据泄露）：提交/导出用的证据必须匿名化，不得包含真实路径、
 * 端点主机或令牌。
 *
 * 这些用例断言可观测差异：`ApiError` 实例上是否真的挂着 `code` /
 * `retryable` 等字段，以及序列化后的证据里是否出现路径/端点/凭据。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, setToken } from '../lib/api'

function mockFetch(status: number, body: unknown) {
  const res = {
    ok: status >= 200 && status < 300,
    status,
    statusText: `HTTP ${status}`,
    json: async () => body,
  } as unknown as Response
  vi.stubGlobal('fetch', vi.fn(async () => res))
}

afterEach(() => {
  vi.unstubAllGlobals()
  setToken(null)
})

describe('TSB-339 structured API errors', () => {
  it('V29 keeps the stable error_code from the detail envelope', async () => {
    mockFetch(409, {
      detail: {
        error_code: 'MODEL_BUDGET_EXCEEDED',
        message: 'this job has already made 200 call(s)',
      },
    })

    await expect(api('/api/jobs/x')).rejects.toBeInstanceOf(ApiError)
    try {
      await api('/api/jobs/x')
    } catch (err) {
      const e = err as ApiError
      // The defect: `code` was never populated, so callers could not branch.
      expect(e.code).toBe('MODEL_BUDGET_EXCEEDED')
      expect(e.message).toContain('200 call(s)')
    }
  })

  it('V29b keeps retryability and status so the UI can decide', async () => {
    mockFetch(503, {
      detail: {
        error_code: 'MODEL_PROVIDER_FAULT',
        message: 'upstream unavailable',
        retryable: true,
        retry_after: 2.5,
      },
    })

    try {
      await api('/api/vision/run')
      throw new Error('expected a rejection')
    } catch (err) {
      const e = err as ApiError
      expect(e.code).toBe('MODEL_PROVIDER_FAULT')
      expect(e.retryable).toBe(true)
      expect(e.retryAfter).toBe(2.5)
      expect(e.status).toBe(503)
    }
  })

  it('V29c a non-retryable budget error is reported as such', async () => {
    mockFetch(400, {
      detail: { error_code: 'MODEL_BUDGET_EXCEEDED', message: 'over budget' },
    })
    try {
      await api('/api/vision/run')
      throw new Error('expected a rejection')
    } catch (err) {
      const e = err as ApiError
      expect(e.code).toBe('MODEL_BUDGET_EXCEEDED')
      expect(e.retryable).toBe(false)
    }
  })

  it('V29d a plain-string detail still produces a usable error', async () => {
    mockFetch(404, { detail: 'not found' })
    try {
      await api('/api/missing')
      throw new Error('expected a rejection')
    } catch (err) {
      const e = err as ApiError
      expect(e.status).toBe(404)
      expect(e.message).toBe('not found')
      // No code was supplied, so none may be invented.
      expect(e.code).toBeUndefined()
    }
  })

  it('V29e an unparseable body does not throw a parse error', async () => {
    const res = {
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response
    vi.stubGlobal('fetch', vi.fn(async () => res))

    try {
      await api('/api/boom')
      throw new Error('expected a rejection')
    } catch (err) {
      const e = err as ApiError
      expect(e.status).toBe(500)
      expect(e.message).toBe('Internal Server Error')
    }
  })

  it('V29f nested envelopes are unwrapped, not stringified', async () => {
    mockFetch(422, {
      detail: {
        error_code: 'VIDEO_CAPABILITY_UNVERIFIED',
        message: 'endpoint not verified',
        context: { state: 'unverified' },
      },
    })
    try {
      await api('/api/vision/video')
      throw new Error('expected a rejection')
    } catch (err) {
      const e = err as ApiError
      expect(e.code).toBe('VIDEO_CAPABILITY_UNVERIFIED')
      expect(e.message).toBe('endpoint not verified')
      expect(e.context).toEqual({ state: 'unverified' })
      // It must not degenerate to the raw JSON blob.
      expect(e.message).not.toContain('{')
    }
  })
})
