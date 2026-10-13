export function formatDuration(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return '0:00'
  const s = Math.round(sec)
  const m = Math.floor(s / 60)
  const r = s % 60
  if (m >= 60) {
    const h = Math.floor(m / 60)
    const mm = m % 60
    return `${h}:${String(mm).padStart(2, '0')}:${String(r).padStart(2, '0')}`
  }
  return `${m}:${String(r).padStart(2, '0')}`
}

export function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatTimeCode(sec: number): string {
  const s = Math.max(0, sec)
  const mm = Math.floor(s / 60)
  const ss = (s % 60).toFixed(1).padStart(4, '0')
  return `${mm}:${ss}`
}
