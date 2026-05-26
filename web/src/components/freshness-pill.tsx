/**
 * Data-freshness indicator pill for the top nav.
 *
 * Renders a tiny status badge showing how long ago the oldest source was
 * synced.  Color thresholds:
 *
 *   * green  — under 2 hours (matches Snowflake ACCOUNT_USAGE's ~45min lag;
 *              anything under 2h is "as fresh as it can be")
 *   * yellow — under 24 hours (stale but workable)
 *   * red    — over 24 hours (probably broken sync, investigate)
 *   * grey   — no sync ever ("snowtuner sync" never run on this install)
 *
 * Click the pill to open a popover showing per-source freshness, the
 * AutomationLoop's state (enabled / interval / next tick), and a "Sync now"
 * button that fires `POST /sync/run` against the API.
 */
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { humanizeAgo } from '@/lib/format'
import { cn } from '@/lib/utils'

type Severity = 'fresh' | 'stale' | 'critical' | 'never'

const FRESH_MS = 2 * 60 * 60 * 1000        // 2h
const STALE_MS = 24 * 60 * 60 * 1000       // 24h

function ageMs(iso: string | null | undefined): number | null {
  if (!iso) return null
  // FastAPI emits naive UTC; humanizeAgo handles the same fixup.
  const withTz = /[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'
  const t = new Date(withTz)
  if (Number.isNaN(t.getTime())) return null
  return Date.now() - t.getTime()
}

function severityFor(ms: number | null): Severity {
  if (ms === null) return 'never'
  if (ms < FRESH_MS) return 'fresh'
  if (ms < STALE_MS) return 'stale'
  return 'critical'
}

const SEVERITY_CLASS: Record<Severity, string> = {
  fresh:
    'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/20',
  stale:
    'bg-amber-500/15 text-amber-700 dark:text-amber-300 hover:bg-amber-500/20',
  critical:
    'bg-red-500/15 text-red-700 dark:text-red-300 hover:bg-red-500/20',
  never:
    'bg-muted text-muted-foreground hover:bg-muted/80',
}

const SEVERITY_DOT: Record<Severity, string> = {
  fresh: 'bg-emerald-500',
  stale: 'bg-amber-500',
  critical: 'bg-red-500',
  never: 'bg-muted-foreground/50',
}

export function FreshnessPill() {
  const qc = useQueryClient()

  // Refetch every 60s so the pill ages itself without the user reloading.
  const status = useQuery({
    queryKey: ['status'],
    queryFn: api.status,
    refetchInterval: 60_000,
  })
  // Refetch every 30s — automation state can change underneath us.
  const automation = useQuery({
    queryKey: ['automation-status'],
    queryFn: api.automationStatus,
    refetchInterval: 30_000,
  })

  const syncNow = useMutation({
    mutationFn: api.runSync,
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['status'] })
      qc.invalidateQueries({ queryKey: ['automation-status'] })
    },
  })

  // Oldest source's last_synced_at is the headline metric.  An install with
  // some fresh sources but one totally-stale source is in the "stale" state.
  const sources = status.data?.sources ?? []
  const oldestSync = sources
    .map((s) => s.last_synced_at)
    .filter((v): v is string => Boolean(v))
    .sort()
    .at(0)              // ascending sort, first item = oldest
  const oldestAge = ageMs(oldestSync)
  const severity = severityFor(oldestAge)
  const label = severity === 'never' ? 'never synced' : humanizeAgo(oldestSync)

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          aria-label={`Data freshness: ${label}`}
          className={cn(
            'ml-3 flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-xs font-medium transition-colors',
            SEVERITY_CLASS[severity],
          )}
        >
          <span
            className={cn('h-1.5 w-1.5 rounded-full', SEVERITY_DOT[severity])}
            aria-hidden
          />
          <span>{label}</span>
        </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="start"
          sideOffset={6}
          className={cn(
            'z-50 w-80 rounded-md border bg-popover p-3 text-popover-foreground shadow-md',
            'data-[state=open]:animate-in data-[state=closed]:animate-out',
          )}
        >
          <div className="mb-2 text-sm font-medium">Data freshness</div>

          {sources.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No sync watermarks yet.  Run{' '}
              <code className="rounded bg-muted px-1 py-0.5 text-[10px]">
                snowtuner sync
              </code>{' '}
              to ingest from Snowflake.
            </p>
          ) : (
            <ul className="space-y-1.5 text-xs">
              {sources.map((s) => {
                const sev = severityFor(ageMs(s.last_synced_at))
                return (
                  <li key={s.name} className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={cn('h-1.5 w-1.5 rounded-full', SEVERITY_DOT[sev])}
                        aria-hidden
                      />
                      <span className="truncate font-mono text-[11px]">
                        {s.name}
                      </span>
                    </div>
                    <span className="shrink-0 text-muted-foreground">
                      {s.last_synced_at ? humanizeAgo(s.last_synced_at) : '—'}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}

          <DropdownMenu.Separator className="my-3 -mx-3 h-px bg-border" />

          {/* AutomationLoop status — tells the operator whether ticks are
              auto-firing or whether this install is fully manual. */}
          <div className="space-y-1 text-xs">
            <div className="flex items-center justify-between">
              <span className="text-muted-foreground">Automation</span>
              {automation.data?.enabled ? (
                <span className="font-medium text-emerald-700 dark:text-emerald-300">
                  every {Math.round(automation.data.interval_seconds / 60)} min
                </span>
              ) : (
                <span className="font-medium text-muted-foreground">disabled</span>
              )}
            </div>
            {automation.data?.next_run_at && automation.data.enabled && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Next tick</span>
                <span>{humanizeAgo(automation.data.next_run_at)}</span>
              </div>
            )}
            {automation.data?.last_tick && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Last tick</span>
                <span
                  className={cn(
                    'font-medium',
                    automation.data.last_tick.overall === 'success' &&
                      'text-emerald-700 dark:text-emerald-300',
                    automation.data.last_tick.overall === 'failed' &&
                      'text-red-700 dark:text-red-300',
                    automation.data.last_tick.overall === 'skipped' &&
                      'text-amber-700 dark:text-amber-300',
                  )}
                >
                  {automation.data.last_tick.overall}
                </span>
              </div>
            )}
          </div>

          <DropdownMenu.Separator className="my-3 -mx-3 h-px bg-border" />

          <div className="flex items-center justify-between gap-2">
            <button
              onClick={() => syncNow.mutate()}
              disabled={syncNow.isPending}
              className={cn(
                'inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1 text-xs font-medium transition-colors hover:bg-accent',
                syncNow.isPending && 'opacity-60',
              )}
            >
              {syncNow.isPending ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <RefreshCw className="h-3 w-3" />
              )}
              {syncNow.isPending ? 'Syncing…' : 'Sync now'}
            </button>
            {syncNow.isError && (
              <span className="text-[10px] text-red-700 dark:text-red-300">
                failed
              </span>
            )}
            {syncNow.isSuccess && (
              <span className="text-[10px] text-emerald-700 dark:text-emerald-300">
                done
              </span>
            )}
          </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
