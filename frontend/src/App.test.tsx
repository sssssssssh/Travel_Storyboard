// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import App from './App'
const { boot } = vi.hoisted(() => ({ boot: vi.fn() }))
vi.mock('./pages/Home', () => ({ default: () => <div>home</div> }))
vi.mock('./lib/store', () => ({
  useApp: (select: (state: { ready: boolean; error: null; boot: () => void }) => unknown) => select({ ready: true, error: null, boot }),
}))
afterEach(cleanup)
test('application shell displays the release version built from package metadata', () => {
  render(<App />)
  expect(screen.getByText('v2.1.2 · 本地运行')).toBeTruthy()
})
