import type { ButtonHTMLAttributes, ReactNode } from 'react'

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="inline-flex items-center gap-2 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-600 border-t-accent" />
      {label ?? '加载中…'}
    </span>
  )
}

export function Button({
  variant = 'primary',
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' }) {
  const styles: Record<string, string> = {
    primary:
      'bg-accent text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed',
    ghost:
      'bg-ink-800 text-slate-200 hover:bg-ink-700 border border-ink-700 disabled:opacity-50',
    danger: 'bg-red-600 text-white hover:bg-red-500 disabled:opacity-50',
  }
  return (
    <button
      className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${styles[variant]} ${className}`}
      {...props}
    />
  )
}

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-ink-700 bg-ink-900 p-4 ${className}`}>{children}</div>
}

export function Badge({ children, tone = 'slate' }: { children: ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    slate: 'bg-ink-700 text-slate-300',
    green: 'bg-emerald-900/60 text-emerald-300',
    red: 'bg-red-900/60 text-red-300',
    yellow: 'bg-amber-900/60 text-amber-300',
    blue: 'bg-blue-900/60 text-blue-300',
  }
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${tones[tone] ?? tones.slate}`}>
      {children}
    </span>
  )
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-400">{label}</span>
      {children}
    </label>
  )
}

export const inputClass =
  'w-full rounded-md border border-ink-700 bg-ink-800 px-3 py-2 text-sm text-slate-200 outline-none focus:border-accent'

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      <div className="text-slate-300">{title}</div>
      {hint && <div className="text-sm text-slate-500">{hint}</div>}
    </div>
  )
}
