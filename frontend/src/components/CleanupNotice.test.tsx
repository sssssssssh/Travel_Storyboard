// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test } from 'vitest'
import CleanupNotice from './CleanupNotice'
afterEach(cleanup)
test('missing cleanup never claims recovery', () => {
  render(<CleanupNotice />)
  expect(screen.getByText('任务已停止；尚无本次清理统计')).toBeTruthy()
  expect(screen.queryByText(/已回收|已清理/)).toBeNull()
})
test('successful cleanup displays actual files and bytes', () => {
  render(<CleanupNotice cleanup={{ ok: true, removed_files: 3, removed_bytes: 42 }} />)
  expect(screen.getByText('已清理：3 个文件，42 字节')).toBeTruthy()
})
test('failed cleanup displays residuals and only a bounded path summary', () => {
  render(<CleanupNotice cleanup={{ ok: false, error_code: 'CLEANUP_FAILED', residual_count: 1, residual_bytes: 9, residual_paths: ['/Users/alice/private/proxies/staging/token'] }} />)
  expect(screen.getByText(/有暂存残留，需安全重试.*9 字节.*CLEANUP_FAILED/)).toBeTruthy()
  expect(screen.queryByText(/Users|alice|private/)).toBeNull()
})

test('unknown residual byte count never claims zero bytes', () => {
  render(<CleanupNotice cleanup={{ ok: false, residual_count: 1, residual_bytes: null }} />)
  expect(screen.getByText(/残留大小待核对/)).toBeTruthy()
  expect(screen.queryByText(/0 字节/)).toBeNull()
})
