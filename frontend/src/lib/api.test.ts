// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { api, setToken, withToken, roughcutUrl } from './api'
afterEach(() => { vi.unstubAllGlobals(); setToken(null) })
test('TSB-202 media URLs keep one query delimiter and correctly encode token', () => {
  setToken('a?b&c')
  const path = withToken('/api/test?existing=yes')
  expect(path.split('?')).toHaveLength(2)
  expect(new URL(path, window.location.origin).searchParams.get('token')).toBe('a?b&c')
  expect(roughcutUrl('p').split('?')).toHaveLength(2)
})
test('TSB-204 structured workspace errors show their human message', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 409, json: async () => ({
    detail: { code: 'WORKSPACE_HAS_DATA', message: '需要备份和迁移' },
  }) })))
  await expect(api('/api/projects/p')).rejects.toThrow('需要备份和迁移')
})
