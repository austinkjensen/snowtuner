/**
 * Tests for the FreshnessPill component.
 *
 * We're verifying the *labeling logic* — given a freshness state, does
 * the pill show the right text + severity dot color?  We mock the API
 * client so we don't need a real backend; the network layer is exercised
 * in the Python integration suite.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FreshnessPill } from './freshness-pill'
import { api } from '@/lib/api'

// Helper: render the pill inside a fresh QueryClient so the useQuery
// hooks have a place to cache.  The default `retry: false` keeps
// failed-query tests from hanging on backoff retries.
function renderPill() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <FreshnessPill />
    </QueryClientProvider>,
  )
}

describe('FreshnessPill', () => {
  beforeEach(() => {
    // Only mock the Date constructor — leaving setTimeout/setInterval
    // alone so TanStack Query's internal scheduling still fires.  Mocking
    // ALL timers makes useQuery hooks hang forever, timing out the test.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-01-15T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('shows "never synced" when no source watermarks exist', async () => {
    vi.spyOn(api, 'status').mockResolvedValue({
      sources: [],
      warehouses: [],
      recommender_states: [],
      recommendation_counts: {},
    })
    vi.spyOn(api, 'automationStatus').mockResolvedValue({
      enabled: false,
      interval_seconds: 0,
      currently_running: false,
    })

    renderPill()
    // The pill's aria-label is "Data freshness: <state>".  Use it as
    // the testable surface — the rendered span text is wrapped in
    // whitespace-nowrap markup which is harder to match exactly.
    const pill = await screen.findByLabelText(/Data freshness:/)
    expect(pill).toHaveAttribute('aria-label', 'Data freshness: never synced')
  })

  it('shows a "Xm ago" label when sources have recent syncs', async () => {
    vi.spyOn(api, 'status').mockResolvedValue({
      sources: [
        {
          name: 'query_history',
          rows: 0,
          earliest: null,
          latest: null,
          // 5 minutes ago, naive UTC (matches FastAPI emission)
          last_synced_at: '2026-01-15T11:55:00',
        },
      ],
      warehouses: [],
      recommender_states: [],
      recommendation_counts: {},
    })
    vi.spyOn(api, 'automationStatus').mockResolvedValue({
      enabled: true,
      interval_seconds: 3600,
      currently_running: false,
    })

    renderPill()
    const pill = await screen.findByLabelText(/Data freshness: 5m ago/)
    expect(pill).toBeInTheDocument()
  })

  it('shows the oldest source when multiple sources have different ages', async () => {
    // Pill displays the OLDEST source — if any one source is stale,
    // the whole installation is stale.
    vi.spyOn(api, 'status').mockResolvedValue({
      sources: [
        { name: 'query_history', rows: 0, earliest: null, latest: null,
          last_synced_at: '2026-01-15T11:55:00' },  // 5m ago (fresh)
        { name: 'warehouse_events', rows: 0, earliest: null, latest: null,
          last_synced_at: '2026-01-15T09:00:00' },  // 3h ago (stale-ish)
      ],
      warehouses: [],
      recommender_states: [],
      recommendation_counts: {},
    })
    vi.spyOn(api, 'automationStatus').mockResolvedValue({
      enabled: false,
      interval_seconds: 0,
      currently_running: false,
    })

    renderPill()
    const pill = await screen.findByLabelText(/3h ago/)
    expect(pill).toBeInTheDocument()
  })

  it('skips sources with null last_synced_at when computing oldest', async () => {
    // A source that's never synced shouldn't pollute the "oldest" calc —
    // it should be ignored, not treated as infinitely old.
    vi.spyOn(api, 'status').mockResolvedValue({
      sources: [
        { name: 'query_history', rows: 0, earliest: null, latest: null,
          last_synced_at: '2026-01-15T11:55:00' },  // 5m
        { name: 'metering', rows: 0, earliest: null, latest: null,
          last_synced_at: null },  // never synced — should be ignored
      ],
      warehouses: [],
      recommender_states: [],
      recommendation_counts: {},
    })
    vi.spyOn(api, 'automationStatus').mockResolvedValue({
      enabled: false,
      interval_seconds: 0,
      currently_running: false,
    })

    renderPill()
    // The 5m source wins as "oldest" (it's the only one we can age).
    const pill = await screen.findByLabelText(/5m ago/)
    expect(pill).toBeInTheDocument()
  })
})
