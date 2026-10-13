import { create } from 'zustand'
import { api, setToken } from './api'
import { endpoints } from './api'

interface AppState {
  token: string | null
  ready: boolean
  error: string | null
  boot: () => Promise<void>
}

export const useApp = create<AppState>((set) => ({
  token: null,
  ready: false,
  error: null,
  boot: async () => {
    try {
      const data = await api<{ token: string }>(endpoints.token)
      setToken(data.token)
      set({ token: data.token, ready: true, error: null })
    } catch (err) {
      set({ ready: true, error: err instanceof Error ? err.message : String(err) })
    }
  },
}))
