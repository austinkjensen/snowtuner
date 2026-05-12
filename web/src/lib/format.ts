/** UI-side display helpers, mirroring src/snowtuner/format.py. */

const NEGLIGIBLE = 0.005

export function creditsDelta(value: number | null | undefined): string {
  if (value == null) return '—'
  if (Math.abs(value) < NEGLIGIBLE) return '≈0'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}`
}

export function humanizeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  // Stored timestamps are naive UTC by convention (see backend storage.db).
  // ISO strings without a tz suffix from FastAPI need the 'Z' added.
  const withTz = /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'
  const t = new Date(withTz)
  if (Number.isNaN(t.getTime())) return iso
  const secs = Math.floor((Date.now() - t.getTime()) / 1000)
  if (secs < 0) return 'in the future'
  if (secs < 60) return 'just now'
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export function formatNumber(n: number | null | undefined): string {
  if (n == null) return '—'
  return n.toLocaleString()
}
