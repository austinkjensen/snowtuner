import { createFileRoute, Link, useParams } from '@tanstack/react-router'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'
import {
  AlertCircle, ArrowLeft, Check, ChevronRight, Settings2, X, RotateCcw,
} from 'lucide-react'
import {
  api,
  type Recommendation,
  type AutonomousConfig,
  type AutonomousApplication,
} from '@/lib/api'
import { creditsDelta, formatNumber, humanizeAgo } from '@/lib/format'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'

export const Route = createFileRoute('/warehouses/$name')({
  component: WarehouseDetail,
})

function WarehouseDetail() {
  const { name } = useParams({ from: '/warehouses/$name' })

  const warehouses = useQuery({ queryKey: ['warehouses'], queryFn: api.warehouses })
  const proposed = useQuery({
    queryKey: ['recommendations', 'PROPOSED'],
    queryFn: () => api.listRecommendations({ status: 'PROPOSED', limit: 500 }),
  })
  const autoConfig = useQuery({
    queryKey: ['autonomous-config'],
    queryFn: api.listAutonomousConfig,
  })
  const apps = useQuery({
    queryKey: ['autonomous-applications', name],
    queryFn: () => api.listAutonomousApplications({ warehouse: name, limit: 25 }),
  })

  const wh = warehouses.data?.find((w) => w.name === name)
  const myRecs = useMemo(
    () =>
      (proposed.data ?? []).filter(
        (r) => warehouseFromTarget(r.target_resource) === name,
      ),
    [proposed.data, name],
  )
  const myConfigs = useMemo(
    () => (autoConfig.data ?? []).filter((c) => c.warehouse_name === name),
    [autoConfig.data, name],
  )

  if (warehouses.isError) return <ErrorState message="Couldn't load warehouses" />

  if (!warehouses.isLoading && !wh) {
    return (
      <div className="space-y-6">
        <BackLink />
        <Card className="border-destructive/40">
          <CardHeader className="flex-row items-start gap-3 p-6">
            <AlertCircle className="h-5 w-5 text-destructive" />
            <div>
              <CardTitle className="text-foreground">Warehouse not found</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">
                <code className="font-mono">{name}</code> isn't in your local snapshot. Try
                <code className="ml-1 font-mono">snowtuner sync</code> to refresh from
                Snowflake.
              </p>
            </div>
          </CardHeader>
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <BackLink />

      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{name}</h1>
        <p className="text-sm text-muted-foreground">
          {wh ? <WarehouseSummary wh={wh} /> : <span>Loading…</span>}
        </p>
      </div>

      <OpenProposalsCard recs={myRecs} loading={proposed.isLoading} />

      <AutonomousCard warehouseName={name} configs={myConfigs} loading={autoConfig.isLoading} />

      <ApplicationsCard apps={apps.data ?? []} loading={apps.isLoading} />

      {wh && <ActivityCard wh={wh} />}
    </div>
  )
}

// ── Header bits ─────────────────────────────────────────────────────────────

function BackLink() {
  return (
    <Link
      to="/warehouses"
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      <ArrowLeft className="h-3.5 w-3.5" /> Warehouses
    </Link>
  )
}

function WarehouseSummary({ wh }: { wh: import('@/lib/api').Warehouse }) {
  const parts = [
    wh.size ?? null,
    wh.auto_suspend_seconds == null ? null : `${wh.auto_suspend_seconds}s auto-suspend`,
    `${formatNumber(wh.queries_in_window)} queries (14d)`,
    `${formatNumber(wh.suspend_resume_events)} cycles`,
  ].filter(Boolean) as string[]
  return <span>{parts.join(' · ')}</span>
}

// ── Open proposals card ────────────────────────────────────────────────────

function OpenProposalsCard({
  recs,
  loading,
}: {
  recs: Recommendation[]
  loading: boolean
}) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between p-6 pb-2">
        <CardTitle>Open recommendations ({recs.length})</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-3">
        {loading ? (
          <SkeletonRows />
        ) : recs.length === 0 ? (
          <Empty>No open proposals for this warehouse.</Empty>
        ) : (
          <ul className="divide-y divide-border">
            {recs.map((r) => (
              <ProposalRow key={r.id ?? 0} rec={r} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function ProposalRow({ rec }: { rec: Recommendation }) {
  const qc = useQueryClient()
  const accept = useMutation({
    mutationFn: () => api.acceptRecommendation(rec.id ?? 0),
    onSuccess: () => invalidate(qc),
  })
  const reject = useMutation({
    mutationFn: () => api.rejectRecommendation(rec.id ?? 0),
    onSuccess: () => invalidate(qc),
  })

  return (
    <li className="flex flex-col gap-2 px-3 py-3 sm:flex-row sm:items-center">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-mono text-xs text-muted-foreground">#{rec.id}</span>
          <span className="font-medium">{rec.action_type}</span>
          <span className="truncate text-muted-foreground">
            {firstLine(rec.preview)}
          </span>
        </div>
        <div className="mt-0.5 flex items-center gap-3 text-xs text-muted-foreground tabular-nums">
          <span>conf {(rec.expected_impact?.confidence ?? 0).toFixed(2)}</span>
          <span>{creditsDelta(rec.expected_impact?.credits_delta_daily ?? null)}/day</span>
          <span className="font-mono opacity-70">{rec.generated_by}</span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link
            to="/recommendations"
            search={{ id: rec.id ?? undefined }}
          >
            Detail <ChevronRight className="h-3.5 w-3.5" />
          </Link>
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={accept.isPending || reject.isPending}
          onClick={() => accept.mutate()}
        >
          <Check className="h-3.5 w-3.5" /> Accept
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={accept.isPending || reject.isPending}
          onClick={() => reject.mutate()}
        >
          <X className="h-3.5 w-3.5" /> Reject
        </Button>
      </div>
    </li>
  )
}

// ── Autonomous card ────────────────────────────────────────────────────────

// Knobs the ALTER_WAREHOUSE recommenders actually emit today.  Each maps to
// its own autonomous_config row so users can opt in to AUTO_SUSPEND
// autonomy without auto-applying WAREHOUSE_SIZE on the same warehouse.
const KNOWN_ALTER_KNOBS = ['AUTO_SUSPEND', 'WAREHOUSE_SIZE'] as const

function AutonomousCard({
  warehouseName,
  configs,
  loading,
}: {
  warehouseName: string
  configs: AutonomousConfig[]
  loading: boolean
}) {
  const qc = useQueryClient()

  const upsert = useMutation({
    mutationFn: (vars: { knob: string; enabled: boolean }) =>
      api.upsertAutonomousConfig('ALTER_WAREHOUSE', warehouseName, vars.knob, {
        enabled: vars.enabled,
        confidence_threshold: 0.85,
        cooldown_hours: 24,
        max_rollbacks_per_week: 2,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['autonomous-config'] }),
  })

  // Existing rows + a placeholder for any known knob not yet configured.
  const rowsByKnob = new Map<string, AutonomousConfig | null>()
  for (const k of KNOWN_ALTER_KNOBS) rowsByKnob.set(k, null)
  for (const c of configs) {
    if (c.action_type !== 'ALTER_WAREHOUSE') continue
    rowsByKnob.set(c.knob ?? '*', c)
  }
  const hasCatchAll = configs.some(
    (c) => c.action_type === 'ALTER_WAREHOUSE' && c.knob === '*',
  )

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between p-6 pb-2">
        <CardTitle>Autonomous mode (ALTER_WAREHOUSE)</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-3">
        {loading ? (
          <SkeletonRows />
        ) : (
          <>
            <div className="px-3 pb-3 text-xs text-muted-foreground">
              Each knob is gated independently. A specific knob row overrides
              the ★ catch-all row when both exist.
            </div>

            <div className="rounded-md border border-warning/40 bg-warning/5 px-3 py-3 text-xs mb-2">
              <p className="font-medium text-foreground">Before any apply runs:</p>
              <p className="mt-1 text-muted-foreground">
                The SNOWTUNER_ROLE needs <code className="font-mono">MODIFY</code> on{' '}
                <code className="font-mono">{warehouseName}</code>. As ACCOUNTADMIN in Snowsight:
              </p>
              <pre className="mt-2 overflow-x-auto rounded bg-muted/50 px-2 py-1.5 font-mono text-[11px]">
                GRANT MODIFY, OPERATE ON WAREHOUSE {warehouseName} TO ROLE SNOWTUNER_ROLE;
              </pre>
            </div>

            <ul className="divide-y divide-border">
              {hasCatchAll && rowsByKnob.get('*') && (
                <ConfigRow
                  key="catch-all"
                  config={rowsByKnob.get('*')!}
                  knobLabel="★ every knob (catch-all)"
                />
              )}
              {KNOWN_ALTER_KNOBS.map((knob) => {
                const cfg = rowsByKnob.get(knob)
                if (cfg) {
                  return (
                    <ConfigRow key={knob} config={cfg} knobLabel={knob} />
                  )
                }
                return (
                  <li
                    key={knob}
                    className="flex flex-col gap-2 px-3 py-3 sm:flex-row sm:items-center"
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium">{knob}</span>
                        <Badge variant="outline">OFF</Badge>
                        {hasCatchAll && (
                          <span className="text-xs text-muted-foreground">
                            (covered by catch-all)
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        Not individually configured.
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant="default"
                        size="sm"
                        onClick={() => upsert.mutate({ knob, enabled: true })}
                        disabled={upsert.isPending}
                      >
                        <Settings2 className="h-3.5 w-3.5" />
                        Enable
                      </Button>
                    </div>
                  </li>
                )
              })}
            </ul>

            {!hasCatchAll && (
              <div className="px-3 pt-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => upsert.mutate({ knob: '*', enabled: true })}
                  disabled={upsert.isPending}
                >
                  + Enable catch-all (every knob)
                </Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function ConfigRow({
  config,
  knobLabel,
}: {
  config: AutonomousConfig
  knobLabel: string
}) {
  const qc = useQueryClient()
  const toggle = useMutation({
    mutationFn: () =>
      api.upsertAutonomousConfig(
        config.action_type,
        config.warehouse_name,
        config.knob ?? '*',
        {
          enabled: !config.enabled,
          confidence_threshold: config.confidence_threshold,
          cooldown_hours: config.cooldown_hours,
          max_rollbacks_per_week: config.max_rollbacks_per_week,
        },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['autonomous-config'] }),
  })
  const resetCircuit = useMutation({
    mutationFn: () =>
      api.resetAutonomousCircuit(
        config.action_type, config.warehouse_name, config.knob ?? '*',
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['autonomous-config'] }),
  })

  const circuitOpen =
    config.circuit_open_until != null && new Date(config.circuit_open_until + 'Z') > new Date()

  return (
    <li className="flex flex-col gap-2 px-3 py-3 sm:flex-row sm:items-center">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium">{knobLabel}</span>
          {config.enabled ? (
            <Badge variant="success">ON</Badge>
          ) : (
            <Badge variant="outline">OFF</Badge>
          )}
          {circuitOpen && (
            <Badge variant="warning" title={`Circuit open until ${config.circuit_open_until}`}>
              circuit open
            </Badge>
          )}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground tabular-nums">
          <span>threshold {config.confidence_threshold.toFixed(2)}</span>
          <span>cooldown {config.cooldown_hours}h</span>
          <span>max rollbacks/wk {config.max_rollbacks_per_week}</span>
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {circuitOpen && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => resetCircuit.mutate()}
            disabled={resetCircuit.isPending}
          >
            Reset circuit
          </Button>
        )}
        <Button
          variant={config.enabled ? 'ghost' : 'default'}
          size="sm"
          onClick={() => toggle.mutate()}
          disabled={toggle.isPending}
        >
          {config.enabled ? 'Disable' : 'Enable'}
        </Button>
      </div>
    </li>
  )
}

// ── Applications log ───────────────────────────────────────────────────────

function ApplicationsCard({
  apps,
  loading,
}: {
  apps: AutonomousApplication[]
  loading: boolean
}) {
  return (
    <Card>
      <CardHeader className="p-6 pb-2">
        <CardTitle>Recent applications</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-3">
        {loading ? (
          <SkeletonRows />
        ) : apps.length === 0 ? (
          <Empty>No autonomous applications recorded for this warehouse.</Empty>
        ) : (
          <ul className="divide-y divide-border">
            {apps.map((a) => (
              <ApplicationRow key={a.id} app={a} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function ApplicationRow({ app }: { app: AutonomousApplication }) {
  const qc = useQueryClient()
  const rollback = useMutation({
    mutationFn: () => api.rollbackAutonomousApplication(app.id),
    onSuccess: () => invalidate(qc),
  })
  const stateVariant: 'success' | 'warning' | 'destructive' =
    app.state === 'APPLIED' ? 'success' : app.state === 'ROLLED_BACK' ? 'warning' : 'destructive'

  return (
    <li className="flex flex-col gap-2 px-3 py-3 sm:flex-row sm:items-center">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-mono text-xs text-muted-foreground">#{app.id}</span>
          <span className="font-medium">{app.action_type}</span>
          <Badge variant={stateVariant} className="text-[10px] uppercase tracking-wide">
            {app.state}
          </Badge>
          <span className="ml-auto text-xs text-muted-foreground">
            {humanizeAgo(app.applied_at)}
          </span>
        </div>
        <div className="mt-0.5 truncate font-mono text-xs text-muted-foreground" title={app.applied_sql}>
          {app.applied_sql}
        </div>
        {app.error && (
          <p className="mt-1 text-xs text-destructive">Error: {app.error}</p>
        )}
      </div>
      {app.state === 'APPLIED' && app.rollback_sql && (
        <div className="flex shrink-0 items-center">
          <Button
            variant="outline"
            size="sm"
            onClick={() => rollback.mutate()}
            disabled={rollback.isPending}
          >
            <RotateCcw className="h-3.5 w-3.5" />
            {rollback.isPending ? 'Rolling back…' : 'Rollback'}
          </Button>
        </div>
      )}
    </li>
  )
}

// ── Activity card ──────────────────────────────────────────────────────────

function ActivityCard({ wh }: { wh: import('@/lib/api').Warehouse }) {
  return (
    <Card>
      <CardHeader className="p-6 pb-2">
        <CardTitle>Activity (last 14 days)</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-4 p-6 pt-2 sm:grid-cols-4">
        <Stat label="Queries" value={formatNumber(wh.queries_in_window)} />
        <Stat label="Suspend / resume" value={formatNumber(wh.suspend_resume_events)} />
        <Stat label="Auto-suspend" value={wh.auto_suspend_seconds == null ? '—' : `${wh.auto_suspend_seconds}s`} />
        <Stat label="Auto-resume" value={wh.auto_resume == null ? '—' : wh.auto_resume ? 'on' : 'off'} />
      </CardContent>
    </Card>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-xl font-semibold tabular-nums">{value}</div>
    </div>
  )
}

// ── Shared bits ────────────────────────────────────────────────────────────

function ErrorState({ message }: { message: string }) {
  return (
    <Card className="border-destructive/40">
      <CardHeader className="flex-row items-start gap-3 p-6">
        <AlertCircle className="h-5 w-5 text-destructive" />
        <div>
          <CardTitle className="text-foreground">{message}</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Is <code className="font-mono">snowtuner api</code> running?
          </p>
        </div>
      </CardHeader>
    </Card>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="px-3 py-6 text-center text-sm text-muted-foreground">{children}</p>
}

function SkeletonRows() {
  return (
    <ul className="divide-y divide-border">
      {[0, 1].map((i) => (
        <li key={i} className="px-3 py-3">
          <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
        </li>
      ))}
    </ul>
  )
}

// ── helpers ────────────────────────────────────────────────────────────────

function warehouseFromTarget(target: string | null | undefined): string | null {
  if (!target) return null
  const m = target.match(/^warehouse:([^:]+)/)
  return m ? m[1] : null
}

function firstLine(text: string | null | undefined): string {
  if (!text) return ''
  const idx = text.indexOf('\n')
  return idx >= 0 ? text.slice(0, idx) : text
}

function invalidate(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['recommendations'] })
  qc.invalidateQueries({ queryKey: ['autonomous-applications'] })
  qc.invalidateQueries({ queryKey: ['warehouses'] })
  qc.invalidateQueries({ queryKey: ['status'] })
}
