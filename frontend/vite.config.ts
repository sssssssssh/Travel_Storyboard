import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { readFileSync } from 'node:fs'

const releaseVersion = readFileSync(new URL('../VERSION', import.meta.url), 'utf8').trim()
const packageVersion = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')).version
if (packageVersion !== releaseVersion) throw new Error('Release version mismatch: run scripts/sync_version.py')

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8737',
        changeOrigin: true,
      },
    },
  },
})
