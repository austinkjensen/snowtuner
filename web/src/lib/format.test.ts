/**
 * Tests for the formatting helpers in lib/format.ts.
 *
 * These run everywhere in the UI — credit deltas in tables, "5m ago"
 * relative times in the activity feed, etc.  A regression here is
 * visually pervasive and would be obvious *if* someone ran the app, but
 * easy to miss in PR review.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { creditsDelta, humanizeAgo, formatNumber } from './format'

describe('creditsDelta', () => {
  it('returns dash for null/undefined', () => {
    expect(creditsDelta(null)).toBe('—')
    expect(creditsDelta(undefined)).toBe('—')
  })

  it('returns "≈0" for negligible values', () => {
    // Threshold is 0.005 (matches the backend convention)
    expect(creditsDelta(0.001)).toBe('≈0')
    expect(creditsDelta(-0.003)).toBe('≈0')
    expect(creditsDelta(0)).toBe('≈0')
  })

  it('prefixes positive values with +', () => {
    expect(creditsDelta(1.5)).toBe('+1.50')
    expect(creditsDelta(10)).toBe('+10.00')
  })

  it('shows negative values with minus', () => {
    // Negative = savings — operator's eye wants this to be clearly different
    // from cost.  No "+" prefix; the minus comes from toFixed.
    expect(creditsDelta(-1.5)).toBe('-1.50')
  })

  it('always shows 2 decimal places', () => {
    expect(creditsDelta(0.5)).toBe('+0.50')
    expect(creditsDelta(1)).toBe('+1.00')
  })
})

describe('humanizeAgo', () => {
  // Freeze "now" at a known time so age math is deterministic.  Only
  // mock Date — see freshness-pill.test.tsx for why we don't mock all
  // timers (TanStack Query relies on real setTimeout).
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-01-15T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns dash for null/undefined/empty', () => {
    expect(humanizeAgo(null)).toBe('—')
    expect(humanizeAgo(undefined)).toBe('—')
    expect(humanizeAgo('')).toBe('—')
  })

  it('handles "just now" for sub-minute ages', () => {
    // 30 seconds ago
    expect(humanizeAgo('2026-01-15T11:59:30Z')).toBe('just now')
  })

  it('reports minutes', () => {
    // 5 minutes ago
    expect(humanizeAgo('2026-01-15T11:55:00Z')).toBe('5m ago')
  })

  it('reports hours', () => {
    // 3 hours ago
    expect(humanizeAgo('2026-01-15T09:00:00Z')).toBe('3h ago')
  })

  it('reports days', () => {
    // 2 days ago
    expect(humanizeAgo('2026-01-13T12:00:00Z')).toBe('2d ago')
  })

  it('treats naive UTC strings (no tz suffix) as UTC', () => {
    // FastAPI emits naive UTC: "2026-01-15T11:55:00".  The helper
    // appends 'Z' before parsing so the comparison is correct.
    expect(humanizeAgo('2026-01-15T11:55:00')).toBe('5m ago')
  })

  it('handles future timestamps gracefully', () => {
    expect(humanizeAgo('2026-01-16T00:00:00Z')).toBe('in the future')
  })

  it('returns the raw string on unparseable input', () => {
    expect(humanizeAgo('not a date')).toBe('not a date')
  })
})

describe('formatNumber', () => {
  it('returns dash for null', () => {
    expect(formatNumber(null)).toBe('—')
  })

  it('adds thousand separators', () => {
    expect(formatNumber(1234)).toBe('1,234')
    expect(formatNumber(1234567)).toBe('1,234,567')
  })

  it('handles small numbers without separators', () => {
    expect(formatNumber(42)).toBe('42')
    expect(formatNumber(0)).toBe('0')
  })
})
