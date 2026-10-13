import { create } from 'zustand'
import { bootstrapToken, getToken } from './api'

interface AppState {
  token: string | null
  ready: boolean
  error: string | null
  boot: () => Promise<void>
}

/**
 * Bootstrap state. The token is no longer fetched from an unauthenticated API
 * (audit P0-01): it comes from the launcher URL (`?token=…`) or sessionStorage
 * via `bootstrapToken()`. `ready` simply reports whether we have one, so pages
 * can show a "reopen via the launcher link" hint instead of a 401 storm.
 */
export const useApp = create<AppState>((set) => ({
  token: null,
  ready: false,
  error: null,
  boot: async () => {
    const token = bootstrapToken() ?? getToken()
    set({
      token,
      ready: true,
      error: token
        ? null
        : '未加载访问令牌 —— 请通过启动器输出/ token.txt 中的 ?token=… 链接打开本页面',
    })
  },
}))
