import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, AlertCircle } from 'lucide-react'
import { api } from '@/lib/api'
import { creditsDelta, humanizeAgo } from '@/lib/format'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { SkeletonRows } from '@/components/skeleton-rows'

export const Route = createFileRoute('/')({
  component: Dashboard,
})

function Dashboard() {
  const status = useQuery({ queryKey: ['status'], queryFn: api.status })
  const proposed = useQuery({
    queryKey: ['recommendations', 'PROPOSED'],
    queryFn: () => api.listRecommendations({ status: 'PROPOSED', limit: 500 }),
  })
  const recentApps = useQuery({
    queryKey: ['autonomous-applications'],
    queryFn: () => api.listAutonomousApplications({ limit: 5 }),
  })

  if (status.isError || proposed.isError || recentApps.isError) {
    return <ErrorState />
  }

  const counts = status.data?.recommendation_counts ?? {}
  const openCount = counts.PROPOSED ?? 0
  const appliedCount = counts.APPLIED ?? 0
  const totalCreditDelta = (proposed.data ?? []).reduce(
    (acc, r) => acc + (r.expected_impact?.credits_delta_daily ?? 0),
    0,
  )
  const lastSync = status.data?.sources
    .map((s) => s.last_synced_at)
    .filter(Boolean)
    .sort()
    .at(-1)
  const isLoading = status.isLoading || proposed.isLoading || recentApps.isLoading

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="text-sm text-muted-foreground">
          Snapshot of your Snowflake account as snowtuner sees it right now.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard label="Open proposals" value={openCount} loading={isLoading} />
        <KpiCard label="Applied (lifetime)" value={appliedCount} loading={isLoading} />
        <KpiCard
          label="Daily credit delta"
          value={creditsDelta(totalCreditDelta)}
          loading={isLoading}
          hint="Sum across open proposals. Negative = savings."
        />
        <KpiCard
          label="Last sync"
          value={humanizeAgo(lastSync ?? null)}
          loading={isLoading}
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center justify-between p-6 pb-0">
            <CardTitle>Recent recommendations</CardTitle>
            <Button asChild variant="ghost" size="sm">
              <Link to="/recommendations">
                view all <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </CardHeader>
          <CardContent className="p-3 pt-3">
            {proposed.data?.length ? (
              <ul className="divide-y divide-border">
                {proposed.data.slice(0, 5).map((r) => (
                  <RecommendationRow key={r.id ?? 0} rec={r} />
                ))}
              </ul>
            ) : isLoading ? (
              <SkeletonRows />
            ) : (
              <EmptyHint>
                No open proposals. Run <code className="font-mono">snowtuner run</code> to refresh.
              </EmptyHint>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="p-6 pb-0">
            <CardTitle>Recent autonomous applications</CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-3">
            {recentApps.data?.length ? (
              <ul className="divide-y divide-border">
                {recentApps.data.map((a) => (
                  <li key={a.id} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                    <span className="font-mono text-xs text-muted-foreground">#{a.id}</span>
                    <span className="font-medium">{a.warehouse_name ?? '—'}</span>
                    <span className="text-muted-foreground">{a.action_type}</span>
                    <ApplicationStateBadge state={a.state} />
                    <span className="ml-auto text-xs text-muted-foreground">
                      {humanizeAgo(a.applied_at)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : isLoading ? (
              <SkeletonRows />
            ) : (
              <EmptyHint>
                No autonomous applications yet. Enable autonomous mode for a warehouse to see them here.
              </EmptyHint>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function KpiCard({
  label,
  value,
  loading,
  hint,
}: {
  label: string
  value: number | string
  loading?: boolean
  hint?: string
}) {
  return (
    <Card>
      <CardHeader className="p-6 pb-2">
        <CardTitle>{label}</CardTitle>
      </CardHeader>
      <CardContent className="p-6 pt-0">
        <div className="text-3xl font-semibold tabular-nums">
          {loading ? <span className="text-muted-foreground">—</span> : value}
        </div>
        {hint && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
      </CardContent>
    </Card>
  )
}

function RecommendationRow({ rec }: { rec: import('@/lib/api').Recommendation }) {
  const cdd = rec.expected_impact?.credits_delta_daily ?? null
  return (
    <li className="flex items-center gap-3 px-3 py-2.5 text-sm">
      <span className="font-mono text-xs text-muted-foreground">#{rec.id}</span>
      <span className="font-medium">{shortTarget(rec.target_resource)}</span>
      <span className="text-muted-foreground">{rec.action_type}</span>
      <span className="ml-auto flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
        <span>conf {(rec.expected_impact?.confidence ?? 0).toFixed(2)}</span>
        <span>{creditsDelta(cdd)}/day</span>
      </span>
    </li>
  )
}

function ApplicationStateBadge({ state }: { state: string }) {
  const variant =
    state === 'APPLIED' ? 'success' : state === 'ROLLED_BACK' ? 'warning' : 'destructive'
  return (
    <Badge variant={variant} className="text-[10px] uppercase tracking-wide">
      {state}
    </Badge>
  )
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return <p className="px-3 py-6 text-center text-sm text-muted-foreground">{children}</p>
}

function ErrorState() {
  return (
    <Card className="border-destructive/40">
      <CardHeader className="flex-row items-start gap-3 p-6">
        <AlertCircle className="h-5 w-5 text-destructive" />
        <div>
          <CardTitle className="text-foreground">Can't reach the snowtuner API</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Is <code className="font-mono">snowtuner api</code> running? The UI talks to it via{' '}
            <code className="font-mono">/api/*</code> (proxied to{' '}
            <code className="font-mono">http://127.0.0.1:8770</code> in dev).
          </p>
        </div>
      </CardHeader>
    </Card>
  )
}

function shortTarget(target: string | null | undefined): string {
  if (!target) return '—'
  // Strip the warehouse: prefix, drop the trailing :KNOB suffix for readability.
  return target.replace(/^warehouse:/, '').replace(/:.*$/, '')
}
