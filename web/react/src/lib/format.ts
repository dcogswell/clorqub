function toNum(v: unknown): number | null {
  if (v == null || v === '' || typeof v === 'boolean') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function usd(v: unknown, digits = 3): string {
  const n = toNum(v)
  return n == null ? '—' : `$${n.toFixed(digits)}`
}

export function num(v: unknown, digits = 0): string {
  const n = toNum(v)
  return n == null ? '—' : n.toFixed(digits)
}

export function int(v: unknown): string {
  const n = toNum(v)
  return n == null ? '—' : Math.round(n).toLocaleString()
}

export function dt(ts: string | null | undefined): string {
  if (!ts) return '—'
  // naive timestamps are UTC in the DB; Date parses them as local
  const s = /(?:Z|[+-]\d{2}:?\d{2})$/.test(ts.trim()) ? ts : `${ts.trim()}Z`
  const d = new Date(s.includes('T') ? s : s.replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return ts.slice(0, 19)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function ago(ts: string | null | undefined): string {
  if (!ts) return '—'
  const s = (Date.now() - new Date(ts).getTime()) / 1000
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`
  return `${(s / 86400).toFixed(1)}d ago`
}

export function duration(a: string | null | undefined, b?: string | null): string {
  if (!a) return '—'
  // clamp to now: for active rigs `until` is the future term end
  const end = Math.min(b ? new Date(b).getTime() : Infinity, Date.now())
  const s = Math.max(0, (end - new Date(a).getTime()) / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.round((s % 3600) / 60)
  return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : `${h}h ${m}m`
}

export const HEALTH_COLOR: Record<string, string> = {
  ok: 'text-teal-300',
  good: 'text-teal-300',
  warn: 'text-amber-400',
  error: 'text-red-400',
  ssh_error: 'text-red-400',
}
